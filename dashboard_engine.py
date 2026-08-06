from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Sequence

try:
    from .bulk_import import initialize_bulk_schema
    from .config import TIMEZONE
    from .database import calculate_profit, d, ensure_settings, get_conn
except ImportError:
    from bulk_import import initialize_bulk_schema
    from config import TIMEZONE
    from database import calculate_profit, d, ensure_settings, get_conn

logger = logging.getLogger("bank-dashboard-engine-v9")
_ENGINE_READY = False
MONEY = Decimal("0.01")
VALID_PERIODS = {"24h", "7", "30", "90", "365", "all"}
VALID_BASES = {"result", "placed"}
RESOLVED = {"GREEN", "RED", "VOID", "HALF_GREEN", "HALF_RED"}


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start: datetime | None
    end: datetime
    previous_start: datetime | None
    previous_end: datetime | None


@dataclass(frozen=True)
class Bet:
    id: int
    placed_at: datetime
    result_at: datetime | None
    analytic_at: datetime
    date_estimated: bool
    sport: str
    event: str
    market: str
    odds: Decimal
    stake: Decimal
    status: str
    stored_profit: Decimal
    profit: Decimal
    tipster: str
    bookmaker: str
    external_id: str | None
    import_batch_id: str | None
    source: str | None


def q(value: Any) -> Decimal:
    return d(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def f(value: Any) -> float:
    return float(q(value))


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_window(period: str, now: datetime | None = None) -> Window:
    key = period if period in VALID_PERIODS else "30"
    end = utc(now) or datetime.now(timezone.utc)
    labels = {
        "24h": "Últimas 24 horas",
        "7": "Últimos 7 dias",
        "30": "Últimos 30 dias",
        "90": "Últimos 90 dias",
        "365": "Últimos 365 dias",
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
        return Window(key, labels[key], None, end, None, None)
    duration = durations[key]
    start = end - duration
    return Window(key, labels[key], start, end, start - duration, start)


def initialize_dashboard_engine() -> None:
    global _ENGINE_READY
    if _ENGINE_READY:
        return
    initialize_bulk_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_dashboard_result_v9 "
                "ON bets(chat_id, result_at DESC) WHERE is_deleted=FALSE AND status<>'PENDING';"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_dashboard_placed_v9 "
                "ON bets(chat_id, bet_date DESC) WHERE is_deleted=FALSE AND status<>'PENDING';"
            )
    _ENGINE_READY = True


def _normalize(row: dict[str, Any], basis: str) -> Bet:
    placed = utc(row.get("bet_date")) or datetime.now(timezone.utc)
    result = utc(row.get("result_at")) or utc(row.get("settled_at"))
    if basis == "placed":
        analytic = placed
        estimated = False
    else:
        analytic = result or placed
        estimated = result is None
    status = str(row.get("status") or "").upper()
    odds = d(row.get("odds"))
    stake = q(row.get("stake"))
    canonical = calculate_profit(odds, stake, status)
    return Bet(
        id=int(row.get("id") or 0),
        placed_at=placed,
        result_at=result,
        analytic_at=analytic,
        date_estimated=estimated,
        sport=str(row.get("sport") or "Não informado"),
        event=str(row.get("event") or "—"),
        market=str(row.get("market") or "—"),
        odds=odds,
        stake=stake,
        status=status,
        stored_profit=q(row.get("profit")),
        profit=q(canonical),
        tipster=str(row.get("tipster") or "Não informado"),
        bookmaker=str(row.get("bookmaker") or "Não informado"),
        external_id=str(row.get("external_id")) if row.get("external_id") else None,
        import_batch_id=str(row.get("import_batch_id")) if row.get("import_batch_id") else None,
        source=str(row.get("source")) if row.get("source") else None,
    )


def _within(rows: Sequence[Bet], start: datetime | None, end: datetime) -> list[Bet]:
    return [row for row in rows if row.analytic_at < end and (start is None or row.analytic_at >= start)]


def _metrics(rows: Sequence[Bet]) -> dict[str, Any]:
    counts = Counter(row.status for row in rows)
    turnover = sum((row.stake for row in rows), Decimal("0"))
    roi_stake = sum((row.stake for row in rows if row.status != "VOID"), Decimal("0"))
    profit = sum((row.profit for row in rows), Decimal("0"))
    gross_win = sum((row.profit for row in rows if row.profit > 0), Decimal("0"))
    gross_loss = abs(sum((row.profit for row in rows if row.profit < 0), Decimal("0")))
    decisions = counts["GREEN"] + counts["RED"] + counts["HALF_GREEN"] + counts["HALF_RED"]
    weighted_wins = Decimal(counts["GREEN"]) + Decimal(counts["HALF_GREEN"]) / 2
    average_odds = sum((row.odds for row in rows), Decimal("0")) / len(rows) if rows else Decimal("0")
    average_stake = turnover / len(rows) if rows else Decimal("0")
    return {
        "concluded": len(rows),
        "greens": counts["GREEN"],
        "reds": counts["RED"],
        "voids": counts["VOID"],
        "half_greens": counts["HALF_GREEN"],
        "half_reds": counts["HALF_RED"],
        "turnover": f(turnover),
        "invested": f(roi_stake),
        "profit": f(profit),
        "gross_profit": f(gross_win),
        "gross_loss": f(gross_loss),
        "roi": float((profit / roi_stake * 100) if roi_stake else Decimal("0")),
        "win_rate": float((weighted_wins / Decimal(decisions) * 100) if decisions else Decimal("0")),
        "average_odds": float(average_odds),
        "average_stake": f(average_stake),
        "profit_factor": float(gross_win / gross_loss) if gross_loss else None,
    }


def _month_floor(value: datetime) -> datetime:
    local = value.astimezone(TIMEZONE)
    return datetime(local.year, local.month, 1, tzinfo=TIMEZONE).astimezone(timezone.utc)


def _next_month(value: datetime) -> datetime:
    local = value.astimezone(TIMEZONE)
    year = local.year + (1 if local.month == 12 else 0)
    month = 1 if local.month == 12 else local.month + 1
    return datetime(year, month, 1, tzinfo=TIMEZONE).astimezone(timezone.utc)


def _calendar_buckets(window: Window, rows: Sequence[Bet]) -> tuple[str, list[tuple[datetime, datetime]]]:
    if window.key == "24h":
        start = window.start or window.end - timedelta(hours=24)
        return "hour", [(start + timedelta(hours=i), start + timedelta(hours=i + 1)) for i in range(24)]
    if window.key == "7":
        start = window.start or window.end - timedelta(days=7)
        return "day", [(start + timedelta(days=i), start + timedelta(days=i + 1)) for i in range(7)]
    if window.key == "30":
        start = window.start or window.end - timedelta(days=30)
        return "day", [(start + timedelta(days=i), start + timedelta(days=i + 1)) for i in range(30)]
    if window.key == "90":
        start = window.start or window.end - timedelta(days=90)
        count = 13
        step = (window.end - start) / count
        return "week", [(start + step * i, start + step * (i + 1)) for i in range(count)]
    if window.key == "365":
        start = window.start or window.end - timedelta(days=365)
        count = 12
        step = (window.end - start) / count
        return "month", [(start + step * i, start + step * (i + 1)) for i in range(count)]

    earliest = min((row.analytic_at for row in rows), default=window.end)
    cursor = _month_floor(earliest)
    buckets: list[tuple[datetime, datetime]] = []
    while cursor < window.end and len(buckets) < 60:
        nxt = _next_month(cursor)
        buckets.append((cursor, min(nxt, window.end)))
        cursor = nxt
    if not buckets:
        start = window.end - timedelta(days=1)
        buckets = [(start, window.end)]
    return "month", buckets


def _bucket_label(start: datetime, end: datetime, granularity: str) -> str:
    local = start.astimezone(TIMEZONE)
    if granularity == "hour":
        return local.strftime("%Hh")
    if granularity in {"day", "week"}:
        return local.strftime("%d/%m")
    return local.strftime("%b/%y").replace("May", "mai").replace("Aug", "ago").replace("Sep", "set").replace("Oct", "out").replace("Dec", "dez")


def _series(rows: Sequence[Bet], window: Window) -> dict[str, Any]:
    granularity, bucket_ranges = _calendar_buckets(window, rows)
    points: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    for start, end in bucket_ranges:
        selected = [row for row in rows if start <= row.analytic_at < end]
        profit = sum((row.profit for row in selected), Decimal("0"))
        invested = sum((row.stake for row in selected if row.status != "VOID"), Decimal("0"))
        cumulative += profit
        counts = Counter(row.status for row in selected)
        points.append(
            {
                "label": _bucket_label(start, end, granularity),
                "start": start.astimezone(TIMEZONE).isoformat(),
                "end": end.astimezone(TIMEZONE).isoformat(),
                "profit": f(profit),
                "cumulative": f(cumulative),
                "invested": f(invested),
                "bets": len(selected),
                "greens": counts["GREEN"],
                "reds": counts["RED"],
            }
        )
    total = q(sum((row.profit for row in rows), Decimal("0")))
    chart_total = q(sum((d(point["profit"]) for point in points), Decimal("0")))
    nonempty = [point for point in points if point["bets"]]
    best = max(nonempty, key=lambda item: item["profit"], default=None)
    worst = min(nonempty, key=lambda item: item["profit"], default=None)
    return {
        "granularity": granularity,
        "points": points,
        "total_profit": f(total),
        "reconciled": abs(total - chart_total) <= Decimal("0.01"),
        "difference": f(total - chart_total),
        "best": best,
        "worst": worst,
    }


def _risk(rows: Sequence[Bet]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row.analytic_at, row.id))
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    win_streak = loss_streak = longest_win = longest_loss = 0
    current_kind = "NONE"
    current_count = 0
    for row in ordered:
        cumulative += row.profit
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        if row.profit > 0:
            win_streak += 1
            loss_streak = 0
            longest_win = max(longest_win, win_streak)
            current_kind, current_count = "WIN", win_streak
        elif row.profit < 0:
            loss_streak += 1
            win_streak = 0
            longest_loss = max(longest_loss, loss_streak)
            current_kind, current_count = "LOSS", loss_streak
    return {
        "max_drawdown": f(max_drawdown),
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "current_streak": {"kind": current_kind, "count": current_count},
    }


