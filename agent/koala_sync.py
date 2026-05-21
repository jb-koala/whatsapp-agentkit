# agent/koala_sync.py — Sincronización de leads con Koala OS (Supabase)
# Generado por AgentKit

"""
Sincronización de leads con la tabla crm_leads de Supabase.
Cada contacto por WhatsApp entra como lead al CRM automáticamente.
Usa upsert por teléfono para evitar duplicados.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Retries / timeouts globales para llamadas a Supabase REST.
# Más altos que el default porque un mensaje perdido es peor que
# un webhook lento.
_HTTP_TIMEOUT_S = 20.0
_HTTP_RETRIES = 3
_HTTP_BACKOFF_BASE_S = 0.6


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
    # None cuando KOALA_LOCATION_ID no está seteado: ese caso significa
    # "todavía no sabemos a qué local pertenece"; lo dejamos NULL en la
    # DB y se completa cuando el cliente lo mencione (detectar_local).
    default_location_id: Optional[str] = None
    default_channel: str = "whatsapp"
    lead_source: str = "Agente IA WhatsApp"
    # Minutos sin actividad para considerar que la sesión del cliente
    # terminó. Si llega un mensaje pasado ese tiempo, se cierra el lead
    # abierto (estado=perdido) y se abre uno nuevo aunque el intent sea
    # el mismo.
    intent_timeout_min: int = 60

    @classmethod
    def from_env(cls) -> Optional["KoalaSyncConfig"]:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        loc = os.getenv("KOALA_LOCATION_ID", "").strip()
        if not (url and key):
            return None
        try:
            timeout_min = int(os.getenv("KOALA_INTENT_TIMEOUT_MIN", "60"))
        except ValueError:
            timeout_min = 60
        return cls(
            supabase_url=url.rstrip("/"),
            service_role_key=key,
            default_location_id=loc or None,
            default_channel=os.getenv("KOALA_DEFAULT_CHANNEL", "whatsapp"),
            lead_source=os.getenv("KOALA_LEAD_SOURCE", "Agente IA WhatsApp"),
            intent_timeout_min=max(1, timeout_min),
        )


# Estados que indican un lead aún "en juego" (no cerrado).
_OPEN_STATES = ("nuevo", "en_contacto", "calificado", "propuesta")


def _now_utc() -> "datetime":
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_iso_utc(value: Optional[str]) -> Optional["datetime"]:
    """Parsea timestamps ISO con o sin Z, devolviendo aware UTC."""
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        # Postgres devuelve algo como '2026-05-21T00:20:11.673+00:00'
        # o '2026-05-21T00:20:11.673Z'.
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _map_intent_status_to_estado(intent: str, intent_status: str) -> str:
    """Mapea (intent, intent_status) → estado del pipeline al cerrar."""
    if intent_status == "confirmado":
        return "ganado"
    if intent_status == "perdido":
        return "perdido"
    if intent_status == "cerrado":
        # Consultas que terminaron sin reserva: lead "ganado" en términos
        # de atención, aunque no haya venta. Cancelaciones cerradas → perdido.
        if intent in ("consulta", "evento"):
            return "ganado"
        return "perdido"
    # Abierto: no debería entrar acá (no se cierra), pero por seguridad
    # lo dejamos como perdido para no quedar colgado.
    return "perdido"


async def find_open_lead(
    cfg: KoalaSyncConfig,
    phone: str,
) -> Optional[dict]:
    """Devuelve el lead más reciente NO cerrado del contacto, o None.

    Útil tanto para la lógica interna de `upsert_lead_from_message` como
    para que `main.py` pueda consultar el `intent` vigente antes de
    pedirle un resumen a Claude.
    """
    endpoint = f"{cfg.supabase_url}/rest/v1/crm_leads"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
    }
    states = ",".join(_OPEN_STATES)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            r = await client.get(
                endpoint,
                headers=headers,
                params={
                    "select": "id,telefono,intent,estado,updated_at",
                    "or": f"(telefono.eq.{phone},contacto.eq.{phone})",
                    "estado": f"in.({states})",
                    "order": "updated_at.desc",
                    "limit": 1,
                },
            )
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
    except httpx.HTTPError as exc:
        log.warning("koala_sync._find_open_lead: %s", exc)
        return None


async def _close_lead(
    cfg: KoalaSyncConfig,
    lead_id: str,
    estado: str,
    motivo: str = "",
) -> bool:
    """Cierra un lead seteando su estado final (ganado / perdido)."""
    endpoint = f"{cfg.supabase_url}/rest/v1/crm_leads?id=eq.{lead_id}"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
    }
    patch: dict[str, object] = {"estado": estado, "updated_at": "now()"}
    if estado == "perdido" and motivo:
        patch["motivo_perdida"] = motivo[:200]
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            r = await client.patch(endpoint, headers=headers, json=patch)
            r.raise_for_status()
            log.info("koala_sync: lead %s cerrado (estado=%s)", lead_id, estado)
            return True
    except httpx.HTTPError as exc:
        log.warning("koala_sync._close_lead %s: %s", lead_id, exc)
        return False


async def upsert_lead_from_message(
    cfg: KoalaSyncConfig,
    phone: str,
    name: Optional[str],
    last_message: str,
    notas_internas: str = "",
    location_id: Optional[str] = None,
    etiquetas: Optional[list[str]] = None,
    intent: Optional[str] = None,
    intent_status: Optional[str] = None,
    intent_changed: bool = False,
    resumen_intent: Optional[str] = None,
) -> Optional[str]:
    """Crea o actualiza un lead en crm_leads según la intención del contacto.

    Modelo de oportunidades:
      * Un lead = una intención (reserva, cancelación, consulta, etc.).
      * Mientras la intención **no cambie** y la sesión esté activa (sin
        timeout), se actualiza el mismo lead.
      * Si Claude declara `intent_changed=True` o pasaron más de
        `cfg.intent_timeout_min` minutos sin actividad, se cierra el lead
        anterior (estado `ganado` / `perdido` según `intent_status`) y se
        abre uno nuevo con el intent nuevo.

    Parámetros nuevos vs versión anterior:
      * `intent`           — intent declarado por Claude (`indefinido`, `reserva`,
                              `cancelacion`, `consulta`, `queja`, `evento`, `otro`).
      * `intent_status`    — `abierto` | `confirmado` | `perdido` | `cerrado`.
                              Solo se usa al CERRAR el lead anterior.
      * `intent_changed`   — Claude detectó que la intención cambió.
      * `resumen_intent`   — Resumen específico del intent actual. Se usa
                              como `notas_internas` del lead nuevo (para no
                              contaminar el resumen con intenciones viejas).

    Retorna el id del lead (UUID como str) o None si falló.
    """
    phone = (phone or "").strip()
    if not phone:
        return None

    intent_norm = (intent or "indefinido").lower().strip() or "indefinido"
    status_norm = (intent_status or "abierto").lower().strip() or "abierto"
    notes = (notas_internas or "")[:500]
    notes_intent = (resumen_intent or notas_internas or "")[:500]

    # 1) Lead actualmente abierto del contacto.
    open_lead = await find_open_lead(cfg, phone)
    timed_out = False
    if open_lead:
        last_seen = _parse_iso_utc(open_lead.get("updated_at"))
        if last_seen is not None:
            from datetime import timedelta
            timed_out = (_now_utc() - last_seen) > timedelta(
                minutes=cfg.intent_timeout_min
            )

    # 2) Decidir si abrir un lead nuevo o seguir con el existente.
    open_intent = (open_lead.get("intent") if open_lead else None) or "indefinido"
    promoting_indef = (
        open_lead is not None
        and open_intent == "indefinido"
        and intent_norm not in ("", "indefinido")
    )
    must_open_new = open_lead is None or (
        not promoting_indef and (intent_changed or timed_out)
    )

    if must_open_new and open_lead:
        # Cerrar el lead viejo antes de crear el nuevo.
        if timed_out:
            await _close_lead(
                cfg,
                open_lead["id"],
                "perdido",
                motivo="Cierre por inactividad (timeout)",
            )
        else:
            estado_cierre = _map_intent_status_to_estado(open_intent, status_norm)
            motivo = "" if estado_cierre == "ganado" else f"Cambio de intent → {intent_norm}"
            await _close_lead(cfg, open_lead["id"], estado_cierre, motivo=motivo)

    # 3) Determinar location_id del lead actual.
    loc = location_id or detectar_local(last_message) or cfg.default_location_id

    endpoint = f"{cfg.supabase_url}/rest/v1/crm_leads"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    # 4a) Crear lead nuevo
    if must_open_new:
        payload = {
            "location_id": loc,
            "nombre": (name or phone)[:120],
            "contacto": phone,
            "canal": cfg.default_channel,
            "estado": "nuevo",
            "ultimo_mensaje": last_message[:500],
            "telefono": phone,
            "fuente_detalle": cfg.lead_source,
            "notas_internas": notes_intent,
            "intent": intent_norm,
        }
        if etiquetas:
            payload["etiquetas"] = etiquetas
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
                ins = await client.post(endpoint, headers=headers, json=payload)
                ins.raise_for_status()
                body = ins.json()
                new_id = body[0]["id"] if body else None
                log.info(
                    "koala_sync: lead nuevo %s creado (intent=%s)",
                    new_id or "?", intent_norm,
                )
                return new_id
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo creando lead: %s", exc)
            return None

    # 4b) Actualizar el lead existente.
    lead_id = open_lead["id"]  # type: ignore[index]
    patch_data: dict[str, object] = {
        "ultimo_mensaje": last_message[:500],
        "updated_at": "now()",
    }
    if name:
        patch_data["nombre"] = name[:120]
    if loc:
        patch_data["location_id"] = loc
    if notes:
        patch_data["notas_internas"] = notes
    if etiquetas:
        patch_data["etiquetas"] = etiquetas
    # Promover el intent si veníamos de 'indefinido' y ahora hay uno definido.
    if open_intent == "indefinido" and intent_norm not in ("", "indefinido"):
        patch_data["intent"] = intent_norm
        # En este caso usamos el resumen del intent (más específico).
        if notes_intent:
            patch_data["notas_internas"] = notes_intent
    # Si el LLM dice que el intent está "confirmado" mientras seguimos en
    # estado 'nuevo', promover a 'calificado' para reflejar el progreso.
    if status_norm == "abierto":
        # No tocar el estado.
        pass

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            upd = await client.patch(
                f"{endpoint}?id=eq.{lead_id}",
                headers=headers,
                json=patch_data,
            )
            upd.raise_for_status()
            log.info("koala_sync: lead %s actualizado (intent=%s)", lead_id, intent_norm)
            return lead_id
    except httpx.HTTPError as exc:
        log.warning("koala_sync: fallo actualizando lead %s: %s", lead_id, exc)
        return lead_id


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

    # Retries con backoff exponencial: queremos NUNCA perder un mensaje
    # porque Supabase pestañeó. Solo se reintenta ante errores de red /
    # 5xx. 4xx (request mal formado) no se reintentan.
    last_exc: Optional[Exception] = None
    for attempt in range(1, _HTTP_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
                r = await client.post(endpoint, headers=headers, json=payload)
                if 500 <= r.status_code < 600:
                    last_exc = httpx.HTTPStatusError(
                        f"Supabase {r.status_code}: {r.text[:200]}",
                        request=r.request,
                        response=r,
                    )
                    log.warning(
                        "wa_append_message: 5xx en intento %d/%d (%s): %s",
                        attempt, _HTTP_RETRIES, role, r.text[:200],
                    )
                else:
                    r.raise_for_status()
                    conv_id = r.json() if r.content else None
                    log.info("wa_append_message: %s conv=%s", role, conv_id)
                    return conv_id
        except httpx.HTTPStatusError as exc:
            # 4xx no se reintenta (payload mal formado, RLS, etc.)
            log.warning(
                "wa_append_message: HTTP %s (%s) en intento %d, no se reintenta: %s",
                exc.response.status_code, role, attempt, exc.response.text[:200],
            )
            return None
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last_exc = exc
            log.warning(
                "wa_append_message: fallo de red en intento %d/%d (%s): %s",
                attempt, _HTTP_RETRIES, role, exc,
            )

        if attempt < _HTTP_RETRIES:
            await asyncio.sleep(_HTTP_BACKOFF_BASE_S * (2 ** (attempt - 1)))

    log.error(
        "wa_append_message: agotados %d intentos para %s phone=%s — mensaje no persistido: %s",
        _HTTP_RETRIES, role, phone, last_exc,
    )
    return None
