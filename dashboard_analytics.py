from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

try:
    from .config import TIMEZONE, TIMEZONE_NAME
    from .database import d, ensure_settings, get_conn, money
except ImportError:  # execução direta durante desenvolvimento
    from config import TIMEZONE, TIMEZONE_NAME
    from database import d, ensure_settings, get_conn, money


VALID_PERIODS = {"24h", "7", "30", "90", "365", "all"}
MONEY_QUANT = Decimal("0.01")


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start_utc: datetime | None
    end_utc: datetime
    previous_start_utc: datetime | None
    previous_end_utc: datetime | None
    granularity: str

    @property
    def duration(self) -> timedelta | None:
        return None if self.start_utc is None else self.end_utc - self.start_utc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_window(period: str, *, now: datetime | None = None) -> Window:
    """Cria a única janela usada por todos os blocos do dashboard.

    Os períodos são janelas móveis exatas, e não "dias de calendário":
    24h = 24 horas; 7 = 168 horas; 30 = 720 horas; etc.
    """
    key = period if period in VALID_PERIODS else "30"
    end = _utc(now or datetime.now(timezone.utc))
    labels = {
        "24h": "Últimas 24 horas",
        "7": "Últimos 7 dias",
        "30": "Últimos 30 dias",
        "90": "Últimos 90 dias",
        "365": "Últimos 365 dias",
        "all": "Todo o histórico",
    }
    granularities = {
        "24h": "hour",
        "7": "day",
        "30": "day",
        "90": "week",
        "365": "month",
        "all": "month",
    }
    if key == "all":
        return Window(key, labels[key], None, end, None, None, granularities[key])

    duration = {
        "24h": timedelta(hours=24),
        "7": timedelta(days=7),
        "30": timedelta(days=30),
        "90": timedelta(days=90),
        "365": timedelta(days=365),
    }[key]
    start = end - duration
    return Window(
        key=key,
        label=labels[key],
        start_utc=start,
        end_utc=end,
        previous_start_utc=start - duration,
        previous_end_utc=start,
        granularity=granularities[key],
    )


def initialize_dashboard_analytics() -> None:
    """Cria apenas índices seguros. Não reescreve datas históricas."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bets_dashboard_settled
                ON bets(chat_id, settled_at DESC)
                WHERE is_deleted=FALSE
                  AND status<>'PENDING'
                  AND settled_at IS NOT NULL;
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bets_dashboard_legacy
                ON bets(chat_id, bet_date DESC)
                WHERE is_deleted=FALSE
                  AND status<>'PENDING'
                  AND settled_at IS NULL;
                """
            )


