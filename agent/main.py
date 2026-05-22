# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Meta, Twilio) gracias a la capa de providers.
Sincroniza cada contacto como lead en Koala OS (Supabase).
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from agent.brain import generar_respuesta, resumir_conversacion
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.koala_sync import (
    KoalaSyncConfig,
    upsert_lead_from_message,
    append_wa_message,
    find_open_lead as koala_find_open_lead,
)

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Configuración de Koala OS CRM (None si faltan variables)
_koala_cfg = KoalaSyncConfig.from_env()
if _koala_cfg:
    logger.info("Koala OS CRM habilitado — leads se sincronizan a Supabase")
else:
    logger.info("Koala OS CRM no configurado — leads no se sincronizan")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — WhatsApp AI Agent",
    version="1.0.0",
    lifespan=lifespan
)

# CORS para panel Koala OS + widget web
# Configurable via CORS_EXTRA_ORIGINS (coma-separado). Usar "*" para permitir cualquier origen
# (recomendado solo para el widget público, ya que /chat no usa cookies).
_extra_origins = [o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]
if "*" in _extra_origins:
    _cors_origins = ["*"]
else:
    _cors_origins = [
        "https://koala-os.netlify.app",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ] + _extra_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Servir el widget web embebible y la página demo
_static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def root():
    """Endpoint raíz."""
    return {"status": "ok", "service": "agentkit"}


