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

import asyncio
from datetime import datetime, timedelta, timezone

from agent.brain import generar_respuesta, resumir_conversacion
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.koala_sync import (
    KoalaSyncConfig,
    upsert_lead_from_message,
    append_wa_message,
    find_open_lead as koala_find_open_lead,
    get_location_ai_settings,
    resolve_location_from_number,
    resolve_timeout_seconds,
    fetch_conversation,
    enqueue_reply,
    cancel_pending_replies,
    claim_due_replies,
    complete_reply,
)

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
WHATSAPP_PROVIDER_NAME = (os.getenv("WHATSAPP_PROVIDER", "twilio").lower() or "twilio")
PORT = int(os.getenv("PORT", 8000))

# Modelo híbrido (Coexistence): cada cuánto el worker busca respuestas de IA
# vencidas en la cola wa_pending_replies.
WORKER_POLL_SECONDS = max(5, int(os.getenv("KOALA_WORKER_POLL_SECONDS", "20")))
# Modo de IA por defecto cuando el local no tiene fila en wa_location_settings.
# 'always' preserva el comportamiento histórico del bot (responde al instante).
DEFAULT_AI_MODE = (os.getenv("KOALA_DEFAULT_AI_MODE", "always").lower() or "always")

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

    worker_task: Optional["asyncio.Task"] = None
    if _koala_cfg:
        worker_task = asyncio.create_task(_timeout_worker())
        logger.info(
            "Worker de IA de respaldo iniciado (poll=%ss, default_mode=%s)",
            WORKER_POLL_SECONDS, DEFAULT_AI_MODE,
        )
    else:
        logger.info("Worker de IA de respaldo NO iniciado (Koala OS no configurado)")

    try:
        yield
    finally:
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass


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


# ═════════════════════════════════════════════════════════════════
# Modelo híbrido (Coexistence) — resolución de modo + worker de IA de
# respaldo. Ver migración 114_wa_hybrid_ai_fallback.sql en Koala OS.
# ═════════════════════════════════════════════════════════════════

def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _send_kwargs(phone_number_id: str) -> dict:
    """Kwargs de envío para rutear al número correcto (solo Meta)."""
    if phone_number_id and WHATSAPP_PROVIDER_NAME == "meta":
        return {"from_phone_number_id": phone_number_id}
    return {}


async def _resolve_ai_mode(conv_mode: Optional[str], location_id: Optional[str]) -> str:
    """Modo efectivo: override de conversación > config del local > default.

    Valores: 'off' | 'hybrid' | 'always'.
    """
    mode = (conv_mode or "inherit").lower()
    if mode in ("off", "hybrid", "always"):
        return mode
    settings = await get_location_ai_settings(_koala_cfg, location_id) if _koala_cfg else None
    if settings and settings.get("ai_mode") in ("off", "hybrid", "always"):
        return settings["ai_mode"]
    return DEFAULT_AI_MODE


def _history_and_last_user(mensajes: list) -> tuple[list[dict], str, Optional[datetime]]:
    """A partir del JSONB `mensajes` arma (historial_para_claude, ultimo_texto_user, ts).

    El historial se devuelve SIN el último mensaje del cliente, porque
    `generar_respuesta` lo recibe aparte como `mensaje`.
    """
    hist: list[dict] = []
    last_user_text = ""
    last_user_ts: Optional[datetime] = None
    for m in mensajes or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content or content == "__MULTIMEDIA__":
            continue
        if role == "user":
            hist.append({"role": "user", "content": content})
            last_user_text = content
            last_user_ts = _parse_iso(m.get("created_at"))
        elif role in ("bot", "operator"):
            hist.append({"role": "assistant", "content": content})
    if hist and hist[-1]["role"] == "user":
        hist = hist[:-1]
    return hist, last_user_text, last_user_ts