def _q(value: Any) -> Decimal:
    return d(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


def _status_metrics(row: dict[str, Any]) -> dict[str, Any]:
    invested = d(row.get("invested"))
    profit = d(row.get("profit"))
    gross_profit = d(row.get("gross_profit"))
    gross_loss = d(row.get("gross_loss"))
    greens = int(row.get("greens") or 0)
    reds = int(row.get("reds") or 0)
    half_greens = int(row.get("half_greens") or 0)
    half_reds = int(row.get("half_reds") or 0)
    decisions = greens + reds + half_greens + half_reds
    wins = greens + half_greens
    return {
        "concluded": int(row.get("concluded") or 0),
        "greens": greens,
        "reds": reds,
        "half_greens": half_greens,
        "half_reds": half_reds,
        "voids": int(row.get("voids") or 0),
        "decisions": decisions,
        "invested": float(_q(invested)),
        "profit": float(_q(profit)),
        "gross_profit": float(_q(gross_profit)),
        "gross_loss": float(_q(gross_loss)),
        "roi": float((profit / invested * 100) if invested else 0),
        "win_rate": float((Decimal(wins) / Decimal(decisions) * 100) if decisions else 0),
        "average_odds": float(d(row.get("average_odds"))),
        "average_stake": float(_q(row.get("average_stake"))),
        "profit_factor": float(gross_profit / gross_loss) if gross_loss else None,
    }


def _performance_sql(where: str) -> str:
    return f"""
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
        WHERE {where};
    """


def _exact_where(window: Window, *, alias: str = "") -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    filters = [
        f"{p}chat_id=%s",
        f"{p}is_deleted=FALSE",
        f"{p}status<>'PENDING'",
        f"{p}settled_at IS NOT NULL",
        f"{p}settled_at<%s",
    ]
    params: list[Any] = [None, window.end_utc]
    if window.start_utc is not None:
        filters.append(f"{p}settled_at>=%s")
        params.append(window.start_utc)
    return " AND ".join(filters), params


def _fill_chat_id(params: list[Any], chat_id: int) -> list[Any]:
    params = list(params)
    params[0] = chat_id
    return params


def _previous_totals(cur: Any, chat_id: int, window: Window) -> dict[str, Any] | None:
    if window.previous_start_utc is None or window.previous_end_utc is None:
        return None
    where = (
        "chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' "
        "AND settled_at IS NOT NULL AND settled_at>=%s AND settled_at<%s"
    )
    cur.execute(_performance_sql(where), (chat_id, window.previous_start_utc, window.previous_end_utc))
    return _status_metrics(_row_dict(cur.fetchone()))


def _global_state(cur: Any, chat_id: int, settings: dict[str, Any], at: datetime) -> dict[str, Any]:
    cur.execute(
        """
        SELECT
            COALESCE(SUM(profit) FILTER (
                WHERE status<>'PENDING' AND COALESCE(settled_at, bet_date)<%s
            ),0) AS detailed_profit,
            COUNT(*) FILTER (
                WHERE status<>'PENDING' AND COALESCE(settled_at, bet_date)<%s
            ) AS detailed_concluded,
            COALESCE(SUM(stake) FILTER (WHERE status='PENDING'),0) AS open_exposure,
            COUNT(*) FILTER (WHERE status='PENDING') AS pending
        FROM bets
        WHERE chat_id=%s AND is_deleted=FALSE;
        """,
        (at, at, chat_id),
    )
    bets = _row_dict(cur.fetchone())
    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind='WITHDRAWAL' THEN -amount ELSE amount END),0) AS effect,
            COALESCE(SUM(amount) FILTER (WHERE kind IN ('DEPOSIT','BONUS','ADJUSTMENT')),0) AS inflow,
            COALESCE(SUM(amount) FILTER (WHERE kind='WITHDRAWAL'),0) AS outflow
        FROM bankroll_transactions
        WHERE chat_id=%s AND created_at<%s;
        """,
        (chat_id, at),
    )
    tx = _row_dict(cur.fetchone())
    initial = d(settings.get("initial_bankroll"))
    base_profit = d(settings.get("base_profit"))
    bankroll = initial + base_profit + d(bets.get("detailed_profit")) + d(tx.get("effect"))
    exposure = d(bets.get("open_exposure"))
    exposure_pct = (exposure / bankroll * 100) if bankroll > 0 else Decimal("0")
    return {
        "current_bankroll": float(_q(bankroll)),
        "initial_bankroll": float(_q(initial)),
        "base_profit": float(_q(base_profit)),
        "detailed_profit": float(_q(bets.get("detailed_profit"))),
        "transactions_effect": float(_q(tx.get("effect"))),
        "inflow": float(_q(tx.get("inflow"))),
        "outflow": float(_q(tx.get("outflow"))),
        "pending": int(bets.get("pending") or 0),
        "open_exposure": float(_q(exposure)),
        "open_exposure_pct": float(exposure_pct),
        "max_open_exposure_percent": float(d(settings.get("max_open_exposure_percent"))),
    }


def _cashflow(cur: Any, chat_id: int, window: Window) -> dict[str, Any]:
    filters = ["chat_id=%s", "created_at<%s"]
    params: list[Any] = [chat_id, window.end_utc]
    if window.start_utc is not None:
        filters.append("created_at>=%s")
        params.append(window.start_utc)
    cur.execute(
        f"""
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE kind IN ('DEPOSIT','BONUS','ADJUSTMENT')),0) AS inflow,
            COALESCE(SUM(amount) FILTER (WHERE kind='WITHDRAWAL'),0) AS outflow,
            COALESCE(SUM(CASE WHEN kind='WITHDRAWAL' THEN -amount ELSE amount END),0) AS net
        FROM bankroll_transactions
        WHERE {' AND '.join(filters)};
        """,
        params,
    )
    row = _row_dict(cur.fetchone())
    return {"inflow": float(_q(row.get("inflow"))), "outflow": float(_q(row.get("outflow"))), "net": float(_q(row.get("net")))}


def _floor_local(value: datetime, granularity: str) -> datetime:
    local = value.astimezone(TIMEZONE)
    if granularity == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("Granularidade inválida")


def _advance_local(value: datetime, granularity: str) -> datetime:
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
    raise ValueError("Granularidade inválida")


def _bucket_label(value: datetime, granularity: str) -> str:
    if granularity == "hour":
        return value.strftime("%d/%m %Hh")
    if granularity == "day":
        return value.strftime("%d/%m")
    if granularity == "week":
        return f"Sem. {value.strftime('%d/%m')}"
    months = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
    return f"{months[value.month - 1]}/{str(value.year)[-2:]}"


def _series(cur: Any, chat_id: int, window: Window) -> dict[str, Any]:
    granularity = window.granularity
    filters = [
        "chat_id=%s",
        "is_deleted=FALSE",
        "status<>'PENDING'",
        "settled_at IS NOT NULL",
        "settled_at<%s",
    ]
    params: list[Any] = [granularity, TIMEZONE_NAME, chat_id, window.end_utc]
    if window.start_utc is not None:
        filters.append("settled_at>=%s")
        params.append(window.start_utc)
    cur.execute(
        f"""
        SELECT
            date_trunc(%s, settled_at AT TIME ZONE %s) AS bucket,
            COUNT(*) AS bets,
            COUNT(*) FILTER (WHERE status IN ('GREEN','HALF_GREEN')) AS wins,
            COUNT(*) FILTER (WHERE status IN ('RED','HALF_RED')) AS losses,
            COALESCE(SUM(stake),0) AS invested,
            COALESCE(SUM(profit),0) AS profit
        FROM bets
        WHERE {' AND '.join(filters)}
        GROUP BY bucket
        ORDER BY bucket;
        """,
        params,
    )
    raw = [dict(row) for row in cur.fetchall()]

    by_bucket: dict[datetime, dict[str, Any]] = {}
    for row in raw:
        value = row["bucket"]
        if value.tzinfo is None:
            value = value.replace(tzinfo=TIMEZONE)
        else:
            value = value.astimezone(TIMEZONE)
        by_bucket[value] = row

    if window.start_utc is not None:
        first = _floor_local(window.start_utc, granularity)
    elif by_bucket:
        first = min(by_bucket)
    else:
        first = _floor_local(window.end_utc, granularity)
    last = _floor_local(window.end_utc, granularity)

    points: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    total_profit = Decimal("0")
    best = None
    worst = None
    cursor = first
    safety = 0
    while cursor <= last and safety < 1200:
        safety += 1
        row = by_bucket.get(cursor, {})
        profit = _q(row.get("profit"))
        invested = _q(row.get("invested"))
        cumulative += profit
        total_profit += profit
        best = profit if best is None else max(best, profit)
        worst = profit if worst is None else min(worst, profit)
        nxt = _advance_local(cursor, granularity)
        points.append(
            {
                "label": _bucket_label(cursor, granularity),
                "start": max(cursor, window.start_utc.astimezone(TIMEZONE) if window.start_utc else cursor).isoformat(),
                "end": min(nxt, window.end_utc.astimezone(TIMEZONE)).isoformat(),
                "bets": int(row.get("bets") or 0),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
                "invested": float(invested),
                "profit": float(profit),
                "cumulative": float(_q(cumulative)),
            }
        )
        cursor = nxt

    return {
        "granularity": granularity,
        "points": points,
        "total_profit": float(_q(total_profit)),
        "best_interval": float(_q(best or 0)),
        "worst_interval": float(_q(worst or 0)),
    }


def _drawdown_and_streak(cur: Any, chat_id: int, window: Window) -> dict[str, Any]:
    filters = [
        "chat_id=%s",
        "is_deleted=FALSE",
        "status<>'PENDING'",
        "settled_at IS NOT NULL",
        "settled_at<%s",
    ]
    params: list[Any] = [chat_id, window.end_utc]
    if window.start_utc is not None:
        filters.append("settled_at>=%s")
        params.append(window.start_utc)
    cur.execute(
        f"""
        SELECT status, profit
        FROM bets
        WHERE {' AND '.join(filters)}
        ORDER BY settled_at, id;
        """,
        params,
    )
    rows = [dict(row) for row in cur.fetchall()]
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    current_kind = "NONE"
    current_count = 0
    longest_win = 0
    longest_loss = 0
    for row in rows:
        equity += d(row.get("profit"))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        status = row.get("status")
        if status == "VOID":
            continue
        kind = "WIN" if status in {"GREEN", "HALF_GREEN"} else "LOSS"
        if kind == current_kind:
            current_count += 1
        else:
            current_kind = kind
            current_count = 1
        if kind == "WIN":
            longest_win = max(longest_win, current_count)
        else:
            longest_loss = max(longest_loss, current_count)
    return {
        "max_drawdown": float(_q(max_dd)),
        "streak": {"kind": current_kind, "count": current_count},
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
    }


def _breakdown(cur: Any, chat_id: int, window: Window, column: str, limit: int = 8) -> list[dict[str, Any]]:
    allowed = {"sport", "tipster", "bookmaker", "market"}
    if column not in allowed:
        raise ValueError("Dimensão inválida")
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    params: list[Any] = [chat_id]
    # Em Tudo, categorias usam todos os registros detalhados. Em janelas finitas,
    # apenas resultados com data de liquidação confiável entram.
    if window.start_utc is None:
        filters.append("COALESCE(settled_at, bet_date)<%s")
        params.append(window.end_utc)
    else:
        filters.extend(["settled_at IS NOT NULL", "settled_at>=%s", "settled_at<%s"])
        params.extend([window.start_utc, window.end_utc])
    params.append(max(1, min(12, int(limit))))
    cur.execute(
        f"""
        SELECT
            COALESCE(NULLIF(TRIM({column}),''),'Não informado') AS label,
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
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        item = dict(row)
        invested = d(item.get("invested"))
        profit = d(item.get("profit"))
        out.append(
            {
                "label": item.get("label") or "Não informado",
                "bets": int(item.get("bets") or 0),
                "invested": float(_q(invested)),
                "profit": float(_q(profit)),
                "roi": float((profit / invested * 100) if invested else 0),
            }
        )
    return out


