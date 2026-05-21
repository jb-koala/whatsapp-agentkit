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


# Valores válidos para `intent` y `intent_status`.
# Se mantienen en sintonía con la documentación de Koala OS
# (migración 41_crm_leads_intent.sql).
_INTENTS_VALIDOS = {
    "indefinido",
    "reserva",
    "cancelacion",
    "consulta",
    "queja",
    "evento",
    "otro",
}
_STATUS_VALIDOS = {"abierto", "confirmado", "perdido", "cerrado"}


async def resumir_conversacion(
    historial: list[dict],
    mensaje_actual: str,
    intent_actual: str | None = None,
) -> dict:
    """Resumen + clasificación de intent de la conversación.

    Devuelve un dict con:
      * resumen (str)                — 2-3 líneas para `crm_leads.notas_internas`.
      * etiquetas (list[str])        — texto libre, usado como tags.
      * intent (str)                 — categoría operativa (ver `_INTENTS_VALIDOS`).
      * intent_status (str)          — estado del intent (ver `_STATUS_VALIDOS`).
      * intent_changed (bool)        — opinión del LLM: ¿cambió respecto al
                                        intent vigente del lead?
      * resumen_intent (str)         — resumen específico de ESTE intent
                                        (sin contaminar con anteriores).

    El parámetro `intent_actual` indica al LLM cuál era el intent del lead
    abierto, así puede comparar y decir si cambió.
    """
    mensajes_texto = ""
    for msg in historial[-12:]:
        rol = "Cliente" if msg["role"] == "user" else "Agente"
        mensajes_texto += f"{rol}: {msg['content']}\n"
    mensajes_texto += f"Cliente: {mensaje_actual}\n"

    intent_actual_norm = (intent_actual or "indefinido").lower().strip()

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "Sos un asistente que analiza conversaciones de WhatsApp para un CRM de restaurantes. "
                "Respondé SOLO con un JSON válido, sin texto adicional.\n\n"
                "Formato exacto:\n"
                '{"resumen": "...", "etiquetas": [...], "intent": "...", '
                '"intent_status": "...", "intent_changed": false, "resumen_intent": "..."}\n\n'
                "Reglas para `resumen` (resumen general de toda la conv):\n"
                "- 2-3 líneas con los datos clave separados por ·\n"
                "- Incluí: tipo de pedido, personas, fecha, hora, local, nombre si lo dio\n\n"
                "Reglas para `etiquetas` (texto libre, sin tildes ni mayúsculas):\n"
                "- 1-3 tags. Ej: reserva, cancelacion, consulta, evento, queja, cumpleanos\n\n"
                "Reglas para `intent` (ELEGÍ UNA SOLA de esta lista cerrada):\n"
                "- indefinido: saludo, hola, hi, sin información todavía\n"
                "- reserva: quiere reservar mesa o ya está reservando\n"
                "- cancelacion: quiere cancelar, mover o modificar una reserva existente\n"
                "- consulta: pregunta puntual (horarios, menú, ubicación, opciones)\n"
                "- queja: reclamo, mala experiencia, comida fría, mal trato\n"
                "- evento: evento corporativo, catering, fiesta privada, salón cerrado\n"
                "- otro: nada de lo anterior\n\n"
                "Reglas para `intent_status`:\n"
                "- abierto: el intent está en curso (todavía falta confirmar o info)\n"
                "- confirmado: ya quedó cerrado con éxito (reserva confirmada, evento agendado)\n"
                "- perdido: el cliente se arrepintió, no se concretó, canceló sin reemplazo\n"
                "- cerrado: la consulta terminó sin acción pendiente\n\n"
                "Reglas para `intent_changed`:\n"
                "- true: el intent NUEVO de esta conversación es DISTINTO al que ya tenía "
                "(intent_actual). Por ejemplo: pasó de 'reserva' a 'cancelacion', o de "
                "'indefinido' a 'reserva'.\n"
                "- false: sigue siendo el mismo intent que ya tenía.\n\n"
                "Reglas para `resumen_intent`:\n"
                "- Resumen específico SOLO del intent actual (no del histórico).\n"
                "- Si intent_changed=true, este resumen describe el intent NUEVO solamente.\n\n"
                f"intent_actual del lead abierto: '{intent_actual_norm}'\n"
            ),
            messages=[{"role": "user", "content": mensajes_texto}],
        )
        import json
        import re
        texto = response.content[0].text.strip()
        logger.info(
            f"Resumen CRM generado ({response.usage.input_tokens} in / "
            f"{response.usage.output_tokens} out)"
        )

        json_match = re.search(r"\{.*\}", texto, re.DOTALL)
        if not json_match:
            logger.warning(f"Haiku no devolvió JSON válido: {texto[:200]}")
            return _resumen_fallback(mensaje_actual, intent_actual_norm)

        try:
            raw = json.loads(json_match.group())
        except json.JSONDecodeError as exc:
            logger.warning(f"JSON inválido del resumen: {exc} — {texto[:200]}")
            return _resumen_fallback(mensaje_actual, intent_actual_norm)

        intent = (raw.get("intent") or "indefinido").lower().strip()
        if intent not in _INTENTS_VALIDOS:
            intent = "otro"

        status = (raw.get("intent_status") or "abierto").lower().strip()
        if status not in _STATUS_VALIDOS:
            status = "abierto"

        changed = bool(raw.get("intent_changed", False))
        # Sanitizar: si el LLM dice changed=true pero el intent es el mismo,
        # forzar false. Y viceversa.
        if intent == intent_actual_norm:
            changed = False
        elif intent != intent_actual_norm and intent_actual_norm not in ("", "indefinido"):
            # Promoción de 'indefinido' a un intent definido NO se considera
            # "cambio" — es el primer intent claro. Cualquier otro cambio sí.
            changed = True
        else:
            # intent_actual era indefinido y ahora hay uno definido: NO crear
            # un lead nuevo, simplemente actualizar el existente.
            changed = False

        etiquetas = raw.get("etiquetas") or []
        if not isinstance(etiquetas, list):
            etiquetas = []
        etiquetas = [str(t).strip().lower() for t in etiquetas if str(t).strip()]

        resumen = str(raw.get("resumen") or mensaje_actual)[:500]
        resumen_intent = str(raw.get("resumen_intent") or resumen)[:500]

        return {
            "resumen": resumen,
            "etiquetas": etiquetas,
            "intent": intent,
            "intent_status": status,
            "intent_changed": changed,
            "resumen_intent": resumen_intent,
        }

    except Exception as e:
        logger.error(f"Error generando resumen CRM: {e}")
        return _resumen_fallback(mensaje_actual, intent_actual_norm)


def _resumen_fallback(mensaje_actual: str, intent_actual: str) -> dict:
    """Resumen mínimo cuando Claude falla o devuelve algo inválido."""
    return {
        "resumen": mensaje_actual[:500],
        "etiquetas": [],
        "intent": intent_actual if intent_actual in _INTENTS_VALIDOS else "indefinido",
        "intent_status": "abierto",
        "intent_changed": False,
        "resumen_intent": mensaje_actual[:500],
    }
