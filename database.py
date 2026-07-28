from __future__ import annotations

import csv
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

import psycopg2
import psycopg2.extras

try:
    from .config import (
        DEFAULT_CURRENCY,
        DEFAULT_INITIAL_BANKROLL,
        MAX_OPEN_EXPOSURE_PERCENT,
        MAX_STAKE_PERCENT,
        TIMEZONE,
        TIMEZONE_NAME,
        VALID_STATUSES,
        database_url,
    )
except ImportError:
    from config import (
        DEFAULT_CURRENCY,
        DEFAULT_INITIAL_BANKROLL,
        MAX_OPEN_EXPOSURE_PERCENT,
        MAX_STAKE_PERCENT,
        TIMEZONE,
        TIMEZONE_NAME,
        VALID_STATUSES,
        database_url,
    )

MONEY_QUANT = Decimal("0.01")


def get_conn():
    return psycopg2.connect(
        database_url(),
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=15,
        application_name="telegram-bankroll-v3-lite",
    )


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bankroll_settings (
                    chat_id BIGINT PRIMARY KEY,
                    initial_bankroll NUMERIC(16,2) NOT NULL DEFAULT 1000,
                    currency TEXT NOT NULL DEFAULT 'R$',
                    base_bets INTEGER NOT NULL DEFAULT 0,
                    base_staked NUMERIC(16,2) NOT NULL DEFAULT 0,
                    base_profit NUMERIC(16,2) NOT NULL DEFAULT 0,
                    daily_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    monthly_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    display_name TEXT,
                    username TEXT,
                    max_stake_percent NUMERIC(8,2) NOT NULL DEFAULT 5,
                    max_open_exposure_percent NUMERIC(8,2) NOT NULL DEFAULT 15,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bets (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    bet_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    sport TEXT NOT NULL,
                    event TEXT NOT NULL,
                    market TEXT NOT NULL,
                    odds NUMERIC(12,4) NOT NULL,
                    stake NUMERIC(16,2) NOT NULL,
                    status TEXT NOT NULL,
                    profit NUMERIC(16,2) NOT NULL DEFAULT 0,
                    notes TEXT,
                    tipster TEXT,
                    bookmaker TEXT,
                    league TEXT,
                    settled_at TIMESTAMPTZ,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    deleted_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT bets_status_check CHECK (
                        status IN ('GREEN','RED','VOID','HALF_GREEN','HALF_RED','PENDING')
                    )
                );
                """
            )
            # Compatibilidade com a tabela antiga.
            for statement in (
                "ALTER TABLE bankroll_settings ADD COLUMN IF NOT EXISTS display_name TEXT;",
                "ALTER TABLE bankroll_settings ADD COLUMN IF NOT EXISTS username TEXT;",
                "ALTER TABLE bankroll_settings ADD COLUMN IF NOT EXISTS max_stake_percent NUMERIC(8,2) NOT NULL DEFAULT 5;",
                "ALTER TABLE bankroll_settings ADD COLUMN IF NOT EXISTS max_open_exposure_percent NUMERIC(8,2) NOT NULL DEFAULT 15;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS tipster TEXT;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS bookmaker TEXT;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS league TEXT;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;",
            ):
                cur.execute(statement)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bankroll_transactions (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('DEPOSIT','WITHDRAWAL','BONUS','ADJUSTMENT')),
                    amount NUMERIC(16,2) NOT NULL CHECK (amount > 0),
                    note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_chat_date ON bets(chat_id, bet_date DESC);")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_chat_pending ON bets(chat_id, bet_date DESC) "
                "WHERE is_deleted=FALSE AND status='PENDING';"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_chat_active_date ON bets(chat_id, bet_date DESC) "
                "WHERE is_deleted=FALSE;"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_chat_date "
                "ON bankroll_transactions(chat_id, created_at DESC);"
            )


def d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return d(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def parse_decimal(text: str) -> Decimal:
    raw = text.strip().replace("R$", "").replace("$", "").replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Número inválido: {text}") from exc


def fmt_money(value: Any, currency: str = "R$") -> str:
    amount = money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, cents = f"{amount:.2f}".split(".")
    formatted = f"{int(whole):,}".replace(",", ".")
    return f"{sign}{currency} {formatted},{cents}"


def fmt_num(value: Any, places: int = 2) -> str:
    return f"{d(value):.{places}f}".replace(".", ",")


def normalize_status(text: str) -> str:
    raw = text.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "G": "GREEN", "WIN": "GREEN", "GANHOU": "GREEN", "GREEN": "GREEN",
        "R": "RED", "LOSS": "RED", "PERDEU": "RED", "RED": "RED",
        "VOID": "VOID", "DEVOLVIDA": "VOID", "DEVOLVIDO": "VOID",
        "HG": "HALF_GREEN", "HALFGREEN": "HALF_GREEN", "HALF_GREEN": "HALF_GREEN",
        "HR": "HALF_RED", "HALFRED": "HALF_RED", "HALF_RED": "HALF_RED",
        "P": "PENDING", "PENDENTE": "PENDING", "PENDING": "PENDING",
    }
    status = aliases.get(raw)
    if status not in VALID_STATUSES:
        raise ValueError("Status inválido. Use GREEN, RED, VOID, HALF_GREEN, HALF_RED ou PENDING.")
    return status


def calculate_profit(odds: Decimal, stake: Decimal, status: str) -> Decimal:
    status = normalize_status(status)
    if status == "GREEN":
        return money((odds - Decimal("1")) * stake)
    if status == "RED":
        return money(-stake)
    if status == "HALF_GREEN":
        return money(((odds - Decimal("1")) * stake) / Decimal("2"))
    if status == "HALF_RED":
        return money(-stake / Decimal("2"))
    return Decimal("0.00")


def ensure_settings(chat_id: int, display_name: str | None = None, username: str | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bankroll_settings (
                    chat_id, initial_bankroll, currency, display_name, username,
                    max_stake_percent, max_open_exposure_percent
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    display_name=COALESCE(EXCLUDED.display_name, bankroll_settings.display_name),
                    username=COALESCE(EXCLUDED.username, bankroll_settings.username),
                    updated_at=NOW();
                """,
                (
                    chat_id, DEFAULT_INITIAL_BANKROLL, DEFAULT_CURRENCY,
                    display_name, username, MAX_STAKE_PERCENT, MAX_OPEN_EXPOSURE_PERCENT,
                ),
            )


