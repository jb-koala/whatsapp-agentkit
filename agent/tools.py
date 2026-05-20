# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas específicas del negocio Koala.
Extienden las capacidades del agente más allá de responder texto.
"""

import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular según hora actual y horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# Herramientas de Reservaciones
# ════════════════════════════════════════════════════════════

def registrar_reserva(telefono: str, nombre: str, personas: int,
                      fecha: str, hora: str, local: str,
                      ocasion: str = "") -> dict:
    """
    Registra una solicitud de reserva.
    En producción esto se conectaría con el sistema de reservas real.
    """
    reserva = {
        "telefono": telefono,
        "nombre": nombre,
        "personas": personas,
        "fecha": fecha,
        "hora": hora,
        "local": local,
        "ocasion": ocasion,
        "estado": "pendiente",
        "timestamp": datetime.now().isoformat(),
    }
    logger.info(f"Nueva reserva registrada: {reserva}")
    return reserva


# ════════════════════════════════════════════════════════════
# Herramientas de Leads / Ventas
# ════════════════════════════════════════════════════════════

def registrar_lead(telefono: str, nombre: str, interes: str,
                   tipo: str = "general") -> dict:
    """
    Registra un lead interesado en eventos, catering, etc.
    """
    lead = {
        "telefono": telefono,
        "nombre": nombre,
        "interes": interes,
        "tipo": tipo,  # "evento_corporativo", "catering", "celebracion", "general"
        "estado": "nuevo",
        "timestamp": datetime.now().isoformat(),
    }
    logger.info(f"Nuevo lead registrado: {lead}")
    return lead


def escalar_a_equipo(telefono: str, contexto: str, area: str = "comercial") -> dict:
    """
    Marca una conversación para ser atendida por el equipo humano.
    """
    escalamiento = {
        "telefono": telefono,
        "contexto": contexto,
        "area": area,
        "timestamp": datetime.now().isoformat(),
    }
    logger.info(f"Escalamiento a {area}: {escalamiento}")
    return escalamiento
