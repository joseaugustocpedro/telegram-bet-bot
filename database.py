from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
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
                    result_at TIMESTAMPTZ,
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
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS result_at TIMESTAMPTZ;",
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
            # Canonicaliza a data analítica de resultados antigos uma única vez.
            cur.execute(
                """
                UPDATE bets
                SET result_at=COALESCE(settled_at, bet_date)
                WHERE status<>'PENDING' AND result_at IS NULL;
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_chat_result_at "
                "ON bets(chat_id, result_at DESC) "
                "WHERE is_deleted=FALSE AND status<>'PENDING';"
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
VALID_DATE_BASES = {"result", "placed"}
RESULT_AT_SQL = "COALESCE(result_at, settled_at, bet_date)"
PLACED_AT_SQL = "bet_date"


@dataclass(frozen=True)
class PeriodWindow:
    key: str
    label: str
    start_utc: datetime | None
    end_utc: datetime
    previous_start_utc: datetime | None
    previous_end_utc: datetime | None

    @property
    def duration(self) -> timedelta | None:
        if self.start_utc is None:
            return None
        return self.end_utc - self.start_utc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_period_window(period: str, *, now: datetime | None = None) -> PeriodWindow:
    """Cria uma única janela temporal usada por todos os cálculos do dashboard.

    As janelas são móveis e exatas:
    - 24h = 24 horas completas;
    - 7/30/90/365 = quantidade exata de dias imediatamente anteriores;
    - all = todo o histórico detalhado até o mesmo instante de corte.

    Usar um único ``end_utc`` evita diferenças de milissegundos entre cartões,
    gráfico, comparações e análises por categoria.
    """
    key = period if period in VALID_DASHBOARD_PERIODS else "30"
    end_utc = _as_utc(now or datetime.now(timezone.utc))
    labels = {
        "24h": "Últimas 24 horas",
        "7": "Últimos 7 dias",
        "30": "Últimos 30 dias",
        "90": "Últimos 90 dias",
        "365": "Último ano (365 dias)",
        "all": "Todo o histórico",
    }
    durations = {
        "24h": timedelta(hours=24),
        "7": timedelta(days=7),
        "30": timedelta(days=30),
        "90": timedelta(days=90),
        "365": timedelta(days=365),
    }
    if key == "all":
        return PeriodWindow(key, labels[key], None, end_utc, None, None)
    duration = durations[key]
    start_utc = end_utc - duration
    return PeriodWindow(
        key=key,
        label=labels[key],
        start_utc=start_utc,
        end_utc=end_utc,
        previous_start_utc=start_utc - duration,
        previous_end_utc=start_utc,
    )


def _normalize_basis(basis: str) -> str:
    return basis if basis in VALID_DATE_BASES else "result"


def _date_expression(basis: str) -> str:
    return PLACED_AT_SQL if _normalize_basis(basis) == "placed" else RESULT_AT_SQL


def _window_conditions(
    expression: str,
    start: datetime | None,
    end: datetime,
    params: list[Any],
) -> list[str]:
    conditions: list[str] = []
    if start is not None:
        conditions.append(f"{expression} >= %s")
        params.append(start)
    conditions.append(f"{expression} < %s")
    params.append(end)
    return conditions
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
                    tipster, bookmaker, notes, settled_at, result_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, bet_date, profit;
                """,
                (
                    chat_id, sport, event, market, odds, stake, status, profit,
                    tipster or None, bookmaker or None, notes or None, settled_at, settled_at,
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
                UPDATE bets SET status=%s, profit=%s, settled_at=NOW(), result_at=NOW(), updated_at=NOW()
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


def _performance_totals_range(
    chat_id: int,
    start: datetime | None,
    end: datetime,
    basis: str,
) -> dict[str, Any]:
    """Agrega somente apostas liquidadas dentro de [start, end)."""
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, start, end, params))
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
    """Exposição neste instante; propositalmente não depende do período."""
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
    row["decisions"] = decisions
    return row