def get_settings(chat_id: int) -> dict[str, Any]:
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_settings WHERE chat_id=%s;", (chat_id,))
            row = cur.fetchone()
    if not row:
        raise RuntimeError("Configuração não encontrada.")
    return dict(row)


def set_initial_bankroll(chat_id: int, amount: Decimal) -> None:
    if amount < 0:
        raise ValueError("A banca inicial não pode ser negativa.")
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bankroll_settings SET initial_bankroll=%s, updated_at=NOW() WHERE chat_id=%s;",
                (money(amount), chat_id),
            )



def set_base_summary(chat_id: int, base_bets: int, base_staked: Decimal, base_profit: Decimal) -> None:
    if base_bets < 0:
        raise ValueError("A quantidade de apostas não pode ser negativa.")
    base_staked = money(base_staked)
    base_profit = money(base_profit)
    if base_staked < 0:
        raise ValueError("O total apostado não pode ser negativo.")
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bankroll_settings
                SET base_bets=%s, base_staked=%s, base_profit=%s, updated_at=NOW()
                WHERE chat_id=%s;
                """,
                (base_bets, base_staked, base_profit, chat_id),
            )

def add_transaction(chat_id: int, kind: str, amount: Decimal, note: str | None = None) -> int:
    kind = kind.upper().strip()
    if kind not in {"DEPOSIT", "WITHDRAWAL", "BONUS", "ADJUSTMENT"}:
        raise ValueError("Tipo de movimentação inválido.")
    amount = money(amount)
    if amount <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bankroll_transactions (chat_id, kind, amount, note)
                VALUES (%s,%s,%s,%s) RETURNING id;
                """,
                (chat_id, kind, amount, note),
            )
            return int(cur.fetchone()["id"])


VALID_DASHBOARD_PERIODS = {"24h", "7", "30", "90", "365", "all"}


def _period_start(period: str, *, now: datetime | None = None) -> datetime | None:
    """Retorna o início UTC do período exibido no dashboard.

    ``24h`` é uma janela móvel exata. Os demais períodos começam à meia-noite
    local para manter a leitura diária intuitiva.
    """
    current = now or datetime.now(TIMEZONE)
    if period == "all":
        return None
    if period == "24h":
        return (current - timedelta(hours=24)).astimezone(timezone.utc)
    days = int(period)
    start_local = datetime.combine(
        current.date() - timedelta(days=days - 1),
        time.min,
        tzinfo=TIMEZONE,
    )
    return start_local.astimezone(timezone.utc)


def _previous_period_bounds(period: str) -> tuple[datetime, datetime] | None:
    """Janela anterior com a mesma duração, usada nas comparações dos KPIs."""
    if period == "all":
        return None
    now_local = datetime.now(TIMEZONE)
    current_start = _period_start(period, now=now_local)
    if current_start is None:
        return None
    current_end = now_local.astimezone(timezone.utc)
    duration = current_end - current_start
    return current_start - duration, current_start


def _transaction_effect_sql() -> str:
    return "CASE WHEN kind='WITHDRAWAL' THEN -amount ELSE amount END"


def current_bankroll(chat_id: int) -> Decimal:
    settings = get_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(profit),0) AS profit FROM bets WHERE chat_id=%s AND is_deleted=FALSE;",
                (chat_id,),
            )
            profit = d(cur.fetchone()["profit"])
            cur.execute(
                f"SELECT COALESCE(SUM({_transaction_effect_sql()}),0) AS effect "
                "FROM bankroll_transactions WHERE chat_id=%s;",
                (chat_id,),
            )
            effect = d(cur.fetchone()["effect"])
    return money(d(settings["initial_bankroll"]) + d(settings["base_profit"]) + profit + effect)


