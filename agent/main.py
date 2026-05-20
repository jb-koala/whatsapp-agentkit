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

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.koala_sync import KoalaSyncConfig, upsert_lead_from_message

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

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Obtener historial ANTES de guardar el mensaje actual
            historial = await obtener_historial(msg.telefono)

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Sincronizar lead en Koala OS CRM (Supabase)
            if _koala_cfg:
                await upsert_lead_from_message(
                    cfg=_koala_cfg,
                    phone=msg.telefono,
                    name=None,  # Se completa cuando el cliente da su nombre
                    last_message=msg.texto,
                    notas_internas=f"Último mensaje procesado por Koala-OS",
                    conversation_id=msg.mensaje_id,
                )

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
