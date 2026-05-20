# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.
"""

import os
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Responde en español.")


def obtener_mensaje_error() -> str:
    """Retorna el mensaje de error configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    """Retorna el mensaje de fallback configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]

    Returns:
        La respuesta generada por Claude
    """
    # Si el mensaje es muy corto o vacío, usar fallback
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Construir mensajes para la API
    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    # Agregar el mensaje actual
    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )

        respuesta = response.content[0].text
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()


async def resumir_conversacion(historial: list[dict], mensaje_actual: str) -> str:
    """
    Genera un resumen breve de la conversación para el CRM.
    Extrae: intent, datos clave (nombre, personas, fecha, local, etc.)

    Returns:
        Resumen en 1-3 líneas para el campo ultimo_mensaje del CRM.
    """
    mensajes_texto = ""
    for msg in historial[-10:]:  # Últimos 10 mensajes para no exceder contexto
        rol = "Cliente" if msg["role"] == "user" else "Agente"
        mensajes_texto += f"{rol}: {msg['content']}\n"
    mensajes_texto += f"Cliente: {mensaje_actual}\n"

    try:
        response = await client.messages.create(
            model="claude-haiku-3-5-20241022",
            max_tokens=200,
            system=(
                "Sos un asistente que resume conversaciones de WhatsApp para un CRM de restaurantes. "
                "Generá un resumen MUY breve (máximo 2-3 líneas) con los datos clave extraídos. "
                "Formato: 'Intent: [reserva/consulta/evento/otro] · [datos clave separados por ·]'. "
                "Ejemplos:\n"
                "- 'Intent: reserva · 4 personas · sábado 21:00 · Palermo · cumpleaños'\n"
                "- 'Intent: consulta · preguntas sobre menú sin TACC'\n"
                "- 'Intent: evento corporativo · empresa TechCo · 30 personas · diciembre'\n"
                "Si no hay datos concretos, resumí el tema general de la conversación."
            ),
            messages=[{"role": "user", "content": mensajes_texto}]
        )
        resumen = response.content[0].text.strip()
        logger.info(f"Resumen CRM generado ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return resumen
    except Exception as e:
        logger.error(f"Error generando resumen CRM: {e}")
        return mensaje_actual[:500]