def _breakdown(rows: Sequence[Bet], attribute: str, limit: int = 12) -> list[dict[str, Any]]:
    groups: dict[str, list[Bet]] = defaultdict(list)
    for row in rows:
        groups[str(getattr(row, attribute) or "Não informado")].append(row)
    output = []
    for label, selected in groups.items():
        metrics = _metrics(selected)
        output.append({"label": label, **metrics})
    output.sort(key=lambda item: (abs(item["profit"]), item["concluded"]), reverse=True)
    return output[:limit]


def _odds_breakdown(rows: Sequence[Bet]) -> list[dict[str, Any]]:
    labels = [
        ("Abaixo de 1,50", lambda x: x < Decimal("1.50")),
        ("1,50 a 1,99", lambda x: Decimal("1.50") <= x < Decimal("2.00")),
        ("2,00 a 2,99", lambda x: Decimal("2.00") <= x < Decimal("3.00")),
        ("3,00 a 4,99", lambda x: Decimal("3.00") <= x < Decimal("5.00")),
        ("5,00 ou mais", lambda x: x >= Decimal("5.00")),
    ]
    out = []
    for label, predicate in labels:
        selected = [row for row in rows if predicate(row.odds)]
        if selected:
            out.append({"label": label, **_metrics(selected)})
    return out