def pending_exposure(chat_id: int) -> Decimal:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(stake),0) AS exposure
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND status='PENDING';
                """,
                (chat_id,),
            )
            return money(cur.fetchone()["exposure"])


def find_recent_duplicate(
    chat_id: int, event: str, market: str, odds: Decimal, stake: Decimal, minutes: int = 5
) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, bet_date FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE
                  AND LOWER(event)=LOWER(%s) AND LOWER(market)=LOWER(%s)
                  AND odds=%s AND stake=%s
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY id DESC LIMIT 1;
                """,
                (chat_id, event, market, odds, stake, minutes),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def create_bet(
    chat_id: int,
    sport: str,
    event: str,
    market: str,
    odds: Decimal,
    stake: Decimal,
    status: str,
    tipster: str | None = None,
    bookmaker: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    sport, event, market = sport.strip(), event.strip(), market.strip()
    if not sport or not event or not market:
        raise ValueError("Esporte, evento e mercado são obrigatórios.")
    odds = d(odds)
    stake = money(stake)
    status = normalize_status(status)
    if odds <= 1:
        raise ValueError("A odd precisa ser maior que 1,00.")
    if stake <= 0:
        raise ValueError("A stake precisa ser maior que zero.")
    profit = calculate_profit(odds, stake, status)
    settled_at = None if status == "PENDING" else datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bets (
                    chat_id, sport, event, market, odds, stake, status, profit,
                    tipster, bookmaker, notes, settled_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, bet_date, profit;
                """,
                (
                    chat_id, sport, event, market, odds, stake, status, profit,
                    tipster or None, bookmaker or None, notes or None, settled_at,
                ),
            )
            return dict(cur.fetchone())


def settle_bet(chat_id: int, bet_id: int, status: str) -> dict[str, Any] | None:
    status = normalize_status(status)
    if status == "PENDING":
        raise ValueError("Escolha um resultado liquidado.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, odds, stake, event, market FROM bets
                WHERE id=%s AND chat_id=%s AND is_deleted=FALSE AND status='PENDING'
                FOR UPDATE;
                """,
                (bet_id, chat_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            profit = calculate_profit(d(row["odds"]), d(row["stake"]), status)
            cur.execute(
                """
                UPDATE bets SET status=%s, profit=%s, settled_at=NOW(), updated_at=NOW()
                WHERE id=%s AND chat_id=%s
                RETURNING id, event, market, odds, stake, status, profit;
                """,
                (status, profit, bet_id, chat_id),
            )
            return dict(cur.fetchone())


def soft_delete_bet(chat_id: int, bet_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bets SET is_deleted=TRUE, deleted_at=NOW(), updated_at=NOW()
                WHERE id=%s AND chat_id=%s AND is_deleted=FALSE RETURNING id;
                """,
                (bet_id, chat_id),
            )
            return cur.fetchone() is not None


def get_pending_bets(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(25, int(limit)))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, bet_date, sport, event, market, odds, stake, tipster, bookmaker
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND status='PENDING'
                ORDER BY bet_date ASC LIMIT %s;
                """,
                (chat_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def get_recent_bets(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(50, int(limit)))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, bet_date, sport, event, market, odds, stake, status, profit,
                       tipster, bookmaker, notes
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE
                ORDER BY id DESC LIMIT %s;
                """,
                (chat_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def _performance_totals(
    chat_id: int,
    start: datetime | None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """Agrega apenas apostas liquidadas dentro da janela informada."""
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    params: list[Any] = [chat_id]
    if start is not None:
        filters.append("COALESCE(settled_at, bet_date) >= %s")
        params.append(start)
    if end is not None:
        filters.append("COALESCE(settled_at, bet_date) < %s")
        params.append(end)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS concluded,
                    COUNT(*) FILTER (WHERE status='GREEN') AS greens,
                    COUNT(*) FILTER (WHERE status='RED') AS reds,
                    COUNT(*) FILTER (WHERE status='HALF_GREEN') AS half_greens,
                    COUNT(*) FILTER (WHERE status='HALF_RED') AS half_reds,
                    COUNT(*) FILTER (WHERE status='VOID') AS voids,
                    COALESCE(SUM(stake),0) AS invested,
                    COALESCE(SUM(profit),0) AS profit,
                    COALESCE(SUM(profit) FILTER (WHERE profit>0),0) AS gross_profit,
                    ABS(COALESCE(SUM(profit) FILTER (WHERE profit<0),0)) AS gross_loss,
                    COALESCE(AVG(odds),0) AS average_odds,
                    COALESCE(AVG(stake),0) AS average_stake
                FROM bets
                WHERE {' AND '.join(filters)};
                """,
                params,
            )
            return dict(cur.fetchone())


def _open_position_totals(chat_id: int) -> dict[str, Any]:
    """Exposição atual, independente do filtro histórico selecionado."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS pending, COALESCE(SUM(stake),0) AS open_exposure
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status='PENDING';
                """,
                (chat_id,),
            )
            return dict(cur.fetchone())


def _enrich_performance_totals(row: dict[str, Any]) -> dict[str, Any]:
    invested = d(row.get("invested"))
    profit = d(row.get("profit"))
    gross_profit = d(row.get("gross_profit"))
    gross_loss = d(row.get("gross_loss"))
    decisions = (
        int(row.get("greens") or 0)
        + int(row.get("reds") or 0)
        + int(row.get("half_greens") or 0)
        + int(row.get("half_reds") or 0)
    )
    wins = int(row.get("greens") or 0) + int(row.get("half_greens") or 0)
    row["roi"] = (profit / invested * 100) if invested else Decimal("0")
    row["win_rate"] = (Decimal(wins) / Decimal(decisions) * 100) if decisions else Decimal("0")
    row["profit_factor"] = (gross_profit / gross_loss) if gross_loss else None
    return row


def summary(chat_id: int, period: str = "all") -> dict[str, Any]:
    settings = get_settings(chat_id)
    start = _period_start(period)
    row = _performance_totals(chat_id, start)
    open_positions = _open_position_totals(chat_id)

    if period == "all":
        row["concluded"] = int(row["concluded"] or 0) + int(settings["base_bets"] or 0)
        row["invested"] = d(row["invested"]) + d(settings["base_staked"])
        row["profit"] = d(row["profit"]) + d(settings["base_profit"])
        if d(settings["base_profit"]) > 0:
            row["gross_profit"] = d(row["gross_profit"]) + d(settings["base_profit"])
        elif d(settings["base_profit"]) < 0:
            row["gross_loss"] = d(row["gross_loss"]) + abs(d(settings["base_profit"]))

    row.update(open_positions)
    _enrich_performance_totals(row)
    row["current_bankroll"] = current_bankroll(chat_id)
    row["currency"] = settings["currency"]
    row["display_name"] = settings.get("display_name") or "Apostador"
    row["max_stake_percent"] = d(settings["max_stake_percent"])
    row["max_open_exposure_percent"] = d(settings["max_open_exposure_percent"])
    # Metadados do resumo histórico importado. Eles são úteis para explicar
    # diferenças entre o lucro consolidado e a série temporal detalhada.
    row["base_bets"] = int(settings.get("base_bets") or 0)
    row["base_staked"] = d(settings.get("base_staked"))
    row["base_profit"] = d(settings.get("base_profit"))
    row["initial_bankroll"] = d(settings.get("initial_bankroll"))
    return row


def _bankroll_before(chat_id: int, start: datetime | None) -> Decimal:
    settings = get_settings(chat_id)
    if start is None:
        return money(d(settings["initial_bankroll"]) + d(settings["base_profit"]))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING'
                  AND COALESCE(settled_at, bet_date)<%s;
                """,
                (chat_id, start),
            )
            profit = d(cur.fetchone()["profit"])
            cur.execute(
                f"""
                SELECT COALESCE(SUM({_transaction_effect_sql()}),0) AS effect
                FROM bankroll_transactions WHERE chat_id=%s AND created_at<%s;
                """,
                (chat_id, start),
            )
            effect = d(cur.fetchone()["effect"])
    return money(d(settings["initial_bankroll"]) + d(settings["base_profit"]) + profit + effect)


def _series_granularity(period: str) -> str:
    if period == "24h":
        return "hour"
    if period in {"7", "30", "90"}:
        return "day"
    if period == "365":
        return "week"
    return "month"


def _floor_bucket(value: datetime, granularity: str) -> datetime:
    """Arredonda um datetime local para o começo do bucket."""
    local = value.astimezone(TIMEZONE)
    if granularity == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start - timedelta(days=day_start.weekday())
    if granularity == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Granularidade inválida: {granularity}")


def _advance_bucket(value: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return value + timedelta(hours=1)
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    if granularity == "month":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)
    raise ValueError(f"Granularidade inválida: {granularity}")


def _bucket_label(value: datetime, granularity: str) -> str:
    if granularity == "hour":
        return value.strftime("%d/%m %Hh")
    if granularity == "day":
        return value.strftime("%d/%m")
    if granularity == "week":
        return f"Sem. {value.strftime('%d/%m')}"
    month_names = (
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    )
    return f"{month_names[value.month - 1]}/{str(value.year)[-2:]}"


def _localize_db_bucket(value: datetime) -> datetime:
    """O PostgreSQL devolve ``timestamp without time zone`` após AT TIME ZONE."""
    if value.tzinfo is None:
        return value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def _performance_series(chat_id: int, period: str) -> dict[str, Any]:
    """Gera uma curva conciliada, contínua e leve para o dashboard.

    Regras importantes:
    - performance usa a data de liquidação, não a data de cadastro;
    - depósitos e saques alteram o saldo, mas não o lucro de apostas;
    - o primeiro ponto é o saldo de abertura, evitando o erro visual em que
      o gráfico começava depois do primeiro lucro do período;
    - buckets sem atividade são preenchidos com zero para preservar a escala
      real do tempo.
    """
    start_utc = _period_start(period)
    granularity = _series_granularity(period)
    now_local = datetime.now(TIMEZONE)

    bet_params: list[Any] = [granularity, TIMEZONE_NAME, chat_id]
    bet_filter = ""
    if start_utc is not None:
        bet_filter = " AND COALESCE(settled_at, bet_date) >= %s"
        bet_params.append(start_utc)

    transaction_params: list[Any] = [granularity, TIMEZONE_NAME, chat_id]
    transaction_filter = ""
    if start_utc is not None:
        transaction_filter = " AND created_at >= %s"
        transaction_params.append(start_utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH movements AS (
                    SELECT
                        date_trunc(%s, COALESCE(settled_at, bet_date) AT TIME ZONE %s) AS bucket,
                        COALESCE(SUM(profit),0) AS profit,
                        0::numeric AS transaction_effect,
                        COUNT(*) AS bets,
                        COALESCE(SUM(stake),0) AS invested
                    FROM bets
                    WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' {bet_filter}
                    GROUP BY bucket

                    UNION ALL

                    SELECT
                        date_trunc(%s, created_at AT TIME ZONE %s) AS bucket,
                        0::numeric AS profit,
                        COALESCE(SUM({_transaction_effect_sql()}),0) AS transaction_effect,
                        0::bigint AS bets,
                        0::numeric AS invested
                    FROM bankroll_transactions
                    WHERE chat_id=%s {transaction_filter}
                    GROUP BY bucket
                )
                SELECT
                    bucket,
                    COALESCE(SUM(profit),0) AS profit,
                    COALESCE(SUM(transaction_effect),0) AS transaction_effect,
                    COALESCE(SUM(bets),0) AS bets,
                    COALESCE(SUM(invested),0) AS invested
                FROM movements
                GROUP BY bucket
                ORDER BY bucket;
                """,
                bet_params + transaction_params,
            )
            raw_rows = [dict(row) for row in cur.fetchall()]

    rows_by_bucket: dict[datetime, dict[str, Any]] = {}
    for row in raw_rows:
        bucket = _localize_db_bucket(row["bucket"])
        rows_by_bucket[bucket] = row

    if start_utc is not None:
        opening_at = start_utc.astimezone(TIMEZONE)
        first_bucket = _floor_bucket(opening_at, granularity)
        opening_balance = _bankroll_before(chat_id, start_utc)
    elif rows_by_bucket:
        first_bucket = min(rows_by_bucket)
        opening_at = first_bucket
        opening_balance = _bankroll_before(chat_id, None)
    else:
        first_bucket = _floor_bucket(now_local, granularity)
        opening_at = first_bucket
        opening_balance = _bankroll_before(chat_id, None)

    last_bucket = _floor_bucket(now_local, granularity)
    balance = money(opening_balance)
    performance_balance = money(opening_balance)
    cumulative_profit = Decimal("0")
    total_profit = Decimal("0")
    total_transactions = Decimal("0")
    total_invested = Decimal("0")
    total_bets = 0
    peak = performance_balance
    max_drawdown = Decimal("0")
    activity_profits: list[Decimal] = []
    points: list[dict[str, Any]] = []

    cursor = first_bucket
    safety = 0
    while cursor <= last_bucket and safety < 2400:
        safety += 1
        row = rows_by_bucket.get(cursor, {})
        profit = money(row.get("profit", 0))
        transaction_effect = money(row.get("transaction_effect", 0))
        invested = money(row.get("invested", 0))
        bets = int(row.get("bets") or 0)

        total_profit += profit
        total_transactions += transaction_effect
        total_invested += invested
        total_bets += bets
        cumulative_profit += profit
        balance = money(balance + profit + transaction_effect)

        # Drawdown deve medir apenas performance de apostas. Aportes e saques
        # não representam ganho ou perda operacional.
        performance_balance = money(performance_balance + profit)
        peak = max(peak, performance_balance)
        max_drawdown = max(max_drawdown, peak - performance_balance)
        if bets:
            activity_profits.append(profit)

        next_bucket = _advance_bucket(cursor, granularity)
        point_at = min(next_bucket, now_local)
        points.append(
            {
                "bucket_start": cursor.isoformat(),
                "bucket_end": point_at.isoformat(),
                "point_at": point_at.isoformat(),
                "label": _bucket_label(cursor, granularity),
                "profit": float(profit),
                "transaction_effect": float(transaction_effect),
                "net_change": float(money(profit + transaction_effect)),
                "invested": float(invested),
                "bets": bets,
                "cumulative_profit": float(money(cumulative_profit)),
                "balance": float(balance),
                "has_activity": bool(bets or transaction_effect),
            }
        )
        cursor = next_bucket

    closing_balance = money(balance)
    net_change = money(closing_balance - opening_balance)
    expected_change = money(total_profit + total_transactions)
    reconciliation_difference = money(net_change - expected_change)
    drawdown_pct = (max_drawdown / peak * 100) if peak > 0 else Decimal("0")

    best_interval = max(activity_profits) if activity_profits else Decimal("0")
    worst_interval = min(activity_profits) if activity_profits else Decimal("0")

    return {
        "points": points,
        "granularity": granularity,
        "opening_at": opening_at.isoformat(),
        "closing_at": now_local.isoformat(),
        "opening_balance": float(money(opening_balance)),
        "closing_balance": float(closing_balance),
        "bet_profit": float(money(total_profit)),
        "transaction_effect": float(money(total_transactions)),
        "net_change": float(net_change),
        "invested": float(money(total_invested)),
        "bets": total_bets,
        "best_interval": float(money(best_interval)),
        "worst_interval": float(money(worst_interval)),
        "max_drawdown": float(money(max_drawdown)),
        "max_drawdown_pct": float(drawdown_pct),
        "has_activity": bool(total_bets or total_transactions),
        "reconciliation_difference": float(reconciliation_difference),
    }


def _breakdown(chat_id: int, period: str, column: str, limit: int = 8) -> list[dict[str, Any]]:
    allowed = {"sport", "tipster", "bookmaker"}
    if column not in allowed:
        raise ValueError("Agrupamento inválido.")
    start = _period_start(period)
    params: list[Any] = [chat_id]
    date_filter = ""
    if start is not None:
        date_filter = " AND COALESCE(settled_at, bet_date) >= %s"
        params.append(start)
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM({column}),''),'Não informado') AS label,
                       COUNT(*) AS bets,
                       COALESCE(SUM(stake),0) AS invested,
                       COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' {date_filter}
                GROUP BY label
                ORDER BY ABS(COALESCE(SUM(profit),0)) DESC
                LIMIT %s;
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    result = []
    for row in rows:
        invested = d(row["invested"])
        profit = d(row["profit"])
        result.append(
            {
                "label": row["label"],
                "bets": int(row["bets"] or 0),
                "invested": float(money(invested)),
                "profit": float(money(profit)),
                "roi": float((profit / invested * 100) if invested else 0),
            }
        )
    return result