async def _handle_due_reply(pid: str, item: dict) -> None:
    """Procesa UNA respuesta vencida de la cola: genera, envía y persiste."""
    conv_id = item.get("conversation_id")
    try:
        conv = await fetch_conversation(_koala_cfg, conv_id)
        if not conv:
            await complete_reply(_koala_cfg, pid, "cancelled", "conversación inexistente")
            return

        mensajes = conv.get("mensajes") or []

        # Guard: ¿ya respondió un humano o el bot después del cliente?
        if mensajes:
            last_role = (mensajes[-1].get("role") or "")
            if last_role in ("operator", "bot", "system"):
                await complete_reply(_koala_cfg, pid, "cancelled", f"ya respondió {last_role}")
                return

        # Guard: IA apagada en la conversación.
        if (conv.get("ai_mode") or "") == "off":
            await complete_reply(_koala_cfg, pid, "cancelled", "IA off en la conversación")
            return

        # Guard: handoff vigente (un humano tomó la charla hace poco).
        paused = _parse_iso(conv.get("ai_paused_until"))
        if paused and paused > datetime.now(timezone.utc):
            await complete_reply(_koala_cfg, pid, "cancelled", "IA en pausa (handoff)")
            return

        hist, last_user_text, last_user_ts = _history_and_last_user(mensajes)
        if not last_user_text:
            await complete_reply(_koala_cfg, pid, "cancelled", "sin mensaje de cliente")
            return

        # Guard: ventana de 24 h de Meta (fuera de ventana requiere plantilla).
        if last_user_ts and (datetime.now(timezone.utc) - last_user_ts) > timedelta(hours=24):
            await complete_reply(_koala_cfg, pid, "cancelled", "fuera de ventana 24h")
            return

        respuesta = await generar_respuesta(last_user_text, hist)

        phone = conv.get("telefono") or item.get("telefono")
        location_id = conv.get("location_id") or item.get("location_id")
        metadata = conv.get("metadata") or {}
        pnid = str(metadata.get("phone_number_id") or "") if isinstance(metadata, dict) else ""

        # Persistir el bot ANTES de enviar (no perder el registro).
        await append_wa_message(
            cfg=_koala_cfg, phone=phone, role="bot", content=respuesta,
            proveedor=WHATSAPP_PROVIDER_NAME, location_id=location_id,
        )
        ok = await proveedor.enviar_mensaje(phone, respuesta, **_send_kwargs(pnid))
        if not ok:
            await complete_reply(_koala_cfg, pid, "failed", "proveedor no pudo enviar")
            return

        await complete_reply(_koala_cfg, pid, "done")
        logger.info("IA de respaldo respondió conv=%s (len=%d)", conv_id, len(respuesta))
    except Exception as exc:
        logger.warning("_handle_due_reply conv=%s falló: %s", conv_id, exc)
        try:
            await complete_reply(_koala_cfg, pid, "failed", str(exc)[:200])
        except Exception:
            pass


async def _process_due_replies() -> None:
    due = await claim_due_replies(_koala_cfg, limit=10)
    for item in due:
        pid = item.get("id")
        if pid and item.get("conversation_id"):
            await _handle_due_reply(pid, item)


