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
    location_id: Optional[str] = None,
    etiquetas: Optional[list[str]] = None,
) -> Optional[str]:
    """Crea o actualiza un lead en crm_leads a partir de un mensaje entrante.

    Busca por teléfono antes de insertar para evitar duplicados.
    location_id se detecta del mensaje o usa el default.
    Retorna el id del lead (UUID como str) o None si falló.
    """
    phone = (phone or "").strip()
    if not phone:
        return None

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

    async with httpx.AsyncClient(timeout=10) as client:
        # 1) Buscar si ya existe un lead con ese teléfono o contacto
        # Usamos `or=` porque hay leads viejos con contacto seteado pero telefono NULL.
        try:
            r = await client.get(
                endpoint,
                headers=headers,
                params={
                    "select": "id,telefono",
                    "or": f"(telefono.eq.{phone},contacto.eq.{phone})",
                    "limit": 1,
                },
            )
            r.raise_for_status()
            existing = r.json()
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo buscando lead por teléfono: %s", exc)
            return None

        # 2) Si existe: actualizar ultimo_mensaje y notas
        if existing:
            lead_id = existing[0]["id"]
            existing_phone = existing[0].get("telefono")
            patch_data = {
                "ultimo_mensaje": last_message[:500],
                "updated_at": "now()",
            }
            # Si el lead viejo no tenía telefono seteado, lo completamos
            # para que el GET por telefono lo encuentre en el próximo request.
            if not existing_phone:
                patch_data["telefono"] = phone
            # Actualizar nombre si el lead no tenía uno propio
            if name:
                patch_data["nombre"] = name[:120]
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
                return lead_id
            except httpx.HTTPError as exc:
                log.warning("koala_sync: fallo actualizando lead %s: %s", lead_id, exc)
                return lead_id

        # 3) No existe: insertar lead nuevo
        try:
            ins = await client.post(endpoint, headers=headers, json=payload)
            ins.raise_for_status()
            body = ins.json()
            new_id = body[0]["id"] if body else None
            log.info("koala_sync: lead nuevo %s creado", new_id or "?")
            return new_id
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                # 409 Conflict: el lead existe pero no lo encontró el GET
                # Fallback: buscar más amplio y hacer PATCH
                log.info("koala_sync: 409 en INSERT, intentando PATCH por contacto")
                try:
                    patch_data = {
                        "ultimo_mensaje": last_message[:500],
                        "updated_at": "now()",
                        "nombre": (name or phone)[:120],
                        "telefono": phone,  # asegurar que el GET por telefono lo encuentre luego
                    }
                    if notas_internas:
                        patch_data["notas_internas"] = notas_internas[:500]
                    if etiquetas:
                        patch_data["etiquetas"] = etiquetas
                    detected_loc = detectar_local(last_message)
                    if detected_loc:
                        patch_data["location_id"] = detected_loc

                    upd = await client.patch(
                        f"{endpoint}?contacto=eq.{phone}",
                        headers=headers,
                        json=patch_data,
                    )
                    upd.raise_for_status()
                    upd_body = upd.json() if upd.content else []
                    fallback_id = upd_body[0]["id"] if upd_body else None
                    log.info("koala_sync: lead actualizado via fallback PATCH")
                    return fallback_id
                except httpx.HTTPError as patch_exc:
                    log.warning("koala_sync: fallback PATCH también falló: %s", patch_exc)
                    return None
            else:
                log.warning("koala_sync: fallo creando lead: %s", exc)
                return None
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo creando lead: %s", exc)
            return None


async def append_wa_message(
    cfg: KoalaSyncConfig,
    phone: str,
    role: str,
    content: str,
    location_id: Optional[str] = None,
    proveedor: str = "twilio",
    provider_msg_id: Optional[str] = None,
    contacto_nombre: Optional[str] = None,
    lead_id: Optional[str] = None,
    msg_type: str = "text",
) -> Optional[str]:
    """Registra un mensaje en wa_conversations vía la RPC wa_append_message.

    role: 'user' (incoming) | 'bot' | 'operator' | 'system' (outgoing).
    Retorna el id de la conversación, o None si falló.
    """
    phone = (phone or "").strip()
    if not phone or not content:
        return None
    if role not in ("user", "bot", "operator", "system"):
        log.warning("append_wa_message: role inválido %s", role)
        return None

    loc = location_id or detectar_local(content) or cfg.default_location_id

    log.info("wa_append_message → role=%s phone=%s loc=%s lead=%s", role, phone, loc, lead_id)
    endpoint = f"{cfg.supabase_url}/rest/v1/rpc/wa_append_message"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "p_location_id": loc,
        "p_telefono": phone,
        "p_role": role,
        "p_content": content,
        "p_proveedor": proveedor,
        "p_type": msg_type,
    }
    if provider_msg_id:
        payload["p_provider_msg_id"] = provider_msg_id
    if contacto_nombre:
        payload["p_contacto_nombre"] = contacto_nombre
    if lead_id:
        payload["p_lead_id"] = lead_id

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(endpoint, headers=headers, json=payload)
            r.raise_for_status()
            conv_id = r.json() if r.content else None
            log.info("wa_append_message: %s conv=%s", role, conv_id)
            return conv_id
    except httpx.HTTPError as exc:
        log.warning("wa_append_message: fallo (%s): %s", role, exc)
        return None
