# agent/providers/base.py — Clase base para proveedores de WhatsApp
# Generado por AgentKit

"""
Define la interfaz común que todos los proveedores de WhatsApp deben implementar.
Esto permite cambiar de proveedor sin modificar el resto del código.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from fastapi import Request


@dataclass
class MensajeEntrante:
    """Mensaje normalizado — mismo formato sin importar el proveedor."""
    telefono: str       # Número del remitente
    texto: str          # Contenido del mensaje
    mensaje_id: str     # ID único del mensaje
    es_propio: bool     # True si lo envió el agente/bot (se ignora para no loopear)
    nombre: str = ""    # Nombre/alias del contacto (ProfileName en Twilio)

    # ── Coexistence (Meta) ────────────────────────────────────────────────
    # True cuando el mensaje es un `smb_message_echoes`: lo envió una PERSONA
    # desde la app WhatsApp Business. Para Koala OS cuenta como `operator`
    # (dispara el handoff: cancela la cola de IA y pausa la IA).
    es_operador: bool = False
    # phone_number_id del número que RECIBIÓ (o emitió) el mensaje. Permite
    # rutear a qué local pertenece y responder desde el número correcto.
    phone_number_id: str = ""
    # location_id resuelto a partir de phone_number_id (si hay mapa).
    location_id: str = ""


class ProveedorWhatsApp(ABC):
    """Interfaz que cada proveedor de WhatsApp debe implementar."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extrae y normaliza mensajes del payload del webhook."""
        ...

    @abstractmethod
    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía un mensaje de texto. Retorna True si fue exitoso."""
        ...

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Verificación GET del webhook (solo Meta la requiere). Retorna respuesta o None."""
        return None
