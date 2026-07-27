from __future__ import annotations

import os
from decimal import Decimal
from zoneinfo import ZoneInfo


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_url() -> str:
    value = env("DATABASE_URL") or env("BANK_DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL/BANK_DATABASE_URL não configurado.")
    return value


def bot_token(explicit: str | None = None) -> str:
    value = (explicit or env("BOT_TOKEN") or env("BANK_BOT_TOKEN")).strip()
    if not value:
        raise RuntimeError("BOT_TOKEN/BANK_BOT_TOKEN não configurado.")
    return value


def dashboard_base_url() -> str:
    value = env("DASHBOARD_BASE_URL").rstrip("/")
    if not value:
        raise RuntimeError("DASHBOARD_BASE_URL não configurado.")
    if not value.startswith("https://"):
        raise RuntimeError("DASHBOARD_BASE_URL precisa começar com https://")
    return value


def dashboard_secret() -> str:
    value = env("DASHBOARD_SECRET")
    if len(value) < 32:
        raise RuntimeError("DASHBOARD_SECRET deve ter pelo menos 32 caracteres.")
    return value


DEFAULT_INITIAL_BANKROLL = Decimal(env("INITIAL_BANKROLL", "1000").replace(",", "."))
DEFAULT_CURRENCY = env("CURRENCY", "R$") or "R$"
TIMEZONE_NAME = env("BANK_TIMEZONE", "America/Sao_Paulo")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
HISTORY_LIMIT = max(1, min(50, int(env("BANK_HISTORY_DEFAULT_LIMIT", "10"))))
DASHBOARD_LINK_HOURS = max(1, min(24 * 30, int(env("DASHBOARD_LINK_HOURS", "168"))))
MAX_STAKE_PERCENT = Decimal(env("MAX_STAKE_PERCENT", "5").replace(",", "."))
MAX_OPEN_EXPOSURE_PERCENT = Decimal(env("MAX_OPEN_EXPOSURE_PERCENT", "15").replace(",", "."))
VALID_STATUSES = {"GREEN", "RED", "VOID", "HALF_GREEN", "HALF_RED", "PENDING"}
