# Integración con Koala OS

Este documento explica cómo conectar el agente generado por `whatsapp-agentkit`
con la tabla `crm_leads` de Koala OS, para que cada conversación nueva por
WhatsApp aparezca automáticamente en el Pipeline del CRM.

> Hacelo DESPUÉS de correr `/build-agent` y de haber probado el agente en
> `tests/test_local.py`. Antes de esto, no tiene sentido sincronizar nada.

---

## 1. Cómo funciona

```
Cliente → WhatsApp → Twilio/Meta → webhook → agente Python (FastAPI en Railway)
                                                 │
                                                 ├─ Claude AI responde
                                                 ├─ POST a Supabase REST
                                                 │  /rest/v1/crm_leads
                                                 │  (upsert por teléfono)
                                                 │
                                                 └─ RPC wa_append_message
                                                    (cada mensaje del hilo)
                                                          │
                                                          ▼
                                                  Koala OS (Netlify)
                                                  ve el lead nuevo
                                                  y la conversación
                                                  en tiempo real
```

El frontend Koala OS no necesita ningún cambio: ya lee `crm_leads` y
`wa_conversations` desde Supabase y se suscribe vía Realtime. Cada vez que
el agente llama a `wa_append_message`, la pantalla de Canales > WhatsApp
refresca sola.

---

## 2. Variables de entorno

Copiá `.env.koala-os.example` adentro de tu `.env`:

```bash
cat .env.koala-os.example >> .env
```

Y completá los valores en Railway (sección Variables):

| Variable | De dónde sacarla |
|---|---|
| `SUPABASE_URL` | Misma del `.env.local` del frontend (`VITE_SUPABASE_URL`). |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase Dashboard → Settings → API → **service_role**. |
| `KOALA_LOCATION_ID` | ID del local (`loc-pilar`, `loc-palermo`, …). |
| `KOALA_DEFAULT_CHANNEL` | `whatsapp` (es el único que tiene sentido acá). |
| `KOALA_LEAD_SOURCE` | Texto que se guarda en `crm_leads.fuente_detalle`. |

⚠️ **NUNCA** subas `SUPABASE_SERVICE_ROLE_KEY` a GitHub. Está en `.gitignore`
porque `.env` está ignorado.

---

## 3. Módulo `agent/koala_sync.py`

Cuando hayas corrido `/build-agent` y tengas la carpeta `agent/` generada,
creá este archivo. Hace upsert por el campo `telefono` (los últimos 6 dígitos
matchean con la lógica de `crm_find_customer_matches`).

```python
# agent/koala_sync.py
"""Sincronización opcional de leads con Koala OS (Supabase)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class KoalaSyncConfig:
    supabase_url: str
    service_role_key: str
    location_id: str
    default_channel: str = "whatsapp"
    lead_source: str = "Agente IA WhatsApp"

    @classmethod
    def from_env(cls) -> Optional["KoalaSyncConfig"]:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        loc = os.getenv("KOALA_LOCATION_ID", "").strip()
        if not (url and key and loc):
            return None
        return cls(
            supabase_url=url.rstrip("/"),
            service_role_key=key,
            location_id=loc,
            default_channel=os.getenv("KOALA_DEFAULT_CHANNEL", "whatsapp"),
            lead_source=os.getenv("KOALA_LEAD_SOURCE", "Agente IA WhatsApp"),
        )


async def upsert_lead_from_message(
    cfg: KoalaSyncConfig,
    phone: str,
    name: Optional[str],
    last_message: str,
) -> None:
    """Crea/actualiza un lead en crm_leads a partir de un mensaje entrante.

    Usa el endpoint REST de Supabase con la service role key. No necesita
    sesión de usuario.

    El campo `contacto` se usa para hacer upsert manual: buscamos por
    teléfono antes de insertar.
    """
    phone = (phone or "").strip()
    if not phone:
        return

    endpoint = f"{cfg.supabase_url}/rest/v1/crm_leads"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    payload = {
        "location_id": cfg.location_id,
        "nombre": (name or phone)[:120],
        "contacto": phone,
        "canal": cfg.default_channel,
        "estado": "nuevo",
        "ultimo_mensaje": last_message[:500],
        "telefono": phone,
        "fuente_detalle": cfg.lead_source,
        "fuente": "whatsapp-agentkit",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # 1) Buscar por teléfono exacto.
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

        if existing:
            lead_id = existing[0]["id"]
            try:
                upd = await client.patch(
                    f"{endpoint}?id=eq.{lead_id}",
                    headers=headers,
                    json={
                        "ultimo_mensaje": last_message[:500],
                        "updated_at": "now()",
                    },
                )
                upd.raise_for_status()
                log.info("koala_sync: lead %s actualizado", lead_id)
            except httpx.HTTPError as exc:
                log.warning("koala_sync: fallo actualizando lead %s: %s", lead_id, exc)
            return

        # 2) No existe: insertar.
        try:
            ins = await client.post(endpoint, headers=headers, json=payload)
            ins.raise_for_status()
            new_id = ins.json()[0]["id"] if ins.json() else "?"
            log.info("koala_sync: lead nuevo %s creado", new_id)
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo creando lead: %s", exc)
```

