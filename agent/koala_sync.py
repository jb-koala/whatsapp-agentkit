# agent/koala_sync.py — Sincronización de leads con Koala OS (Supabase)
# Generado por AgentKit

"""
Sincronización de leads con la tabla crm_leads de Supabase.
Cada contacto por WhatsApp entra como lead al CRM automáticamente.
Usa upsert por teléfono para evitar duplicados.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


# Mapeo de locales disponibles
LOCALES_KOALA = {
    "pilar": "loc-pilar",
    "palermo": "loc-palermo",
    "puerto madero": "loc-madero",
    "madero": "loc-madero",
}


def detectar_local(texto: str) -> Optional[str]:
    """Detecta el local mencionado en el mensaje del cliente.

    Retorna el location_id si encuentra una coincidencia, None si no.
    """
    texto_lower = texto.lower()
    for keyword, loc_id in LOCALES_KOALA.items():
        if keyword in texto_lower:
            return loc_id
    return None


@dataclass
class KoalaSyncConfig:
    supabase_url: str
    service_role_key: str
    default_location_id: str
    default_channel: str = "whatsapp"
    lead_source: str = "Agente IA WhatsApp"

    @classmethod
    def from_env(cls) -> Optional["KoalaSyncConfig"]:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        loc = os.getenv("KOALA_LOCATION_ID", "").strip()
        if not (url and key):
            return None
        return cls(
            supabase_url=url.rstrip("/"),
            service_role_key=key,
            default_location_id=loc or "a confirmar",
            default_channel=os.getenv("KOALA_DEFAULT_CHANNEL", "whatsapp"),
            lead_source=os.getenv("KOALA_LEAD_SOURCE", "Agente IA WhatsApp"),
        )


async def upsert_lead_from_message(
    cfg: KoalaSyncConfig,
    phone: str,
    name: Optional[str],
    last_message: str,
    notas_internas: str = "",
    conversation_id: str = "",
    location_id: Optional[str] = None,
    etiquetas: Optional[list[str]] = None,
) -> None:
    """Crea o actualiza un lead en crm_leads a partir de un mensaje entrante.

    Busca por teléfono antes de insertar para evitar duplicados.
    location_id se detecta del mensaje o usa el default.
    """
    phone = (phone or "").strip()
    if not phone:
        return

    # Determinar local: el que se pasa explícitamente, o detectar del mensaje, o default
    loc = location_id or detectar_local(last_message) or cfg.default_location_id

    endpoint = f"{cfg.supabase_url}/rest/v1/crm_leads"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    payload = {
        "location_id": loc,
        "nombre": (name or phone)[:120],
        "contacto": phone,
        "canal": cfg.default_channel,
        "estado": "nuevo",
        "ultimo_mensaje": last_message[:500],
        "telefono": phone,
        "fuente_detalle": cfg.lead_source,
        "notas_internas": notas_internas[:500],
    }
    if etiquetas:
        payload["etiquetas"] = etiquetas
    # Solo incluir id_conversacion_canal si es un ID real (no vacío)
    # para evitar conflictos con el unique index parcial
    if conversation_id and conversation_id.strip():
        payload["id_conversacion_canal"] = conversation_id.strip()

    async with httpx.AsyncClient(timeout=10) as client:
        # 1) Buscar si ya existe un lead con ese teléfono
        try:
            r = await client.get(
                endpoint,
                headers=headers,
                params={"select": "id", "telefono": f"eq.{phone}", "limit": 1},
            )
            r.raise_for_status()
            existing = r.json()
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo buscando lead por teléfono: %s", exc)
            return

        # 2) Si existe: actualizar ultimo_mensaje y notas
        if existing:
            lead_id = existing[0]["id"]
            patch_data = {
                "ultimo_mensaje": last_message[:500],
                "updated_at": "now()",
            }
            # Si el cliente mencionó un local, actualizar location_id
            detected_loc = detectar_local(last_message)
            if detected_loc:
                patch_data["location_id"] = detected_loc
            if notas_internas:
                patch_data["notas_internas"] = notas_internas[:500]
            if etiquetas:
                patch_data["etiquetas"] = etiquetas
            try:
                upd = await client.patch(
                    f"{endpoint}?id=eq.{lead_id}",
                    headers=headers,
                    json=patch_data,
                )
                upd.raise_for_status()
                log.info("koala_sync: lead %s actualizado", lead_id)
            except httpx.HTTPError as exc:
                log.warning("koala_sync: fallo actualizando lead %s: %s", lead_id, exc)
            return

        # 3) No existe: insertar lead nuevo
        try:
            ins = await client.post(endpoint, headers=headers, json=payload)
            ins.raise_for_status()
            new_id = ins.json()[0]["id"] if ins.json() else "?"
            log.info("koala_sync: lead nuevo %s creado", new_id)
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo creando lead: %s", exc)