def _transaction_effect(row: dict[str, Any]) -> Decimal:
    amount = q(row.get("amount"))
    return -amount if row.get("kind") == "WITHDRAWAL" else amount


def _cashflow(transactions: Sequence[dict[str, Any]], start: datetime | None, end: datetime) -> dict[str, float]:
    selected = [row for row in transactions if utc(row.get("created_at")) and utc(row.get("created_at")) < end and (start is None or utc(row.get("created_at")) >= start)]
    inflow = sum((_transaction_effect(row) for row in selected if _transaction_effect(row) > 0), Decimal("0"))
    outflow = abs(sum((_transaction_effect(row) for row in selected if _transaction_effect(row) < 0), Decimal("0")))
    return {"inflow": f(inflow), "outflow": f(outflow), "net": f(inflow - outflow), "count": len(selected)}


def _serialize(row: Bet) -> dict[str, Any]:
    return {
        "id": row.id,
        "placed_at": row.placed_at.astimezone(TIMEZONE).isoformat(),
        "result_at": row.result_at.astimezone(TIMEZONE).isoformat() if row.result_at else None,
        "analytic_at": row.analytic_at.astimezone(TIMEZONE).isoformat(),
        "date_estimated": row.date_estimated,
        "sport": row.sport,
        "event": row.event,
        "market": row.market,
        "odds": float(row.odds),
        "stake": f(row.stake),
        "status": row.status,
        "profit": f(row.profit),
        "tipster": row.tipster,
        "bookmaker": row.bookmaker,
        "external_id": row.external_id,
        "source": row.source,
    }