---

## 4. Integración en `agent/main.py`

Justo después de que el agente reciba un mensaje (y antes o después de
responder, ambos están bien), llamá al sync:

```python
# agent/main.py (extracto)

from agent.koala_sync import (
    KoalaSyncConfig,
    upsert_lead_from_message,
    append_wa_message,
)

_koala_cfg = KoalaSyncConfig.from_env()  # None si faltan variables

# Dentro del handler del webhook, después de parsear el mensaje:
if _koala_cfg:
    # 1) Pipeline del CRM
    await upsert_lead_from_message(
        _koala_cfg,
        phone=msg.from_phone,        # adaptar al nombre que use tu agente
        name=msg.sender_name,        # idem
        last_message=msg.text,
    )
    # 2) Hilo de conversación visible en Canales > WhatsApp
    await append_wa_message(
        _koala_cfg,
        phone=msg.from_phone,
        role="user",
        content=msg.text,
        contacto_nombre=msg.sender_name,
        provider_msg_id=msg.id,      # Twilio SID, Meta wamid, etc.
    )

# Y cuando el bot responde, hacé otro append_wa_message con role="bot":
if _koala_cfg:
    await append_wa_message(
        _koala_cfg,
        phone=msg.from_phone,
        role="bot",
        content=reply_text,
    )
```

### Nuevo: `append_wa_message`

Sumalo a `agent/koala_sync.py` (al final del archivo):

```python
async def append_wa_message(
    cfg: KoalaSyncConfig,
    phone: str,
    role: str,                       # "user" | "bot" | "operator" | "system"
    content: str,
    contacto_nombre: Optional[str] = None,
    provider_msg_id: Optional[str] = None,
    msg_type: str = "text",
    payload: Optional[dict] = None,
) -> None:
    """Hace POST a la RPC wa_append_message en Supabase.

    La función SQL upsertea la conversación (location_id, telefono,
    proveedor) y agrega el mensaje al JSONB `mensajes` de forma atómica.
    El front lo recibe vía Realtime sin necesidad de refrescar.
    """
    phone = (phone or "").strip()
    if not phone or not content:
        return

    endpoint = f"{cfg.supabase_url}/rest/v1/rpc/wa_append_message"
    headers = {
        "apikey": cfg.service_role_key,
        "Authorization": f"Bearer {cfg.service_role_key}",
        "Content-Type": "application/json",
    }
    body = {
        "p_location_id":     cfg.location_id,
        "p_telefono":        phone,
        "p_role":            role,
        "p_content":         content,
        "p_proveedor":       "agentkit",
        "p_type":            msg_type,
        "p_payload":         payload or {},
        "p_provider_msg_id": provider_msg_id,
        "p_contacto_nombre": contacto_nombre,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(endpoint, headers=headers, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("koala_sync: fallo en wa_append_message: %s", exc)
```

> Tip: si querés también guardar adjuntos (audios, imágenes), pasá la
> URL en `payload` (`{"url": "...", "caption": "..."}`) y usá `msg_type`
> = `"image"` / `"audio"`. La UI de Koala OS hoy renderiza solo
> `content` pero el JSONB queda guardado para mostrarlo cuando se
> agregue soporte visual.

---

## 5. CORS para el panel de Canales

El panel de Koala OS hace `GET /health` al agente para mostrar estado online.
Habilitá CORS para tu dominio Netlify:

```python
# agent/main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://koala-os.netlify.app",      # tu dominio en Netlify
        "http://localhost:5173",             # vite dev local
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    import os
    return {
        "status": "ok",
        "version": "1.0.0",
        "provider": os.getenv("WHATSAPP_PROVIDER", "none"),
    }
```

---

## 6. Checklist final

- [ ] `/build-agent` ejecutado y agente probado en `tests/test_local.py`.
- [ ] `.env` con `ANTHROPIC_API_KEY` y credenciales del proveedor.
- [ ] `.env.koala-os.example` mergeado al `.env`.
- [ ] `agent/koala_sync.py` creado (con `upsert_lead_from_message` **y** `append_wa_message`).
- [ ] `agent/main.py` llama `upsert_lead_from_message` para el lead y `append_wa_message` para cada mensaje (entrante y saliente).
- [ ] CORS habilitado para dominios Netlify y localhost.
- [ ] `GET /health` responde JSON con `status`, `version`, `provider`.
- [ ] Deploy en Railway con todas las variables seteadas.
- [ ] En Koala OS → Canales → WhatsApp: pegar URL y hacer ping.
- [ ] Webhook de Twilio/Meta apuntando a `<URL>/webhook`.
- [ ] Mandar un WhatsApp de prueba al número del agente y verificar que aparezca el lead en CRM **y** la conversación en Canales > WhatsApp.
