from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

try:
    from .config import DASHBOARD_LINK_HOURS, dashboard_base_url, dashboard_secret
except ImportError:  # execução direta com python bot.py
    from config import DASHBOARD_LINK_HOURS, dashboard_base_url, dashboard_secret


@dataclass(frozen=True)
class DashboardIdentity:
    chat_id: int
    expires_at: int


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_dashboard_token(chat_id: int, hours: int | None = None) -> str:
    ttl_hours = hours or DASHBOARD_LINK_HOURS
    payload = {
        "v": 1,
        "chat_id": int(chat_id),
        "exp": int(time.time()) + int(ttl_hours * 3600),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_part = _b64_encode(payload_bytes)
    signature = hmac.new(
        dashboard_secret().encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_part}.{_b64_encode(signature)}"


def verify_dashboard_token(token: str) -> DashboardIdentity:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected = hmac.new(
            dashboard_secret().encode("utf-8"),
            payload_part.encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied = _b64_decode(signature_part)
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("assinatura inválida")
        payload = json.loads(_b64_decode(payload_part))
        if int(payload.get("v", 0)) != 1:
            raise ValueError("versão inválida")
        chat_id = int(payload["chat_id"])
        expires_at = int(payload["exp"])
        if expires_at < int(time.time()):
            raise ValueError("link expirado")
        return DashboardIdentity(chat_id=chat_id, expires_at=expires_at)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Link do dashboard inválido ou expirado.") from exc


def dashboard_url(chat_id: int) -> str:
    return f"{dashboard_base_url()}/dashboard/{create_dashboard_token(chat_id)}"