def _bankroll_before(chat_id: int, instant: datetime | None, basis: str = "result") -> Decimal:
    """Saldo imediatamente antes do instante, sem aproximar por bucket."""
    settings = get_settings(chat_id)
    base = d(settings["initial_bankroll"]) + d(settings["base_profit"])
    if instant is None:
        return money(base)
    date_expr = _date_expression(basis)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING'
                  AND {date_expr}<%s;
                """,
                (chat_id, instant),
            )
            profit = d(cur.fetchone()["profit"])
            cur.execute(
                f"""
                SELECT COALESCE(SUM({_transaction_effect_sql()}),0) AS effect
                FROM bankroll_transactions
                WHERE chat_id=%s AND created_at<%s;
                """,
                (chat_id, instant),
            )
            effect = d(cur.fetchone()["effect"])
    return money(base + profit + effect)


def _bankroll_at(chat_id: int, instant: datetime, basis: str = "result") -> Decimal:
    settings = get_settings(chat_id)
    date_expr = _date_expression(basis)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING'
                  AND {date_expr}<%s;
                """,
                (chat_id, instant),
            )
            profit = d(cur.fetchone()["profit"])
            cur.execute(
                f"""
                SELECT COALESCE(SUM({_transaction_effect_sql()}),0) AS effect
                FROM bankroll_transactions
                WHERE chat_id=%s AND created_at<%s;
                """,
                (chat_id, instant),
            )
            effect = d(cur.fetchone()["effect"])
    return money(d(settings["initial_bankroll"]) + d(settings["base_profit"]) + profit + effect)


def summary(
    chat_id: int,
    period: str = "all",
    *,
    window: PeriodWindow | None = None,
    basis: str = "result",
) -> dict[str, Any]:
    """Resumo coerente com a mesma janela usada pelo gráfico e breakdowns."""
    window = window or build_period_window(period)
    basis = _normalize_basis(basis)
    settings = get_settings(chat_id)
    detailed = _performance_totals_range(chat_id, window.start_utc, window.end_utc, basis)
    row = dict(detailed)

    row["detailed_concluded"] = int(detailed.get("concluded") or 0)
    row["detailed_invested"] = d(detailed.get("invested"))
    row["detailed_profit"] = d(detailed.get("profit"))

    imported_bets = int(settings.get("base_bets") or 0)
    imported_staked = d(settings.get("base_staked"))
    imported_profit = d(settings.get("base_profit"))
    if window.key == "all":
        row["concluded"] = int(row.get("concluded") or 0) + imported_bets
        row["invested"] = d(row.get("invested")) + imported_staked
        row["profit"] = d(row.get("profit")) + imported_profit
        if imported_profit > 0:
            row["gross_profit"] = d(row.get("gross_profit")) + imported_profit
        elif imported_profit < 0:
            row["gross_loss"] = d(row.get("gross_loss")) + abs(imported_profit)

    row.update(_open_position_totals(chat_id))
    _enrich_performance_totals(row)
    row["current_bankroll"] = _bankroll_at(chat_id, window.end_utc, basis)
    row["currency"] = settings["currency"]
    row["display_name"] = settings.get("display_name") or "Apostador"
    row["max_stake_percent"] = d(settings["max_stake_percent"])
    row["max_open_exposure_percent"] = d(settings["max_open_exposure_percent"])
    row["base_bets"] = imported_bets
    row["base_staked"] = imported_staked
    row["base_profit"] = imported_profit
    row["initial_bankroll"] = d(settings.get("initial_bankroll"))
    return row


def _series_granularity(period: str) -> str:
    if period == "24h":
        return "hour"
    if period in {"7", "30", "90"}:
        return "day"
    if period == "365":
        return "week"
    return "month"


def _floor_bucket(value: datetime, granularity: str) -> datetime:
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
    month_names = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
    return f"{month_names[value.month - 1]}/{str(value.year)[-2:]}"