def _quality(all_rows: Sequence[Bet], period_rows: Sequence[Bet], settings: dict[str, Any], chart: dict[str, Any]) -> dict[str, Any]:
    minute_counts = Counter(row.analytic_at.replace(second=0, microsecond=0) for row in all_rows)
    day_counts = Counter(row.analytic_at.astimezone(TIMEZONE).date() for row in all_rows)
    total = len(all_rows)
    same_minute = max(minute_counts.values(), default=0)
    same_day = max(day_counts.values(), default=0)
    external_ids = [row.external_id for row in all_rows if row.external_id]
    duplicate_external_rows = len(external_ids) - len(set((row.bookmaker.lower(), row.external_id) for row in all_rows if row.external_id))
    mismatches = sum(1 for row in all_rows if abs(row.stored_profit - row.profit) > Decimal("0.01"))
    estimated_period = sum(1 for row in period_rows if row.date_estimated)
    return {
        "detailed_rows": total,
        "estimated_dates_in_period": estimated_period,
        "without_result_date": sum(1 for row in all_rows if row.result_at is None),
        "with_external_id": len(external_ids),
        "external_id_coverage_pct": (len(external_ids) / total * 100) if total else 0,
        "duplicate_external_rows": max(0, duplicate_external_rows),
        "profit_mismatches": mismatches,
        "same_minute_pct": (same_minute / total * 100) if total else 0,
        "same_day_pct": (same_day / total * 100) if total else 0,
        "timestamps_suspicious": total >= 10 and ((same_minute / total) >= 0.35 or (same_day / total) >= 0.80),
        "chart_reconciled": chart["reconciled"],
        "chart_difference": chart["difference"],
        "base_bets": int(settings.get("base_bets") or 0),
        "base_profit": f(settings.get("base_profit")),
        "base_staked": f(settings.get("base_staked")),
    }