async def _timeout_worker() -> None:
    """Loop en background: cada WORKER_POLL_SECONDS procesa la cola vencida."""
    while True:
        try:
            await _process_due_replies()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("timeout_worker: %s", exc)
        await asyncio.sleep(WORKER_POLL_SECONDS)


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
            # Ignorar mensajes propios (del bot/API) o vacíos
            if msg.es_propio or not msg.texto:
                continue

            # Nombre del proveedor para registrar en wa_conversations
            proveedor_nombre = WHATSAPP_PROVIDER_NAME
            location_id = (getattr(msg, "location_id", "") or None)
            phone_number_id = (getattr(msg, "phone_number_id", "") or "")

            # Si el mapa de env (KOALA_NUMBER_MAP) no resolvió el local, lo
            # buscamos en Koala OS por phone_number_id (líneas dadas de alta
            # desde Canales → WhatsApp). Así no hace falta tocar env vars.
            if _koala_cfg and not location_id and phone_number_id:
                try:
                    location_id = await resolve_location_from_number(_koala_cfg, phone_number_id)
                except Exception as exc:
                    logger.warning("resolve_location_from_number falló: %s", exc)

            # ─────────────────────────────────────────────────────────────
            # COEXISTENCE: echo de la app = respondió un HUMANO del local.
            # Se registra como `operator`, lo que dispara el handoff en la
            # RPC wa_append_message (cancela la cola de IA y pausa la IA).
            # No generamos respuesta del bot.
            # ─────────────────────────────────────────────────────────────
            if getattr(msg, "es_operador", False):
                if _koala_cfg and msg.texto != "__MULTIMEDIA__":
                    try:
                        await append_wa_message(
                            cfg=_koala_cfg,
                            phone=msg.telefono,
                            role="operator",
                            content=msg.texto,
                            proveedor=proveedor_nombre,
                            provider_msg_id=msg.mensaje_id or None,
                            contacto_nombre=msg.nombre or None,
                            location_id=location_id,
                        )
                        logger.info("Echo de la app (operator) → handoff: %s", msg.telefono)
                    except Exception as exc:
                        logger.warning("append_wa_message (operator echo) falló: %s", exc)
                continue

            logger.info(f"Mensaje de {msg.telefono} ({msg.nombre}): {msg.texto}")

            # ─────────────────────────────────────────────────────────────
            # FASE 1: PERSISTENCIA DEL USER MESSAGE
            # Lo más importante: nunca perder un mensaje del cliente.
            # En este punto NO conocemos todavía el intent (lo calcula
            # Claude después), así que el lead arranca/sigue con
            # `intent="indefinido"` y se promueve en la fase 4.
            # ─────────────────────────────────────────────────────────────
            lead_id = None
            conv_id = None
            if _koala_cfg:
                try:
                    lead_id = await upsert_lead_from_message(
                        cfg=_koala_cfg,
                        phone=msg.telefono,
                        name=msg.nombre or None,
                        last_message=msg.texto,
                        location_id=location_id,
                    )
                except Exception as exc:
                    logger.warning("upsert_lead (user) falló: %s", exc)

                if msg.texto != "__MULTIMEDIA__":
                    try:
                        conv_id = await append_wa_message(
                            cfg=_koala_cfg,
                            phone=msg.telefono,
                            role="user",
                            content=msg.texto,
                            proveedor=proveedor_nombre,
                            provider_msg_id=msg.mensaje_id or None,
                            contacto_nombre=msg.nombre or None,
                            lead_id=lead_id,
                            location_id=location_id,
                            metadata=(
                                {"phone_number_id": phone_number_id}
                                if phone_number_id else None
                            ),
                        )
                    except Exception as exc:
                        logger.warning("append_wa_message (user) falló: %s", exc)

            # ─────────────────────────────────────────────────────────────
            # RESOLUCIÓN DEL MODO DE IA (off / hybrid / always)
            #   * off    → solo humanos, no respondemos.
            #   * hybrid → humano primero; encolamos respuesta diferida.
            #   * always → comportamiento clásico del bot (responde ya).
            # ─────────────────────────────────────────────────────────────
            ai_mode = DEFAULT_AI_MODE
            if _koala_cfg and conv_id:
                try:
                    conv = await fetch_conversation(_koala_cfg, conv_id)
                    conv_mode = (conv or {}).get("ai_mode")
                    ai_mode = await _resolve_ai_mode(conv_mode, location_id)
                except Exception as exc:
                    logger.warning("resolver ai_mode falló: %s", exc)

            if ai_mode == "off":
                logger.info("ai_mode=off — no se responde (solo humanos): %s", msg.telefono)
                continue

            if ai_mode == "hybrid":
                if _koala_cfg and conv_id and msg.texto != "__MULTIMEDIA__":
                    try:
                        settings = await get_location_ai_settings(_koala_cfg, location_id) or {}
                        open_lead = await koala_find_open_lead(_koala_cfg, msg.telefono)
                        intent_actual = (open_lead or {}).get("intent") or "indefinido"
                        timeout_s = resolve_timeout_seconds(settings, intent_actual)
                        respond_after = (
                            datetime.now(timezone.utc) + timedelta(seconds=timeout_s)
                        ).isoformat()
                        await enqueue_reply(
                            cfg=_koala_cfg,
                            conversation_id=conv_id,
                            respond_after_iso=respond_after,
                            intent=intent_actual,
                            trigger_message_id=msg.mensaje_id or None,
                        )
                        logger.info(
                            "ai_mode=hybrid — IA encolada en %ss conv=%s intent=%s",
                            timeout_s, conv_id, intent_actual,
                        )
                    except Exception as exc:
                        logger.warning("enqueue_reply (hybrid) falló: %s", exc)
                # En híbrido no respondemos ahora: espera al humano o al timeout.
                # (La multimedia también espera; el worker avisará solo-texto si
                #  hiciera falta al generar.)
                continue

            # ─────────────────────────────────────────────────────────────
            # FASE 2: MULTIMEDIA SHORT-CIRCUIT (modo always)
            # ─────────────────────────────────────────────────────────────
            if msg.texto == "__MULTIMEDIA__":
                from agent.providers.twilio import ProveedorTwilio
                aviso = ProveedorTwilio.MENSAJE_SOLO_TEXTO
                try:
                    await proveedor.enviar_mensaje(
                        msg.telefono, aviso, **_send_kwargs(phone_number_id)
                    )
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
                            location_id=location_id,
                        )
                    except Exception as exc:
                        logger.warning("append_wa_message (multimedia bot) falló: %s", exc)
                logger.info(f"Multimedia recibida de {msg.telefono} — respondido con aviso de solo texto")
                continue

            # ─────────────────────────────────────────────────────────────
            # FASE 3: GENERACIÓN DE RESPUESTA + PERSISTENCIA INMEDIATA (always)
            # El append del bot ocurre ANTES de cualquier análisis adicional
            # para garantizar que nunca se pierda la conversación, aunque
            # falle el resumen o el envío.
            # ─────────────────────────────────────────────────────────────
            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(msg.texto, historial)

            # Memoria local del agente (SQLite)
            try:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)
            except Exception as exc:
                logger.warning("guardar_mensaje (SQLite local) falló: %s", exc)

            # 3a — Registrar la respuesta del bot ANTES de mandarla
            # (si el envío falla, igual queda registro en Koala OS).
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
                        location_id=location_id,
                    )
                except Exception as exc:
                    logger.warning("append_wa_message (bot) falló: %s", exc)

            # 3b — Enviar por WhatsApp
            try:
                await proveedor.enviar_mensaje(
                    msg.telefono, respuesta, **_send_kwargs(phone_number_id)
                )
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
                        location_id=location_id,
                    )
                except Exception as exc:
                    logger.warning("resumen / upsert (post) falló: %s", exc)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