def _localize_db_bucket(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def _exact_risk_metrics(
    chat_id: int,
    window: PeriodWindow,
    basis: str,
    opening_balance: Decimal,
) -> dict[str, Any]:
    """Calcula drawdown e sequências aposta por aposta, não por dia/mês."""
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, params))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status, profit, {date_expr} AS analysis_at, id
                FROM bets
                WHERE {' AND '.join(filters)}
                ORDER BY {date_expr}, id;
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]

    equity = money(opening_balance)
    peak = equity
    max_drawdown = Decimal("0")
    current_kind = "NONE"
    current_count = 0
    longest_win = 0
    longest_loss = 0
    running_kind = "NONE"
    running_count = 0

    for row in rows:
        equity = money(equity + d(row["profit"]))
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        status = row["status"]
        if status == "VOID":
            continue
        kind = "WIN" if status in {"GREEN", "HALF_GREEN"} else "LOSS"
        if kind == running_kind:
            running_count += 1
        else:
            running_kind = kind
            running_count = 1
        if kind == "WIN":
            longest_win = max(longest_win, running_count)
        else:
            longest_loss = max(longest_loss, running_count)

    for row in reversed(rows):
        status = row["status"]
        if status == "VOID":
            continue
        kind = "WIN" if status in {"GREEN", "HALF_GREEN"} else "LOSS"
        if current_kind == "NONE":
            current_kind = kind
        if kind != current_kind:
            break
        current_count += 1

    max_drawdown_pct = (max_drawdown / peak * 100) if peak > 0 else Decimal("0")
    return {
        "max_drawdown": money(max_drawdown),
        "max_drawdown_pct": max_drawdown_pct,
        "streak": {"kind": current_kind, "count": current_count},
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
    }