def _odds_breakdown(chat_id: int, period: str) -> list[dict[str, Any]]:
    start = _period_start(period)
    params: list[Any] = [chat_id]
    date_filter = ""
    if start is not None:
        date_filter = " AND COALESCE(settled_at, bet_date) >= %s"
        params.append(start)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    CASE
                        WHEN odds < 1.50 THEN 'Abaixo de 1,50'
                        WHEN odds < 2.00 THEN '1,50 a 1,99'
                        WHEN odds < 3.00 THEN '2,00 a 2,99'
                        WHEN odds < 5.00 THEN '3,00 a 4,99'
                        ELSE '5,00 ou mais'
                    END AS label,
                    CASE
                        WHEN odds < 1.50 THEN 1
                        WHEN odds < 2.00 THEN 2
                        WHEN odds < 3.00 THEN 3
                        WHEN odds < 5.00 THEN 4
                        ELSE 5
                    END AS sort_order,
                    COUNT(*) AS bets,
                    COALESCE(SUM(stake),0) AS invested,
                    COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' {date_filter}
                GROUP BY label, sort_order
                ORDER BY sort_order;
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    output = []
    for row in rows:
        invested = d(row["invested"])
        profit = d(row["profit"])
        output.append(
            {
                "label": row["label"],
                "bets": int(row["bets"] or 0),
                "invested": float(money(invested)),
                "profit": float(money(profit)),
                "roi": float((profit / invested * 100) if invested else 0),
            }
        )
    return output