def _comparison(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if previous is None:
        return None
    def pct_change(now: float, before: float) -> float | None:
        if abs(before) < 1e-9:
            return None
        return (now - before) / abs(before) * 100
    return {
        "profit_delta": current["profit"] - previous["profit"],
        "profit_pct": pct_change(current["profit"], previous["profit"]),
        "roi_delta_pp": current["roi"] - previous["roi"],
        "invested_delta": current["invested"] - previous["invested"],
        "bets_delta": current["concluded"] - previous["concluded"],
    }


def _range_label(window: Window) -> str:
    if window.start is None:
        return f"Até {window.end.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"
    return (
        f"{window.start.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')} → "
        f"{window.end.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"
    )


def _insights(summary: dict[str, Any], previous: dict[str, Any] | None, quality: dict[str, Any], global_state: dict[str, Any], risk: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if summary["concluded"] == 0:
        out.append({"level": "info", "title": "Sem resultados", "text": "Nenhuma aposta resolvida pertence ao período selecionado."})
    elif summary["concluded"] < 20:
        out.append({"level": "warning", "title": "Amostra pequena", "text": f"O período tem {summary['concluded']} resultado(s); ROI e taxa de acerto podem oscilar bastante."})
    if quality["timestamps_suspicious"]:
        out.append({"level": "danger", "title": "Datas concentradas", "text": f"{quality['same_day_pct']:.1f}% do histórico está concentrado no mesmo dia. Rode /corrigirdatas para recuperar as datas das notas."})
    if quality["profit_mismatches"]:
        out.append({"level": "danger", "title": "Lucros divergentes", "text": f"{quality['profit_mismatches']} registro(s) não batem com odd, stake e status. Rode /corrigirlucros."})
    if quality["duplicate_external_rows"]:
        out.append({"level": "danger", "title": "Duplicidades", "text": f"Há {quality['duplicate_external_rows']} registro(s) repetido(s) por ID externo. Rode /deduplicar."})
    if quality["estimated_dates_in_period"]:
        out.append({"level": "warning", "title": "Datas estimadas", "text": f"{quality['estimated_dates_in_period']} resultado(s) usaram a data da aposta porque não possuem data de resultado."})
    if previous and summary["concluded"]:
        delta = summary["profit"] - previous["profit"]
        if delta > 0:
            out.append({"level": "success", "title": "Evolução positiva", "text": f"O lucro aumentou R$ {delta:.2f} em relação à janela anterior equivalente."})
        elif delta < 0:
            out.append({"level": "warning", "title": "Queda no período", "text": f"O lucro caiu R$ {abs(delta):.2f} contra a janela anterior equivalente."})
    if global_state["open_exposure_pct"] > global_state["max_open_exposure_percent"]:
        out.append({"level": "danger", "title": "Exposição acima do limite", "text": f"As pendentes representam {global_state['open_exposure_pct']:.1f}% da banca."})
    if risk["max_drawdown"] > 0:
        out.append({"level": "info", "title": "Drawdown", "text": f"A maior queda dentro da janela foi R$ {risk['max_drawdown']:.2f}."})
    if not out:
        out.append({"level": "success", "title": "Dados conciliados", "text": "Cards, gráfico, categorias e tabela vieram da mesma lista de apostas."})
    return out[:7]


def dashboard_snapshot(chat_id: int, period: str = "30", basis: str = "result") -> dict[str, Any]:
    ensure_settings(chat_id)
    basis = basis if basis in VALID_BASES else "result"
    window = build_window(period)

    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_settings WHERE chat_id=%s;", (chat_id,))
            settings = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT id, bet_date, settled_at, result_at, sport, event, market, odds,
                       stake, status, profit, tipster, bookmaker, external_id,
                       import_batch_id, source
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING'
                ORDER BY bet_date, id;
                """,
                (chat_id,),
            )
            resolved_raw = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id, bet_date, settled_at, result_at, sport, event, market, odds,
                       stake, status, profit, tipster, bookmaker, external_id,
                       import_batch_id, source
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND status='PENDING'
                ORDER BY bet_date DESC, id DESC LIMIT 30;
                """,
                (chat_id,),
            )
            pending_raw = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT kind, amount, note, created_at FROM bankroll_transactions "
                "WHERE chat_id=%s ORDER BY created_at;",
                (chat_id,),
            )
            transactions = [dict(row) for row in cur.fetchall()]

    all_rows = [_normalize(row, basis) for row in resolved_raw]
    period_rows = _within(all_rows, window.start, window.end)
    previous_rows = _within(all_rows, window.previous_start, window.previous_end) if window.previous_start else []
    all_until_now = _within(all_rows, None, window.end)
    pending_rows = [_normalize(row, "placed") for row in pending_raw]

    summary = _metrics(period_rows)
    previous = _metrics(previous_rows) if window.previous_start is not None else None
    detailed_all = _metrics(all_until_now)
    chart = _series(period_rows, window)
    risk = _risk(period_rows)
    cashflow_period = _cashflow(transactions, window.start, window.end)
    cashflow_all = _cashflow(transactions, None, window.end)

    initial = q(settings.get("initial_bankroll"))
    base_profit = q(settings.get("base_profit"))
    base_staked = q(settings.get("base_staked"))
    base_bets = int(settings.get("base_bets") or 0)
    current_bankroll = initial + base_profit + d(detailed_all["profit"]) + d(cashflow_all["net"])
    exposure = sum((row.stake for row in pending_rows), Decimal("0"))
    exposure_pct = (exposure / current_bankroll * 100) if current_bankroll > 0 else Decimal("0")

    global_state = {
        "current_bankroll": f(current_bankroll),
        "initial_bankroll": f(initial),
        "pending": len(pending_rows),
        "open_exposure": f(exposure),
        "open_exposure_pct": float(exposure_pct),
        "max_open_exposure_percent": float(d(settings.get("max_open_exposure_percent") or 15)),
        "transactions_net": cashflow_all["net"],
    }
    consolidated = {
        "bets": base_bets + detailed_all["concluded"],
        "invested": f(base_staked + d(detailed_all["invested"])),
        "profit": f(base_profit + d(detailed_all["profit"])),
        "imported_bets": base_bets,
        "imported_invested": f(base_staked),
        "imported_profit": f(base_profit),
        "detailed_bets": detailed_all["concluded"],
        "detailed_invested": detailed_all["invested"],
        "detailed_profit": detailed_all["profit"],
    }
    quality = _quality(all_until_now, period_rows, settings, chart)
    comparison = _comparison(summary, previous)

    recent = sorted(period_rows, key=lambda row: (row.analytic_at, row.id), reverse=True)[:40]
    return {
        "api_version": 9,
        "generated_at": window.end.astimezone(TIMEZONE).isoformat(),
        "profile": {
            "display_name": settings.get("display_name") or "Apostador",
            "currency": settings.get("currency") or "R$",
        },
        "period": {
            "key": window.key,
            "label": window.label,
            "start": window.start.astimezone(TIMEZONE).isoformat() if window.start else None,
            "end": window.end.astimezone(TIMEZONE).isoformat(),
            "range_label": _range_label(window),
            "basis": basis,
            "basis_label": "Data do resultado" if basis == "result" else "Data da aposta",
        },
        "summary": summary,
        "previous": previous,
        "comparison": comparison,
        "detailed_all": detailed_all,
        "consolidated": consolidated,
        "global": global_state,
        "cashflow": cashflow_period,
        "chart": chart,
        "risk": risk,
        "quality": quality,
        "breakdowns": {
            "sports": _breakdown(period_rows, "sport"),
            "tipsters": _breakdown(period_rows, "tipster"),
            "bookmakers": _breakdown(period_rows, "bookmaker"),
            "markets": _breakdown(period_rows, "market"),
            "odds": _odds_breakdown(period_rows),
        },
        "recent_bets": [_serialize(row) for row in recent],
        "pending_bets": [_serialize(row) for row in pending_rows],
        "insights": _insights(summary, previous, quality, global_state, risk),
    }