def _performance_series(chat_id: int, window: PeriodWindow, basis: str) -> dict[str, Any]:
    """Série contínua usando exatamente a mesma janela dos KPIs."""
    basis = _normalize_basis(basis)
    date_expr = _date_expression(basis)
    granularity = _series_granularity(window.key)
    end_local = window.end_utc.astimezone(TIMEZONE)

    bet_params: list[Any] = [granularity, TIMEZONE_NAME, chat_id]
    bet_filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    bet_filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, bet_params))

    tx_params: list[Any] = [granularity, TIMEZONE_NAME, chat_id]
    tx_filters = ["chat_id=%s"]
    tx_filters.extend(_window_conditions("created_at", window.start_utc, window.end_utc, tx_params))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    date_trunc(%s, {date_expr} AT TIME ZONE %s) AS bucket,
                    COALESCE(SUM(profit),0) AS profit,
                    COUNT(*) AS bets,
                    COALESCE(SUM(stake),0) AS invested
                FROM bets
                WHERE {' AND '.join(bet_filters)}
                GROUP BY bucket ORDER BY bucket;
                """,
                bet_params,
            )
            bet_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                f"""
                SELECT
                    date_trunc(%s, created_at AT TIME ZONE %s) AS bucket,
                    COALESCE(SUM({_transaction_effect_sql()}),0) AS transaction_effect
                FROM bankroll_transactions
                WHERE {' AND '.join(tx_filters)}
                GROUP BY bucket ORDER BY bucket;
                """,
                tx_params,
            )
            tx_rows = [dict(row) for row in cur.fetchall()]

    rows_by_bucket: dict[datetime, dict[str, Any]] = {}
    for row in bet_rows:
        bucket = _localize_db_bucket(row["bucket"])
        rows_by_bucket[bucket] = {
            "profit": d(row["profit"]),
            "transaction_effect": Decimal("0"),
            "bets": int(row["bets"] or 0),
            "invested": d(row["invested"]),
        }
    for row in tx_rows:
        bucket = _localize_db_bucket(row["bucket"])
        target = rows_by_bucket.setdefault(
            bucket,
            {"profit": Decimal("0"), "transaction_effect": Decimal("0"), "bets": 0, "invested": Decimal("0")},
        )
        target["transaction_effect"] = d(row["transaction_effect"])

    if window.start_utc is not None:
        opening_at = window.start_utc.astimezone(TIMEZONE)
        first_bucket = _floor_bucket(opening_at, granularity)
        opening_balance = _bankroll_before(chat_id, window.start_utc, basis)
    elif rows_by_bucket:
        first_bucket = min(rows_by_bucket)
        opening_at = first_bucket
        opening_balance = _bankroll_before(chat_id, None, basis)
    else:
        first_bucket = _floor_bucket(end_local, granularity)
        opening_at = first_bucket
        opening_balance = _bankroll_before(chat_id, None, basis)

    last_bucket = _floor_bucket(end_local, granularity)
    balance = money(opening_balance)
    cumulative_profit = Decimal("0")
    total_profit = Decimal("0")
    total_transactions = Decimal("0")
    total_invested = Decimal("0")
    total_bets = 0
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
        if bets:
            activity_profits.append(profit)
        next_bucket = _advance_bucket(cursor, granularity)
        point_at = min(next_bucket, end_local)
        points.append(
            {
                "bucket_start": max(cursor, opening_at).isoformat(),
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
    risk = _exact_risk_metrics(chat_id, window, basis, opening_balance)
    best_interval = max(activity_profits) if activity_profits else Decimal("0")
    worst_interval = min(activity_profits) if activity_profits else Decimal("0")

    return {
        "points": points,
        "granularity": granularity,
        "opening_at": opening_at.isoformat(),
        "closing_at": end_local.isoformat(),
        "opening_balance": float(money(opening_balance)),
        "closing_balance": float(closing_balance),
        "bet_profit": float(money(total_profit)),
        "transaction_effect": float(money(total_transactions)),
        "net_change": float(net_change),
        "invested": float(money(total_invested)),
        "bets": total_bets,
        "best_interval": float(money(best_interval)),
        "worst_interval": float(money(worst_interval)),
        "max_drawdown": float(risk["max_drawdown"]),
        "max_drawdown_pct": float(risk["max_drawdown_pct"]),
        "streak": risk["streak"],
        "longest_win_streak": risk["longest_win_streak"],
        "longest_loss_streak": risk["longest_loss_streak"],
        "has_activity": bool(total_bets or total_transactions),
        "reconciliation_difference": float(reconciliation_difference),
    }


def _breakdown(
    chat_id: int,
    window: PeriodWindow,
    basis: str,
    column: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    allowed = {"sport", "tipster", "bookmaker", "market"}
    if column not in allowed:
        raise ValueError("Dimensão inválida.")
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, params))
    params.append(max(1, min(20, int(limit))))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM({column}),''),'Não informado') AS label,
                       COUNT(*) AS bets,
                       COALESCE(SUM(stake),0) AS invested,
                       COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE {' AND '.join(filters)}
                GROUP BY label
                ORDER BY ABS(COALESCE(SUM(profit),0)) DESC
                LIMIT %s;
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]
    output: list[dict[str, Any]] = []
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


def _odds_breakdown(chat_id: int, window: PeriodWindow, basis: str) -> list[dict[str, Any]]:
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, params))
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
                        WHEN odds < 1.50 THEN 1 WHEN odds < 2.00 THEN 2
                        WHEN odds < 3.00 THEN 3 WHEN odds < 5.00 THEN 4 ELSE 5
                    END AS sort_order,
                    COUNT(*) AS bets,
                    COALESCE(SUM(stake),0) AS invested,
                    COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE {' AND '.join(filters)}
                GROUP BY label, sort_order ORDER BY sort_order;
                """,
                params,
            )
            rows = [dict(row) for row in cur.fetchall()]
    output: list[dict[str, Any]] = []
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


