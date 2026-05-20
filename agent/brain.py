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


async def resumir_conversacion(historial: list[dict], mensaje_actual: str) -> dict:
    """
    Genera un resumen breve de la conversación y etiquetas para el CRM.

    Returns:
        Dict con "resumen" (str) y "etiquetas" (list[str]).
    """
    mensajes_texto = ""
    for msg in historial[-10:]:  # Últimos 10 mensajes para no exceder contexto
        rol = "Cliente" if msg["role"] == "user" else "Agente"
        mensajes_texto += f"{rol}: {msg['content']}\n"
    mensajes_texto += f"Cliente: {mensaje_actual}\n"

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=(
                "Sos un asistente que analiza conversaciones de WhatsApp para un CRM de restaurantes. "
                "Respondé SOLO con un JSON válido, sin texto adicional.\n\n"
                "Formato:\n"
                '{"resumen": "...", "etiquetas": [...]}\n\n'
                "Reglas para el resumen:\n"
                "- Máximo 2-3 líneas con datos clave separados por ·\n"
                "- Incluí: intent, personas, fecha, hora, local, nombre si lo dio\n"
                "- Ejemplo: \"Reserva · 4 personas · sábado 21:00 · Palermo · cumpleaños\"\n\n"
                "Reglas para las etiquetas:\n"
                "- SOLO usá estas etiquetas válidas: reserva, consulta, evento\n"
                "- reserva: el cliente quiere reservar mesa\n"
                "- consulta: pregunta sobre menú, horarios, locales, precios, opciones\n"
                "- evento: evento corporativo, catering, cumpleaños, celebración privada\n"
                "- Podés asignar más de una si aplica (ej: consulta + reserva)\n"
                "- Si no encaja en ninguna, dejá el array vacío []\n\n"
                "Ejemplos:\n"
                '{"resumen": "Reserva · 4 personas · sábado 21:00 · Palermo", "etiquetas": ["reserva"]}\n'
                '{"resumen": "Consulta sobre opciones sin TACC y precios de brunch", "etiquetas": ["consulta"]}\n'
                '{"resumen": "Evento corporativo · empresa TechCo · 30 personas · diciembre · Pilar", "etiquetas": ["evento"]}\n'
                '{"resumen": "Reserva para cumpleaños · 12 personas · consulta menú cerrado", "etiquetas": ["reserva", "evento"]}'
            ),
            messages=[{"role": "user", "content": mensajes_texto}]
        )
        import json
        import re
        texto = response.content[0].text.strip()
        logger.info(f"Resumen CRM generado ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")

        # Extraer JSON del texto (Haiku a veces agrega texto antes/después)
        json_match = re.search(r'\{.*\}', texto, re.DOTALL)
        if json_match:
            resultado = json.loads(json_match.group())
            return {
                "resumen": resultado.get("resumen", mensaje_actual[:500]),
                "etiquetas": resultado.get("etiquetas", []),
            }
        else:
            logger.warning(f"Haiku no devolvió JSON válido: {texto[:200]}")
            return {"resumen": texto[:500], "etiquetas": []}
    except Exception as e:
        logger.error(f"Error generando resumen CRM: {e}")
        return {"resumen": mensaje_actual[:500], "etiquetas": []}