def _recent(cur: Any, chat_id: int, window: Window, limit: int = 20) -> list[dict[str, Any]]:
    filters = ["chat_id=%s", "is_deleted=FALSE", "status<>'PENDING'"]
    params: list[Any] = [chat_id]
    if window.start_utc is None:
        filters.append("COALESCE(settled_at, bet_date)<%s")
        params.append(window.end_utc)
    else:
        filters.extend(["settled_at IS NOT NULL", "settled_at>=%s", "settled_at<%s"])
        params.extend([window.start_utc, window.end_utc])
    params.append(max(1, min(50, int(limit))))
    cur.execute(
        f"""
        SELECT id, bet_date, settled_at, sport, event, market, odds, stake,
               status, profit, tipster, bookmaker
        FROM bets
        WHERE {' AND '.join(filters)}
        ORDER BY COALESCE(settled_at, bet_date) DESC, id DESC
        LIMIT %s;
        """,
        params,
    )
    return [_serialize_bet(dict(row)) for row in cur.fetchall()]


def _pending(cur: Any, chat_id: int, limit: int = 12) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id, bet_date, settled_at, sport, event, market, odds, stake,
               status, profit, tipster, bookmaker
        FROM bets
        WHERE chat_id=%s AND is_deleted=FALSE AND status='PENDING'
        ORDER BY bet_date ASC, id ASC
        LIMIT %s;
        """,
        (chat_id, max(1, min(25, int(limit)))),
    )
    return [_serialize_bet(dict(row)) for row in cur.fetchall()]


def _serialize_bet(row: dict[str, Any]) -> dict[str, Any]:
    when = row.get("settled_at") or row.get("bet_date")
    if when and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when_local = when.astimezone(TIMEZONE) if when else None
    return {
        "id": int(row.get("id") or 0),
        "date": when_local.strftime("%d/%m/%Y %H:%M") if when_local else "—",
        "sport": row.get("sport") or "—",
        "event": row.get("event") or "—",
        "market": row.get("market") or "—",
        "odds": float(d(row.get("odds"))),
        "stake": float(_q(row.get("stake"))),
        "status": row.get("status") or "PENDING",
        "profit": float(_q(row.get("profit"))),
        "tipster": row.get("tipster") or "—",
        "bookmaker": row.get("bookmaker") or "—",
    }


def _quality(cur: Any, chat_id: int, settings: dict[str, Any], window: Window) -> dict[str, Any]:
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE status<>'PENDING' AND settled_at IS NOT NULL AND settled_at<%s
            ) AS exact_dated,
            COUNT(*) FILTER (WHERE status<>'PENDING' AND settled_at IS NULL) AS legacy_undated,
            COUNT(*) FILTER (WHERE status<>'PENDING' AND settled_at>=%s) AS future_dated,
            MIN(settled_at) FILTER (
                WHERE status<>'PENDING' AND settled_at IS NOT NULL AND settled_at<%s
            ) AS first_exact,
            MAX(settled_at) FILTER (
                WHERE status<>'PENDING' AND settled_at IS NOT NULL AND settled_at<%s
            ) AS last_exact
        FROM bets
        WHERE chat_id=%s AND is_deleted=FALSE;
        """,
        (window.end_utc, window.end_utc, window.end_utc, window.end_utc, chat_id),
    )
    row = _row_dict(cur.fetchone())
    exact = int(row.get("exact_dated") or 0)
    legacy = int(row.get("legacy_undated") or 0)
    base_bets = int(settings.get("base_bets") or 0)
    detailed = exact + legacy
    coverage = (Decimal(exact) / Decimal(detailed) * 100) if detailed else Decimal("100")

    first = row.get("first_exact")
    last = row.get("last_exact")
    if first and first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return {
        "exact_dated": exact,
        "legacy_undated": legacy,
        "base_bets": base_bets,
        "future_dated": int(row.get("future_dated") or 0),
        "analytical_coverage_pct": float(coverage),
        "first_exact": first.astimezone(TIMEZONE).isoformat() if first else None,
        "last_exact": last.astimezone(TIMEZONE).isoformat() if last else None,
        "period_uses_exact_dates_only": window.start_utc is not None,
    }


