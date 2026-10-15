# agent/providers/meta.py — Proveedor WhatsApp Cloud API (Meta) con Coexistence
#
# Soporta el modelo híbrido "Coex" de Koala OS:
#   * Mensajes entrantes del cliente              → MensajeEntrante (role user).
#   * `message_echoes` / `smb_message_echoes`     → mensajes que el ENCARGADO
#     envió desde la app WhatsApp Business. Se marcan es_operador=True para que
#     Koala OS los registre como `operator` y dispare el handoff (cancela la
#     cola de IA + pausa la IA).
#   * Ruteo multi-número: cada phone_number_id del WABA se mapea a un local
#     (KOALA_NUMBER_MAP), y las respuestas salen desde el número correcto.

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

log = logging.getLogger("agentkit")

_GRAPH_BASE = "https://graph.facebook.com"


def _load_number_map() -> dict[str, str]:
    """Mapa phone_number_id → location_id desde KOALA_NUMBER_MAP (JSON).

    Ej: KOALA_NUMBER_MAP='{"123456":"loc-pilar","789012":"loc-palermo"}'
    """
    raw = os.getenv("KOALA_NUMBER_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError as exc:
        log.warning("KOALA_NUMBER_MAP inválido (%s): %s", exc, raw[:120])
    return {}


class ProveedorMeta(ProveedorWhatsApp):
    """WhatsApp Cloud API (Meta) — compatible con Coexistence."""

    def __init__(self) -> None:
        self.access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
        self.default_phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "").strip()
        self.api_version = os.getenv("META_API_VERSION", "v21.0").strip() or "v21.0"
        self.verify_token = os.getenv(
            "META_VERIFY_TOKEN", os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        ).strip()
        self.number_map = _load_number_map()
        if not self.access_token:
            log.warning("ProveedorMeta: META_ACCESS_TOKEN vacío — el envío fallará")

    # ── Verificación GET del webhook ─────────────────────────────────────────
    async def validar_webhook(self, request: Request) -> dict | int | None:
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token and token == self.verify_token:
            log.info("Webhook de Meta verificado correctamente")
            try:
                return int(challenge) if challenge is not None else None
            except (TypeError, ValueError):
                return challenge
        log.warning("Verificación de webhook de Meta fallida (token no coincide)")
        return None

    # ── Parseo del webhook (entrantes + echoes de la app) ────────────────────
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        try:
            payload = await request.json()
        except Exception:
            return []

        mensajes: list[MensajeEntrante] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                phone_number_id = str(metadata.get("phone_number_id") or "")
                location_id = self.number_map.get(phone_number_id, "")

                # Nombre del contacto (si viene).
                contactos = {}
                for c in value.get("contacts", []) or []:
                    wa_id = str(c.get("wa_id") or "")
                    nombre = ((c.get("profile") or {}).get("name") or "").strip()
                    if wa_id:
                        contactos[wa_id] = nombre

                # 1) Mensajes entrantes del cliente.
                for m in value.get("messages", []) or []:
                    parsed = self._parse_message_item(
                        m, phone_number_id, location_id, contactos,
                        es_operador=False, from_field="from",
                    )
                    if parsed:
                        mensajes.append(parsed)

                # 2) Echoes de la app (Coexistence): los mandó un humano del
                #    local. Meta/BSP los expone como `message_echoes` o
                #    `smb_message_echoes`. El "from" es el número del negocio
                #    y el destinatario (recipient_id / to) es el cliente.
                echoes = value.get("message_echoes") or value.get("smb_message_echoes") or []
                for m in echoes:
                    parsed = self._parse_message_item(
                        m, phone_number_id, location_id, contactos,
                        es_operador=True, from_field="to",
                    )
                    if parsed:
                        mensajes.append(parsed)

        return mensajes

    def _parse_message_item(
        self,
        m: dict,
        phone_number_id: str,
        location_id: str,
        contactos: dict,
        *,
        es_operador: bool,
        from_field: str,
    ) -> Optional[MensajeEntrante]:
        msg_id = str(m.get("id") or "")
        msg_type = m.get("type") or "text"

        # El teléfono del CLIENTE es siempre el que identifica la conversación.
        # - entrante:  m["from"] = cliente.
        # - echo:      m["to"] / m["recipient_id"] = cliente.
        if es_operador:
            cliente = str(m.get("to") or m.get("recipient_id") or "")
        else:
            cliente = str(m.get(from_field) or "")
        if not cliente:
            return None

        if msg_type == "text":
            texto = ((m.get("text") or {}).get("body") or "").strip()
        else:
            # Multimedia/otros: marcamos para que el flujo responda con el
            # aviso de solo-texto (igual que Twilio) — salvo en echoes, que no
            # requieren respuesta del bot.
            texto = "__MULTIMEDIA__"

        if not texto:
            return None

        return MensajeEntrante(
            telefono=cliente,
            texto=texto,
            mensaje_id=msg_id,
            es_propio=False,
            nombre=contactos.get(cliente, ""),
            es_operador=es_operador,
            phone_number_id=phone_number_id,
            location_id=location_id,
        )

    # ── Envío ────────────────────────────────────────────────────────────────
    async def enviar_mensaje(
        self,
        telefono: str,
        mensaje: str,
        from_phone_number_id: Optional[str] = None,
    ) -> bool:
        pnid = (from_phone_number_id or self.default_phone_number_id).strip()
        if not pnid:
            log.error("enviar_mensaje (meta): sin phone_number_id para enviar")
            return False
        if not self.access_token:
            log.error("enviar_mensaje (meta): sin META_ACCESS_TOKEN")
            return False

        url = f"{_GRAPH_BASE}/{self.api_version}/{pnid}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": mensaje},
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(url, headers=headers, json=body)
                if r.status_code >= 400:
                    log.error("enviar_mensaje (meta) %s: %s", r.status_code, r.text[:300])
                    return False
                return True
        except httpx.HTTPError as exc:
            log.error("enviar_mensaje (meta) fallo de red: %s", exc)
            return False
