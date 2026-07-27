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


def _period_start(period: str) -> datetime | None:
    now = datetime.now(TIMEZONE)
    if period == "all":
        return None
    days = int(period)
    start_local = datetime.combine(now.date() - timedelta(days=days - 1), time.min, tzinfo=TIMEZONE)
    return start_local.astimezone(timezone.utc)


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


def summary(chat_id: int, period: str = "all") -> dict[str, Any]:
    settings = get_settings(chat_id)
    start = _period_start(period)
    params: list[Any] = [chat_id]
    date_filter = ""
    if start is not None:
        date_filter = " AND bet_date >= %s"
        params.append(start)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE status<>'PENDING') AS concluded,
                    COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                    COUNT(*) FILTER (WHERE status='GREEN') AS greens,
                    COUNT(*) FILTER (WHERE status='RED') AS reds,
                    COUNT(*) FILTER (WHERE status='HALF_GREEN') AS half_greens,
                    COUNT(*) FILTER (WHERE status='HALF_RED') AS half_reds,
                    COUNT(*) FILTER (WHERE status='VOID') AS voids,
                    COALESCE(SUM(stake) FILTER (WHERE status<>'PENDING'),0) AS invested,
                    COALESCE(SUM(stake) FILTER (WHERE status='PENDING'),0) AS open_exposure,
                    COALESCE(SUM(profit),0) AS profit,
                    COALESCE(AVG(odds) FILTER (WHERE status<>'PENDING'),0) AS average_odds,
                    COALESCE(AVG(stake) FILTER (WHERE status<>'PENDING'),0) AS average_stake
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE {date_filter};
                """,
                params,
            )
            row = dict(cur.fetchone())
    if period == "all":
        row["concluded"] = int(row["concluded"] or 0) + int(settings["base_bets"] or 0)
        row["invested"] = d(row["invested"]) + d(settings["base_staked"])
        row["profit"] = d(row["profit"]) + d(settings["base_profit"])
    invested = d(row["invested"])
    profit = d(row["profit"])
    decisions = int(row["greens"] or 0) + int(row["reds"] or 0) + int(row["half_greens"] or 0) + int(row["half_reds"] or 0)
    wins = int(row["greens"] or 0) + int(row["half_greens"] or 0)
    row["roi"] = (profit / invested * 100) if invested else Decimal("0")
    row["win_rate"] = (Decimal(wins) / Decimal(decisions) * 100) if decisions else Decimal("0")
    row["current_bankroll"] = current_bankroll(chat_id)
    row["currency"] = settings["currency"]
    row["display_name"] = settings.get("display_name") or "Apostador"
    row["max_stake_percent"] = d(settings["max_stake_percent"])
    row["max_open_exposure_percent"] = d(settings["max_open_exposure_percent"])
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
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND bet_date<%s;
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


def _daily_series(chat_id: int, period: str) -> tuple[list[dict[str, Any]], Decimal, Decimal]:
    start = _period_start(period)
    params: list[Any] = [TIMEZONE_NAME, chat_id]
    date_filter = ""
    if start is not None:
        date_filter = " AND bet_date >= %s"
        params.append(start)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT (bet_date AT TIME ZONE %s)::date AS day, COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' {date_filter}
                GROUP BY day ORDER BY day;
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    balance = _bankroll_before(chat_id, start)
    peak = balance
    max_drawdown = Decimal("0")
    output: list[dict[str, Any]] = []
    for row in rows:
        balance = money(balance + d(row["profit"]))
        peak = max(peak, balance)
        drawdown = peak - balance
        max_drawdown = max(max_drawdown, drawdown)
        output.append({
            "day": row["day"].isoformat(),
            "profit": float(money(row["profit"])),
            "balance": float(balance),
        })
    drawdown_pct = (max_drawdown / peak * 100) if peak else Decimal("0")
    return output, money(max_drawdown), drawdown_pct


def _breakdown(chat_id: int, period: str, column: str, limit: int = 8) -> list[dict[str, Any]]:
    allowed = {"sport", "tipster", "bookmaker"}
    if column not in allowed:
        raise ValueError("Agrupamento inválido.")
    start = _period_start(period)
    params: list[Any] = [chat_id]
    date_filter = ""
    if start is not None:
        date_filter = " AND bet_date >= %s"
        params.append(start)
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM({column}),''),'Não informado') AS label,
                       COUNT(*) FILTER (WHERE status<>'PENDING') AS bets,
                       COALESCE(SUM(stake) FILTER (WHERE status<>'PENDING'),0) AS invested,
                       COALESCE(SUM(profit),0) AS profit
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE {date_filter}
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
        result.append({
            "label": row["label"],
            "bets": int(row["bets"] or 0),
            "invested": float(money(invested)),
            "profit": float(money(profit)),
            "roi": float((profit / invested * 100) if invested else 0),
        })
    return result


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
    if period not in {"7", "30", "90", "365", "all"}:
        period = "30"
    data = summary(chat_id, period)
    series, max_drawdown, max_drawdown_pct = _daily_series(chat_id, period)
    recent = get_recent_bets(chat_id, 12)
    pending = get_pending_bets(chat_id, 8)
    sports = _breakdown(chat_id, period, "sport")
    tipsters = _breakdown(chat_id, period, "tipster")
    bookmakers = _breakdown(chat_id, period, "bookmaker")
    streak = _current_streak(chat_id)
    bankroll = d(data["current_bankroll"])
    open_exposure = d(data["open_exposure"])
    exposure_pct = (open_exposure / bankroll * 100) if bankroll > 0 else Decimal("0")

    insights: list[dict[str, str]] = []
    if exposure_pct > d(data["max_open_exposure_percent"]):
        insights.append({"level": "warning", "text": f"Exposição aberta em {fmt_num(exposure_pct)}% da banca, acima do limite configurado."})
    if d(data["roi"]) < 0 and int(data["concluded"] or 0) >= 10:
        insights.append({"level": "danger", "text": f"O ROI do período está negativo em {fmt_num(data['roi'])}%. Revise mercados e tamanho das stakes."})
    if sports:
        best = max(sports, key=lambda item: item["profit"])
        worst = min(sports, key=lambda item: item["profit"])
        if best["profit"] > 0:
            insights.append({"level": "success", "text": f"Melhor esporte do período: {best['label']} ({fmt_money(best['profit'], data['currency'])})."})
        if worst["profit"] < 0:
            insights.append({"level": "warning", "text": f"Maior perda por esporte: {worst['label']} ({fmt_money(worst['profit'], data['currency'])})."})
    if not insights:
        insights.append({"level": "info", "text": "Registre mais apostas para liberar insights comparativos."})

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

    return {
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "period": period,
        "profile": {"display_name": data["display_name"], "currency": data["currency"]},
        "summary": {
            "concluded": int(data["concluded"] or 0),
            "pending": int(data["pending"] or 0),
            "greens": int(data["greens"] or 0),
            "reds": int(data["reds"] or 0),
            "voids": int(data["voids"] or 0),
            "invested": float(money(data["invested"])),
            "profit": float(money(data["profit"])),
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
        "series": series,
        "breakdowns": {"sports": sports, "tipsters": tipsters, "bookmakers": bookmakers},
        "recent_bets": [serialize_bet(row) for row in recent],
        "pending_bets": [serialize_bet(row) for row in pending],
        "insights": insights[:4],
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