def _all_consolidated(cur: Any, chat_id: int, settings: dict[str, Any], at: datetime) -> dict[str, Any]:
    cur.execute(
        _performance_sql(
            "chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' AND COALESCE(settled_at, bet_date)<%s"
        ),
        (chat_id, at),
    )
    detailed = _row_dict(cur.fetchone())
    row = dict(detailed)
    row["concluded"] = int(row.get("concluded") or 0) + int(settings.get("base_bets") or 0)
    row["invested"] = d(row.get("invested")) + d(settings.get("base_staked"))
    row["profit"] = d(row.get("profit")) + d(settings.get("base_profit"))
    base_profit = d(settings.get("base_profit"))
    if base_profit > 0:
        row["gross_profit"] = d(row.get("gross_profit")) + base_profit
    elif base_profit < 0:
        row["gross_loss"] = d(row.get("gross_loss")) + abs(base_profit)
    return _status_metrics(row)


def _range_label(window: Window) -> str:
    end = window.end_utc.astimezone(TIMEZONE)
    if window.start_utc is None:
        return f"até {end.strftime('%d/%m/%Y %H:%M')}"
    start = window.start_utc.astimezone(TIMEZONE)
    return f"{start.strftime('%d/%m/%Y %H:%M')} → {end.strftime('%d/%m/%Y %H:%M')}"


