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
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta, resumir_conversacion
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.koala_sync import KoalaSyncConfig, upsert_lead_from_message, append_wa_message

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

# CORS para panel Koala OS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://koala-os.netlify.app",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
            # Cualquier fallo posterior no debe romper este registro.
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
            # FASE 4: ANÁLISIS / RESUMEN (best-effort, no bloquea)
            # Si Claude tira excepción acá, no afecta a la conversación
            # ya persistida en fases anteriores.
            # ─────────────────────────────────────────────────────────────
            if _koala_cfg:
                try:
                    historial_completo = await obtener_historial(msg.telefono)
                    analisis = await resumir_conversacion(historial_completo, msg.texto)
                    resumen = (analisis or {}).get("resumen", "") if isinstance(analisis, dict) else ""
                    etiquetas_an = (analisis or {}).get("etiquetas", []) if isinstance(analisis, dict) else []
                    await upsert_lead_from_message(
                        cfg=_koala_cfg,
                        phone=msg.telefono,
                        name=msg.nombre or None,
                        last_message=msg.texto,
                        notas_internas=resumen,
                        etiquetas=etiquetas_an,
                    )
                except Exception as exc:
                    logger.warning("resumen / upsert (post) falló: %s", exc)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