def _previous_comparison(chat_id: int, period: str) -> dict[str, Any] | None:
    bounds = _previous_period_bounds(period)
    if bounds is None:
        return None
    previous = _enrich_performance_totals(_performance_totals(chat_id, bounds[0], bounds[1]))
    return {
        "concluded": int(previous["concluded"] or 0),
        "invested": float(money(previous["invested"])),
        "profit": float(money(previous["profit"])),
        "roi": float(d(previous["roi"])),
        "win_rate": float(d(previous["win_rate"])),
    }


def _current_streak(chat_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status IN ('GREEN','RED','HALF_GREEN','HALF_RED')
                ORDER BY COALESCE(settled_at, bet_date) DESC, id DESC LIMIT 100;
                """,
                (chat_id,),
            )
            statuses = [row["status"] for row in cur.fetchall()]
    if not statuses:
        return {"kind": "NONE", "count": 0}
    first = "WIN" if statuses[0] in {"GREEN", "HALF_GREEN"} else "LOSS"
    count = 0
    for status in statuses:
        kind = "WIN" if status in {"GREEN", "HALF_GREEN"} else "LOSS"
        if kind != first:
            break
        count += 1
    return {"kind": first, "count": count}


def dashboard_data(chat_id: int, period: str = "30") -> dict[str, Any]:
    if period not in VALID_DASHBOARD_PERIODS:
        period = "30"

    data = summary(chat_id, period)
    chart = _performance_series(chat_id, period)
    recent = get_recent_bets(chat_id, 30)
    pending = get_pending_bets(chat_id, 10)
    sports = _breakdown(chat_id, period, "sport", limit=10)
    tipsters = _breakdown(chat_id, period, "tipster", limit=10)
    bookmakers = _breakdown(chat_id, period, "bookmaker", limit=10)
    odds_ranges = _odds_breakdown(chat_id, period)
    previous = _previous_comparison(chat_id, period)
    streak = _current_streak(chat_id)

    bankroll = d(data["current_bankroll"])
    open_exposure = d(data["open_exposure"])
    exposure_pct = (open_exposure / bankroll * 100) if bankroll > 0 else Decimal("0")
    max_drawdown = d(chart["max_drawdown"])
    max_drawdown_pct = d(chart["max_drawdown_pct"])

    insights: list[dict[str, str]] = []
    concluded = int(data["concluded"] or 0)

    if concluded == 0:
        insights.append(
            {
                "level": "info",
                "title": "Sem apostas liquidadas",
                "text": "O período selecionado ainda não possui resultados. Pendentes não entram no lucro nem no ROI.",
            }
        )
    elif concluded < 20:
        insights.append(
            {
                "level": "warning",
                "title": "Amostra pequena",
                "text": f"Há {concluded} aposta(s) concluída(s) no período. Interprete ROI e taxa de acerto com cautela.",
            }
        )

    if exposure_pct > d(data["max_open_exposure_percent"]):
        insights.append(
            {
                "level": "danger",
                "title": "Exposição acima do limite",
                "text": f"A exposição aberta está em {fmt_num(exposure_pct)}% da banca. O limite configurado é {fmt_num(data['max_open_exposure_percent'])}%.",
            }
        )
    elif exposure_pct >= d(data["max_open_exposure_percent"]) * Decimal("0.70") and open_exposure > 0:
        insights.append(
            {
                "level": "warning",
                "title": "Exposição próxima do limite",
                "text": f"As apostas pendentes já comprometem {fmt_num(exposure_pct)}% da banca.",
            }
        )

    if max_drawdown_pct >= Decimal("10"):
        insights.append(
            {
                "level": "warning",
                "title": "Drawdown relevante",
                "text": f"A maior queda desde um pico foi de {fmt_money(max_drawdown, data['currency'])} ({fmt_num(max_drawdown_pct)}%).",
            }
        )

    if d(data["roi"]) < 0 and concluded >= 10:
        insights.append(
            {
                "level": "danger",
                "title": "ROI negativo no período",
                "text": f"O ROI está em {fmt_num(data['roi'])}%. Revise mercados com pior resultado e o tamanho das stakes.",
            }
        )

    qualified_sports = [item for item in sports if int(item["bets"]) >= 3]
    if qualified_sports:
        best = max(qualified_sports, key=lambda item: item["profit"])
        worst = min(qualified_sports, key=lambda item: item["profit"])
        if best["profit"] > 0:
            insights.append(
                {
                    "level": "success",
                    "title": "Melhor categoria com amostra mínima",
                    "text": f"{best['label']} gerou {fmt_money(best['profit'], data['currency'])} em {best['bets']} aposta(s), com ROI de {fmt_num(best['roi'])}%.",
                }
            )
        if worst["profit"] < 0:
            insights.append(
                {
                    "level": "warning",
                    "title": "Maior ponto de atenção",
                    "text": f"{worst['label']} acumula {fmt_money(worst['profit'], data['currency'])} em {worst['bets']} aposta(s).",
                }
            )

    if previous and concluded > 0:
        profit_delta = d(data["profit"]) - d(previous["profit"])
        if profit_delta > 0:
            insights.append(
                {
                    "level": "success",
                    "title": "Melhora contra a janela anterior",
                    "text": f"O resultado aumentou {fmt_money(profit_delta, data['currency'])} em comparação com um período equivalente.",
                }
            )
        elif profit_delta < 0:
            insights.append(
                {
                    "level": "warning",
                    "title": "Queda contra a janela anterior",
                    "text": f"O resultado caiu {fmt_money(abs(profit_delta), data['currency'])} em comparação com um período equivalente.",
                }
            )

    if period == "all" and d(data.get("base_profit")) != 0:
        insights.append(
            {
                "level": "info",
                "title": "Histórico consolidado no saldo inicial",
                "text": f"O gráfico parte de um saldo que já inclui {fmt_money(data['base_profit'], data['currency'])} de resultado histórico sem datas detalhadas.",
            }
        )

    if not insights:
        insights.append(
            {
                "level": "info",
                "title": "Dados consistentes",
                "text": "Nenhum alerta relevante foi identificado para o período selecionado.",
            }
        )

    def serialize_bet(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "date": row["bet_date"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M"),
            "sport": row["sport"],
            "event": row["event"],
            "market": row["market"],
            "odds": float(d(row["odds"])),
            "stake": float(money(row["stake"])),
            "status": row.get("status", "PENDING"),
            "profit": float(money(row.get("profit", 0))),
            "tipster": row.get("tipster") or "—",
            "bookmaker": row.get("bookmaker") or "—",
        }

    profit_factor = data.get("profit_factor")
    base_profit = d(data.get("base_profit"))
    detailed_profit = d(chart["bet_profit"])
    consolidated_difference = money(d(data["profit"]) - detailed_profit)

    return {
        "api_version": 5,
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "period": period,
        "series_granularity": chart["granularity"],
        "profile": {
            "display_name": data["display_name"],
            "currency": data["currency"],
        },
        "summary": {
            "concluded": concluded,
            "pending": int(data["pending"] or 0),
            "greens": int(data["greens"] or 0),
            "reds": int(data["reds"] or 0),
            "half_greens": int(data["half_greens"] or 0),
            "half_reds": int(data["half_reds"] or 0),
            "voids": int(data["voids"] or 0),
            "invested": float(money(data["invested"])),
            "profit": float(money(data["profit"])),
            "detailed_profit": float(money(detailed_profit)),
            "consolidated_difference": float(consolidated_difference),
            "base_profit": float(money(base_profit)),
            "gross_profit": float(money(data["gross_profit"])),
            "gross_loss": float(money(data["gross_loss"])),
            "profit_factor": float(d(profit_factor)) if profit_factor is not None else None,
            "roi": float(d(data["roi"])),
            "win_rate": float(d(data["win_rate"])),
            "average_odds": float(d(data["average_odds"])),
            "average_stake": float(money(data["average_stake"])),
            "current_bankroll": float(money(data["current_bankroll"])),
            "open_exposure": float(money(data["open_exposure"])),
            "open_exposure_pct": float(exposure_pct),
            "max_drawdown": float(max_drawdown),
            "max_drawdown_pct": float(max_drawdown_pct),
            "streak": streak,
        },
        "previous": previous,
        "chart": chart,
        "series": chart["points"],
        "breakdowns": {
            "sports": sports,
            "tipsters": tipsters,
            "bookmakers": bookmakers,
            "odds_ranges": odds_ranges,
        },
        "recent_bets": [serialize_bet(row) for row in recent],
        "pending_bets": [serialize_bet(row) for row in pending],
        "insights": insights[:6],
    }


def risk_warnings(chat_id: int, stake: Decimal) -> list[str]:
    data = summary(chat_id, "all")
    bankroll = d(data["current_bankroll"])
    stake_pct = (d(stake) / bankroll * 100) if bankroll > 0 else Decimal("0")
    exposure_after = d(data["open_exposure"]) + d(stake)
    exposure_pct = (exposure_after / bankroll * 100) if bankroll > 0 else Decimal("0")
    warnings: list[str] = []
    if bankroll <= 0:
        warnings.append("A banca está zerada ou negativa.")
    if stake_pct > d(data["max_stake_percent"]):
        warnings.append(
            f"A stake representa {fmt_num(stake_pct)}% da banca; limite configurado: {fmt_num(data['max_stake_percent'])}%."
        )
    if exposure_pct > d(data["max_open_exposure_percent"]):
        warnings.append(
            f"A exposição aberta poderá chegar a {fmt_num(exposure_pct)}% da banca; limite: {fmt_num(data['max_open_exposure_percent'])}%."
        )
    return warnings


def export_csv_to_tempfile(chat_id: int) -> str:
    fd, path = tempfile.mkstemp(prefix="historico-apostas-", suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow([
            "ID", "Data", "Esporte", "Evento", "Mercado", "Odd", "Stake", "Status",
            "Lucro", "Tipster", "Casa", "Notas",
        ])
        with get_conn() as conn:
            with conn.cursor(name=f"export_{chat_id}") as cur:
                cur.itersize = 500
                cur.execute(
                    """
                    SELECT id, bet_date, sport, event, market, odds, stake, status, profit,
                           tipster, bookmaker, notes
                    FROM bets WHERE chat_id=%s AND is_deleted=FALSE ORDER BY id;
                    """,
                    (chat_id,),
                )
                for row in cur:
                    writer.writerow([
                        row["id"], row["bet_date"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M"),
                        row["sport"], row["event"], row["market"], str(row["odds"]).replace(".", ","),
                        str(row["stake"]).replace(".", ","), row["status"],
                        str(row["profit"]).replace(".", ","), row["tipster"] or "",
                        row["bookmaker"] or "", row["notes"] or "",
                    ])
    return path