def _insights(
    period: dict[str, Any],
    previous: dict[str, Any] | None,
    global_state: dict[str, Any],
    quality: dict[str, Any],
    risk: dict[str, Any],
    currency: str,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if period["concluded"] == 0:
        out.append({"level": "info", "title": "Sem resultados na janela", "text": "Nenhuma aposta com data de liquidação confiável caiu neste período."})
    elif period["concluded"] < 20:
        out.append({"level": "warning", "title": "Amostra pequena", "text": f"O período contém {period['concluded']} resultado(s). ROI e taxa de acerto ainda podem oscilar bastante."})
    if previous and period["concluded"]:
        diff = Decimal(str(period["profit"])) - Decimal(str(previous["profit"]))
        if diff > 0:
            out.append({"level": "success", "title": "Evolução positiva", "text": f"O lucro aumentou {currency} {abs(diff):,.2f} em relação à janela anterior equivalente."})
        elif diff < 0:
            out.append({"level": "warning", "title": "Queda de desempenho", "text": f"O lucro caiu {currency} {abs(diff):,.2f} frente à janela anterior equivalente."})
    if quality["legacy_undated"]:
        out.append({"level": "warning", "title": "Histórico sem data exata", "text": f"{quality['legacy_undated']} aposta(s) antiga(s) não possuem data de liquidação e são excluídas dos filtros móveis para não distorcer 24h, 7d, 30d, 90d e 1 ano."})
    if quality["base_bets"]:
        out.append({"level": "info", "title": "Resumo importado", "text": f"Há {quality['base_bets']} aposta(s) consolidadas sem registros individuais. Elas aparecem no total geral, mas nunca são inventadas no gráfico."})
    if global_state["open_exposure_pct"] > global_state["max_open_exposure_percent"]:
        out.append({"level": "danger", "title": "Exposição elevada", "text": f"A exposição aberta está em {global_state['open_exposure_pct']:.1f}% da banca, acima do limite de {global_state['max_open_exposure_percent']:.1f}%."})
    if risk["max_drawdown"] > 0:
        out.append({"level": "info", "title": "Maior drawdown da janela", "text": f"A maior queda acumulada entre resultados foi de {currency} {risk['max_drawdown']:,.2f}."})
    if not out:
        out.append({"level": "success", "title": "Dados consistentes", "text": "Todos os cards e gráficos usam a mesma janela e somente datas de liquidação confiáveis."})
    return out[:6]


def dashboard_snapshot(chat_id: int, period: str = "30") -> dict[str, Any]:
    """Retorna um snapshot consistente e auditável do dashboard.

    Regra central: filtros móveis usam exclusivamente ``settled_at``. Registros
    antigos sem data exata não são silenciosamente atribuídos a ``bet_date``.
    """
    ensure_settings(chat_id)
    window = build_window(period)

    with get_conn() as conn:
        # Todos os blocos enxergam o mesmo estado do banco.
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_settings WHERE chat_id=%s;", (chat_id,))
            settings = _row_dict(cur.fetchone())
            currency = settings.get("currency") or "R$"

            where, params = _exact_where(window)
            cur.execute(_performance_sql(where), _fill_chat_id(params, chat_id))
            exact_period = _status_metrics(_row_dict(cur.fetchone()))

            previous = _previous_totals(cur, chat_id, window)
            global_state = _global_state(cur, chat_id, settings, window.end_utc)
            cashflow = _cashflow(cur, chat_id, window)
            chart = _series(cur, chat_id, window)
            risk = _drawdown_and_streak(cur, chat_id, window)
            quality = _quality(cur, chat_id, settings, window)
            consolidated = _all_consolidated(cur, chat_id, settings, window.end_utc)
            sports = _breakdown(cur, chat_id, window, "sport", 10)
            tipsters = _breakdown(cur, chat_id, window, "tipster", 10)
            bookmakers = _breakdown(cur, chat_id, window, "bookmaker", 10)
            markets = _breakdown(cur, chat_id, window, "market", 10)
            recent = _recent(cur, chat_id, window, 24)
            pending = _pending(cur, chat_id, 12)

    period_summary = consolidated if window.key == "all" else exact_period
    comparison = previous if window.key != "all" else None
    return {
        "api_version": 7,
        "generated_at": window.end_utc.astimezone(TIMEZONE).isoformat(),
        "profile": {"display_name": settings.get("display_name") or "Apostador", "currency": currency},
        "period": {
            "key": window.key,
            "label": window.label,
            "start": window.start_utc.astimezone(TIMEZONE).isoformat() if window.start_utc else None,
            "end": window.end_utc.astimezone(TIMEZONE).isoformat(),
            "range_label": _range_label(window),
            "granularity": window.granularity,
            "rule": "Data de liquidação (settled_at)",
        },
        "summary": period_summary,
        "exact_period_summary": exact_period,
        "consolidated_summary": consolidated,
        "previous": comparison,
        "global": global_state,
        "cashflow": cashflow,
        "chart": chart,
        "risk": risk,
        "quality": quality,
        "breakdowns": {
            "sports": sports,
            "tipsters": tipsters,
            "bookmakers": bookmakers,
            "markets": markets,
        },
        "recent_bets": recent,
        "pending_bets": pending,
        "insights": _insights(exact_period, previous, global_state, quality, risk, currency),
    }