def _previous_comparison(chat_id: int, window: PeriodWindow, basis: str) -> dict[str, Any] | None:
    if window.previous_start_utc is None or window.previous_end_utc is None:
        return None
    previous = _enrich_performance_totals(
        _performance_totals_range(
            chat_id,
            window.previous_start_utc,
            window.previous_end_utc,
            basis,
        )
    )
    return {
        "concluded": int(previous["concluded"] or 0),
        "invested": float(money(previous["invested"])),
        "profit": float(money(previous["profit"])),
        "roi": float(d(previous["roi"])),
        "win_rate": float(d(previous["win_rate"])),
    }


def _period_bets(
    chat_id: int,
    window: PeriodWindow,
    basis: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, params))
    params.append(max(1, min(100, int(limit))))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, bet_date, settled_at, result_at, {date_expr} AS analysis_at,
                       sport, event, market, odds, stake, status, profit,
                       tipster, bookmaker, notes
                FROM bets
                WHERE {' AND '.join(filters)}
                ORDER BY {date_expr} DESC, id DESC LIMIT %s;
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def _data_quality(chat_id: int, window: PeriodWindow, basis: str, settings: dict[str, Any]) -> dict[str, Any]:
    date_expr = _date_expression(basis)
    params: list[Any] = [chat_id]
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    filters.extend(_window_conditions(date_expr, window.start_utc, window.end_utc, params))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS detailed_rows,
                    COUNT(*) FILTER (WHERE settled_at IS NULL) AS legacy_without_settled_at,
                    COUNT(*) FILTER (WHERE result_at IS NULL) AS missing_result_at,
                    MIN({date_expr}) AS first_analysis_at,
                    MAX({date_expr}) AS last_analysis_at
                FROM bets
                WHERE {' AND '.join(filters)};
                """,
                params,
            )
            row = dict(cur.fetchone())
            cur.execute(
                f"""
                SELECT COUNT(*) AS future_rows
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING'
                  AND {date_expr}>=%s;
                """,
                (chat_id, window.end_utc),
            )
            future_rows = int(cur.fetchone()["future_rows"] or 0)
    imported_bets = int(settings.get("base_bets") or 0)
    detailed_rows = int(row.get("detailed_rows") or 0)
    total_known = detailed_rows + (imported_bets if window.key == "all" else 0)
    coverage = (Decimal(detailed_rows) / Decimal(total_known) * 100) if total_known else Decimal("100")
    return {
        "detailed_rows": detailed_rows,
        "legacy_without_settled_at": int(row.get("legacy_without_settled_at") or 0),
        "missing_result_at": int(row.get("missing_result_at") or 0),
        "future_rows": future_rows,
        "imported_bets_without_dates": imported_bets if window.key == "all" else 0,
        "analytical_coverage_pct": float(coverage),
        "first_analysis_at": row.get("first_analysis_at").astimezone(TIMEZONE).isoformat() if row.get("first_analysis_at") else None,
        "last_analysis_at": row.get("last_analysis_at").astimezone(TIMEZONE).isoformat() if row.get("last_analysis_at") else None,
    }


def _serialize_bet(row: dict[str, Any]) -> dict[str, Any]:
    def local_iso(value: Any) -> str | None:
        if not value:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(TIMEZONE).isoformat()

    analysis_at = row.get("analysis_at") or row.get("result_at") or row.get("settled_at") or row.get("bet_date")
    return {
        "id": int(row["id"]),
        "date": analysis_at.astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M") if analysis_at else "—",
        "analysis_at": local_iso(analysis_at),
        "placed_at": local_iso(row.get("bet_date")),
        "settled_at": local_iso(row.get("result_at") or row.get("settled_at")),
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


def _range_label(window: PeriodWindow) -> str:
    end_local = window.end_utc.astimezone(TIMEZONE)
    if window.start_utc is None:
        return f"até {end_local.strftime('%d/%m/%Y %H:%M')}"
    start_local = window.start_utc.astimezone(TIMEZONE)
    return f"{start_local.strftime('%d/%m/%Y %H:%M')} → {end_local.strftime('%d/%m/%Y %H:%M')}"


def dashboard_data(chat_id: int, period: str = "30", basis: str = "result") -> dict[str, Any]:
    basis = _normalize_basis(basis)
    window = build_period_window(period, now=datetime.now(timezone.utc))
    settings = get_settings(chat_id)
    data = summary(chat_id, window.key, window=window, basis=basis)
    chart = _performance_series(chat_id, window, basis)
    period_bets = _period_bets(chat_id, window, basis, 50)
    pending = get_pending_bets(chat_id, 10)
    sports = _breakdown(chat_id, window, basis, "sport", limit=10)
    tipsters = _breakdown(chat_id, window, basis, "tipster", limit=10)
    bookmakers = _breakdown(chat_id, window, basis, "bookmaker", limit=10)
    markets = _breakdown(chat_id, window, basis, "market", limit=10)
    odds_ranges = _odds_breakdown(chat_id, window, basis)
    previous = _previous_comparison(chat_id, window, basis)
    quality = _data_quality(chat_id, window, basis, settings)

    bankroll = d(data["current_bankroll"])
    open_exposure = d(data["open_exposure"])
    exposure_pct = (open_exposure / bankroll * 100) if bankroll > 0 else Decimal("0")
    max_drawdown = d(chart["max_drawdown"])
    max_drawdown_pct = d(chart["max_drawdown_pct"])
    concluded = int(data["concluded"] or 0)
    detailed_concluded = int(data["detailed_concluded"] or 0)

    insights: list[dict[str, str]] = []
    if detailed_concluded == 0:
        insights.append({"level": "info", "title": "Sem resultados detalhados", "text": "Nenhuma aposta liquidada caiu exatamente dentro da janela selecionada."})
    elif detailed_concluded < 20:
        insights.append({"level": "warning", "title": "Amostra pequena", "text": f"Há {detailed_concluded} resultado(s) detalhado(s) na janela. Interprete ROI e taxa de acerto com cautela."})
    if quality["legacy_without_settled_at"]:
        insights.append({"level": "warning", "title": "Datas históricas aproximadas", "text": f"{quality['legacy_without_settled_at']} registro(s) antigo(s) não têm data de liquidação; nesses casos, o sistema usa a data da aposta."})
    if quality["future_rows"]:
        insights.append({"level": "danger", "title": "Datas futuras detectadas", "text": f"Há {quality['future_rows']} resultado(s) com data posterior ao instante atual. Eles não entram nos totais."})
    if exposure_pct > d(data["max_open_exposure_percent"]):
        insights.append({"level": "danger", "title": "Exposição acima do limite", "text": f"A exposição atual está em {fmt_num(exposure_pct)}% da banca; o limite configurado é {fmt_num(data['max_open_exposure_percent'])}%."})
    if max_drawdown_pct >= Decimal("10"):
        insights.append({"level": "warning", "title": "Drawdown relevante", "text": f"A maior queda aposta a aposta foi de {fmt_money(max_drawdown, data['currency'])} ({fmt_num(max_drawdown_pct)}%)."})
    if d(data["roi"]) < 0 and detailed_concluded >= 10:
        insights.append({"level": "danger", "title": "ROI negativo", "text": f"O ROI da janela está em {fmt_num(data['roi'])}%. Revise as categorias com pior resultado."})
    qualified_sports = [item for item in sports if int(item["bets"]) >= 3]
    if qualified_sports:
        best = max(qualified_sports, key=lambda item: item["profit"])
        worst = min(qualified_sports, key=lambda item: item["profit"])
        if best["profit"] > 0:
            insights.append({"level": "success", "title": "Melhor esporte", "text": f"{best['label']} gerou {fmt_money(best['profit'], data['currency'])} em {best['bets']} aposta(s), ROI de {fmt_num(best['roi'])}%."})
        if worst["profit"] < 0:
            insights.append({"level": "warning", "title": "Ponto de atenção", "text": f"{worst['label']} acumula {fmt_money(worst['profit'], data['currency'])} em {worst['bets']} aposta(s)."})
    if previous and detailed_concluded > 0:
        delta = d(data["detailed_profit"]) - d(previous["profit"])
        if delta > 0:
            insights.append({"level": "success", "title": "Melhora vs. janela anterior", "text": f"O lucro detalhado aumentou {fmt_money(delta, data['currency'])} em uma janela anterior de duração idêntica."})
        elif delta < 0:
            insights.append({"level": "warning", "title": "Queda vs. janela anterior", "text": f"O lucro detalhado caiu {fmt_money(abs(delta), data['currency'])} frente à janela anterior equivalente."})
    if window.key == "all" and d(data.get("base_profit")) != 0:
        insights.append({"level": "info", "title": "Histórico importado", "text": f"Há {fmt_money(data['base_profit'], data['currency'])} de resultado consolidado sem datas individuais. Ele entra nos totais de 'Tudo', mas não pode ser distribuído no gráfico."})
    if not insights:
        insights.append({"level": "success", "title": "Dados consistentes", "text": "Cartões, gráfico, comparações e categorias usam a mesma janela e o mesmo instante de corte."})

    profit_factor = data.get("profit_factor")
    duration = window.duration
    return {
        "api_version": 6,
        "generated_at": window.end_utc.astimezone(TIMEZONE).isoformat(),
        "period": {
            "key": window.key,
            "label": window.label,
            "start": window.start_utc.astimezone(TIMEZONE).isoformat() if window.start_utc else None,
            "end": window.end_utc.astimezone(TIMEZONE).isoformat(),
            "previous_start": window.previous_start_utc.astimezone(TIMEZONE).isoformat() if window.previous_start_utc else None,
            "previous_end": window.previous_end_utc.astimezone(TIMEZONE).isoformat() if window.previous_end_utc else None,
            "duration_hours": float(Decimal(str(duration.total_seconds())) / Decimal("3600")) if duration else None,
            "range_label": _range_label(window),
            "is_all": window.start_utc is None,
        },
        "basis": {
            "key": basis,
            "label": "Data do resultado" if basis == "result" else "Data da aposta",
            "description": "Lucro entra quando a aposta foi liquidada." if basis == "result" else "Lucro é atribuído ao momento em que a aposta foi registrada.",
        },
        "series_granularity": chart["granularity"],
        "profile": {"display_name": data["display_name"], "currency": data["currency"]},
        "summary": {
            "concluded": concluded,
            "detailed_concluded": detailed_concluded,
            "pending": int(data["pending"] or 0),
            "greens": int(data["greens"] or 0),
            "reds": int(data["reds"] or 0),
            "half_greens": int(data["half_greens"] or 0),
            "half_reds": int(data["half_reds"] or 0),
            "voids": int(data["voids"] or 0),
            "invested": float(money(data["invested"])),
            "detailed_invested": float(money(data["detailed_invested"])),
            "profit": float(money(data["profit"])),
            "detailed_profit": float(money(data["detailed_profit"])),
            "base_bets": int(data["base_bets"]),
            "base_staked": float(money(data["base_staked"])),
            "base_profit": float(money(data["base_profit"])),
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
            "streak": chart["streak"],
            "longest_win_streak": chart["longest_win_streak"],
            "longest_loss_streak": chart["longest_loss_streak"],
        },
        "previous": previous,
        "chart": chart,
        "series": chart["points"],
        "breakdowns": {
            "sports": sports,
            "tipsters": tipsters,
            "bookmakers": bookmakers,
            "markets": markets,
            "odds_ranges": odds_ranges,
        },
        "period_bets": [_serialize_bet(row) for row in period_bets],
        "recent_bets": [_serialize_bet(row) for row in period_bets],
        "pending_bets": [_serialize_bet(row) for row in pending],
        "quality": quality,
        "insights": insights[:7],
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