@app.get("/health")
async def health():
    """Health check para Koala OS y Railway."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "provider": os.getenv("WHATSAPP_PROVIDER", "none"),
    }


@app.post("/outbound/send")
async def outbound_send(request: Request):
    """Envía un mensaje OUT-BOUND (del operador hacia el cliente).

    Esta ruta es la que invoca Koala OS desde la pestaña Canales >
    WhatsApp cuando el operador escribe una respuesta. Va siempre
    detrás de una Edge Function de Supabase (`wa-send-operator-message`)
    que autentica al usuario y agrega el header `X-Koala-Secret`.

    Body esperado:
      {
        "phone":            "+5491136957601",   # E.164
        "content":          "Hola, ¿en qué te puedo ayudar?",
        "contacto_nombre":  "Cliente X",       # opcional
        "location_id":      "loc-pilar",       # opcional
        "lead_id":          "uuid"             # opcional
      }
    """
    expected = os.getenv("KOALA_OUTBOUND_SECRET", "").strip()
    if not expected:
        # No configurado: por seguridad rechazamos.
        raise HTTPException(503, "outbound disabled")

    provided = request.headers.get("X-Koala-Secret", "").strip()
    if provided != expected:
        raise HTTPException(403, "forbidden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")

    phone = (body.get("phone") or "").strip()
    content = (body.get("content") or "").strip()
    if not phone or not content:
        raise HTTPException(400, "phone y content son requeridos")

    contacto_nombre = body.get("contacto_nombre")
    location_id = body.get("location_id")
    lead_id = body.get("lead_id")

    logger.info(f"outbound_send → phone={phone} len={len(content)}")

    # 1) Enviar por el proveedor (Twilio / Meta)
    try:
        await proveedor.enviar_mensaje(phone, content)
    except Exception as exc:
        logger.error(f"outbound_send: proveedor falló: {exc}")
        raise HTTPException(502, f"proveedor falló: {exc}")

    # 2) Persistir el mensaje en wa_conversations (best-effort)
    conv_id: str | None = None
    if _koala_cfg:
        try:
            conv_id = await append_wa_message(
                cfg=_koala_cfg,
                phone=phone,
                role="operator",
                content=content,
                proveedor=os.getenv("WHATSAPP_PROVIDER", "twilio").lower() or "twilio",
                contacto_nombre=contacto_nombre,
                location_id=location_id,
                lead_id=lead_id,
            )
        except Exception as exc:
            logger.warning(f"outbound_send: append_wa_message falló: {exc}")

    return {"status": "sent", "conversation_id": conv_id}


# ═════════════════════════════════════════════════════════════════
# Chat web — mismo bot, distinto canal
# El widget embebible en una página web golpea acá. Reutiliza
# brain.py + memory.py + koala_sync.py. Las sesiones web se guardan
# en SQLite con el prefijo "web:" para no colisionar con números de
# WhatsApp, y se sincronizan a Koala OS CRM como leads con canal="web".
# ═════════════════════════════════════════════════════════════════

WEB_SESSION_PREFIX = "web:"
WEB_CANAL = "web"
WEB_FUENTE = "Agente IA Web"
WEB_PROVEEDOR = "web"


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=4000)
    nombre: Optional[str] = Field(default=None, max_length=120)


class ChatResponse(BaseModel):
    response: str
    session_id: str


@app.post("/chat", response_model=ChatResponse)
async def chat_web(req: ChatRequest):
    """Recibe un mensaje del widget web, devuelve la respuesta del bot
    y sincroniza el lead + mensajes a Koala OS CRM (Supabase).

    Replica las 4 fases del webhook de WhatsApp:
      1. Persistencia inmediata del lead + user message (nunca perder un mensaje)
      2. (no aplica multimedia en web)
      3. Generación de respuesta + persistencia del bot message
      4. Resumen + clasificación de intent (best-effort)
    """
    session_id = req.session_id
    session_key = f"{WEB_SESSION_PREFIX}{session_id}"
    nombre = req.nombre

    # ─── FASE 1: persistir lead + user message ───────────────────
    lead_id: Optional[str] = None
    if _koala_cfg:
        try:
            lead_id = await upsert_lead_from_message(
                cfg=_koala_cfg,
                phone=session_key,
                name=nombre or None,
                last_message=req.message,
                canal=WEB_CANAL,
                fuente_detalle=WEB_FUENTE,
            )
        except Exception as exc:
            logger.warning("upsert_lead (web user) falló: %s", exc)

        try:
            await append_wa_message(
                cfg=_koala_cfg,
                phone=session_key,
                role="user",
                content=req.message,
                proveedor=WEB_PROVEEDOR,
                contacto_nombre=nombre or None,
                lead_id=lead_id,
            )
        except Exception as exc:
            logger.warning("append_wa_message (web user) falló: %s", exc)

    # ─── FASE 3: generar respuesta + persistir bot message ───────
    historial = await obtener_historial(session_key)
    respuesta = await generar_respuesta(req.message, historial)

    try:
        await guardar_mensaje(session_key, "user", req.message)
        await guardar_mensaje(session_key, "assistant", respuesta)
    except Exception as exc:
        logger.warning("guardar_mensaje (web) falló: %s", exc)

    if _koala_cfg:
        try:
            await append_wa_message(
                cfg=_koala_cfg,
                phone=session_key,
                role="bot",
                content=respuesta,
                proveedor=WEB_PROVEEDOR,
                contacto_nombre=nombre or None,
                lead_id=lead_id,
            )
        except Exception as exc:
            logger.warning("append_wa_message (web bot) falló: %s", exc)

    logger.info(f"web chat session={session_id[:8]}… msg='{req.message[:60]}'")

    # ─── FASE 4: resumen + clasificación de intent (best-effort) ─
    if _koala_cfg:
        try:
            historial_completo = await obtener_historial(session_key)
            open_lead = await koala_find_open_lead(_koala_cfg, session_key)
            intent_actual = (open_lead or {}).get("intent") or "indefinido"

            analisis = await resumir_conversacion(
                historial_completo,
                req.message,
                intent_actual=intent_actual,
            )
            analisis = analisis if isinstance(analisis, dict) else {}
            resumen = analisis.get("resumen", "") or ""
            etiquetas_an = analisis.get("etiquetas", []) or []
            intent_an = analisis.get("intent", "indefinido")
            status_an = analisis.get("intent_status", "abierto")
            changed_an = bool(analisis.get("intent_changed", False))
            resumen_intent = analisis.get("resumen_intent", resumen) or resumen

            await upsert_lead_from_message(
                cfg=_koala_cfg,
                phone=session_key,
                name=nombre or None,
                last_message=req.message,
                notas_internas=resumen,
                etiquetas=etiquetas_an,
                intent=intent_an,
                intent_status=status_an,
                intent_changed=changed_an,
                resumen_intent=resumen_intent,
                canal=WEB_CANAL,
                fuente_detalle=WEB_FUENTE,
            )
        except Exception as exc:
            logger.warning("resumen / upsert (web post) falló: %s", exc)

    return ChatResponse(response=respuesta, session_id=session_id)


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Procesa el mensaje, genera respuesta con Claude y la envía de vuelta.
    Sincroniza el contacto como lead en Koala OS CRM.
    """
    try:
        # Parsear webhook — el proveedor normaliza el formato
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            # Ignorar mensajes propios o vacíos
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono} ({msg.nombre}): {msg.texto}")

            # Nombre del proveedor para registrar en wa_conversations
            proveedor_nombre = os.getenv("WHATSAPP_PROVIDER", "twilio").lower() or "twilio"

            # ─────────────────────────────────────────────────────────────
            # FASE 1: PERSISTENCIA DEL USER MESSAGE
            # Lo más importante: nunca perder un mensaje del cliente.
            # En este punto NO conocemos todavía el intent (lo calcula
            # Claude después), así que el lead arranca/sigue con
            # `intent="indefinido"` y se promueve en la fase 4.
            # ─────────────────────────────────────────────────────────────
            lead_id = None
            if _koala_cfg:
                try:
                    lead_id = await upsert_lead_from_message(
                        cfg=_koala_cfg,
                        phone=msg.telefono,
                        name=msg.nombre or None,
                        last_message=msg.texto,
                    )
                except Exception as exc:
                    logger.warning("upsert_lead (user) falló: %s", exc)

                if msg.texto != "__MULTIMEDIA__":
                    try:
                        await append_wa_message(
                            cfg=_koala_cfg,
                            phone=msg.telefono,
                            role="user",
                            content=msg.texto,
                            proveedor=proveedor_nombre,
                            provider_msg_id=msg.mensaje_id or None,
                            contacto_nombre=msg.nombre or None,
                            lead_id=lead_id,
                        )
                    except Exception as exc:
                        logger.warning("append_wa_message (user) falló: %s", exc)

            # ─────────────────────────────────────────────────────────────
            # FASE 2: MULTIMEDIA SHORT-CIRCUIT
            # ─────────────────────────────────────────────────────────────
            if msg.texto == "__MULTIMEDIA__":
                from agent.providers.twilio import ProveedorTwilio
                aviso = ProveedorTwilio.MENSAJE_SOLO_TEXTO
                try:
                    await proveedor.enviar_mensaje(msg.telefono, aviso)
                except Exception as exc:
                    logger.warning("enviar_mensaje (multimedia) falló: %s", exc)
                if _koala_cfg:
                    try:
                        await append_wa_message(
                            cfg=_koala_cfg,
                            phone=msg.telefono,
                            role="bot",
                            content=aviso,
                            proveedor=proveedor_nombre,
                            contacto_nombre=msg.nombre or None,
                            lead_id=lead_id,
                        )
                    except Exception as exc:
                        logger.warning("append_wa_message (multimedia bot) falló: %s", exc)
                logger.info(f"Multimedia recibida de {msg.telefono} — respondido con aviso de solo texto")
                continue

            # ─────────────────────────────────────────────────────────────
            # FASE 3: GENERACIÓN DE RESPUESTA + PERSISTENCIA INMEDIATA
            # El append del bot ocurre ANTES de cualquier análisis adicional
            # para garantizar que nunca se pierda la conversación, aunque
            # falle el resumen o Twilio.
            # ─────────────────────────────────────────────────────────────
            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(msg.texto, historial)

            # Memoria local del agente (SQLite)
            try:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)
            except Exception as exc:
                logger.warning("guardar_mensaje (SQLite local) falló: %s", exc)

            # 3a — Registrar la respuesta del bot ANTES de mandarla por Twilio
            # (si Twilio falla, igual queda registro en Koala OS).
            if _koala_cfg:
                try:
                    await append_wa_message(
                        cfg=_koala_cfg,
                        phone=msg.telefono,
                        role="bot",
                        content=respuesta,
                        proveedor=proveedor_nombre,
                        contacto_nombre=msg.nombre or None,
                        lead_id=lead_id,
                    )
                except Exception as exc:
                    logger.warning("append_wa_message (bot) falló: %s", exc)

            # 3b — Enviar por WhatsApp
            try:
                await proveedor.enviar_mensaje(msg.telefono, respuesta)
            except Exception as exc:
                logger.warning("enviar_mensaje (bot) falló: %s", exc)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

            # ─────────────────────────────────────────────────────────────
            # FASE 4: ANÁLISIS / RESUMEN + CLASIFICACIÓN DE INTENT
            #
            # Claude analiza el historial completo y declara:
            #   * intent (reserva / cancelacion / consulta / queja / evento)
            #   * intent_status (abierto / confirmado / perdido / cerrado)
            #   * intent_changed (¿cambió respecto al lead abierto?)
            #
            # Si cambió el intent, koala_sync cierra el lead viejo (estado
            # ganado/perdido según el status) y abre uno nuevo. Si el lead
            # llevaba mucho sin actividad (intent_timeout_min), se trata
            # como sesión nueva aunque el intent sea el mismo.
            #
            # Best-effort: si Claude tira excepción acá, no afecta a la
            # conversación ya persistida en fases anteriores.
            # ─────────────────────────────────────────────────────────────
            if _koala_cfg:
                try:
                    historial_completo = await obtener_historial(msg.telefono)
                    # Pasamos el intent del lead abierto para que Claude
                    # decida si efectivamente cambió.
                    open_lead = await koala_find_open_lead(_koala_cfg, msg.telefono)
                    intent_actual = (open_lead or {}).get("intent") or "indefinido"

                    analisis = await resumir_conversacion(
                        historial_completo,
                        msg.texto,
                        intent_actual=intent_actual,
                    )
                    analisis = analisis if isinstance(analisis, dict) else {}
                    resumen = analisis.get("resumen", "") or ""
                    etiquetas_an = analisis.get("etiquetas", []) or []
                    intent_an = analisis.get("intent", "indefinido")
                    status_an = analisis.get("intent_status", "abierto")
                    changed_an = bool(analisis.get("intent_changed", False))
                    resumen_intent = analisis.get("resumen_intent", resumen) or resumen

                    logger.info(
                        "intent_analysis: actual=%s nuevo=%s status=%s changed=%s",
                        intent_actual, intent_an, status_an, changed_an,
                    )

                    await upsert_lead_from_message(
                        cfg=_koala_cfg,
                        phone=msg.telefono,
                        name=msg.nombre or None,
                        last_message=msg.texto,
                        notas_internas=resumen,
                        etiquetas=etiquetas_an,
                        intent=intent_an,
                        intent_status=status_an,
                        intent_changed=changed_an,
                        resumen_intent=resumen_intent,
                    )
                except Exception as exc:
                    logger.warning("resumen / upsert (post) falló: %s", exc)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
