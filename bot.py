import csv
import io
import logging
import os
import re
import threading
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg2
import psycopg2.extras
from flask import Flask
from telegram import InputFile, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("telegram-bankroll-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DEFAULT_INITIAL_BANKROLL = Decimal(os.environ.get("INITIAL_BANKROLL", "1000").replace(",", "."))
DEFAULT_CURRENCY = os.environ.get("CURRENCY", "R$").strip() or "R$"
BANK_TIMEZONE_NAME = os.environ.get("BANK_TIMEZONE", "America/Sao_Paulo").strip()
DAILY_HOUR = int(os.environ.get("BANK_DAILY_SUMMARY_HOUR", "23"))
DAILY_MINUTE = int(os.environ.get("BANK_DAILY_SUMMARY_MINUTE", "55"))
MONTHLY_HOUR = int(os.environ.get("BANK_MONTHLY_SUMMARY_HOUR", "0"))
MONTHLY_MINUTE = int(os.environ.get("BANK_MONTHLY_SUMMARY_MINUTE", "10"))
HISTORY_LIMIT = int(os.environ.get("BANK_HISTORY_DEFAULT_LIMIT", "10"))
VALID_STATUSES = {"GREEN", "RED", "VOID", "HALF_GREEN", "HALF_RED", "PENDING"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não configurado.")

BANK_TZ = ZoneInfo(BANK_TIMEZONE_NAME)
MONEY_QUANT = Decimal("0.01")

web_app = Flask(__name__)

@web_app.get("/")
def health():
    return {
        "status": "online",
        "service": "telegram-bankroll-bot-v2",
        "database": "postgresql",
        "timezone": BANK_TIMEZONE_NAME,
    }, 200

@web_app.get("/health")
def health_alias():
    return health()

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

def get_conn():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=20,
        application_name="telegram-bankroll-bot-v2",
    )

def criar_banco():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_settings (
                chat_id BIGINT PRIMARY KEY,
                initial_bankroll NUMERIC(16,2) NOT NULL DEFAULT 1000,
                currency TEXT NOT NULL DEFAULT 'R$',
                base_bets INTEGER NOT NULL DEFAULT 0,
                base_staked NUMERIC(16,2) NOT NULL DEFAULT 0,
                base_profit NUMERIC(16,2) NOT NULL DEFAULT 0,
                daily_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                monthly_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
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
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT bets_status_check CHECK (
                    status IN ('GREEN','RED','VOID','HALF_GREEN','HALF_RED','PENDING')
                )
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS summary_dispatch (
                chat_id BIGINT NOT NULL,
                summary_type TEXT NOT NULL,
                period_key TEXT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (chat_id, summary_type, period_key)
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_chat_date ON bets(chat_id, bet_date);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_active ON bets(chat_id, is_deleted);")
    logger.info("Banco PostgreSQL pronto.")

def ensure_settings(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO bankroll_settings (chat_id, initial_bankroll, currency)
            VALUES (%s,%s,%s)
            ON CONFLICT (chat_id) DO NOTHING;
            """, (chat_id, DEFAULT_INITIAL_BANKROLL, DEFAULT_CURRENCY))

def get_settings(chat_id: int) -> Dict[str, Any]:
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bankroll_settings WHERE chat_id=%s;", (chat_id,))
            row = cur.fetchone()
    if not row:
        raise RuntimeError("Configuração não encontrada.")
    return dict(row)

def subscriber_chat_ids(kind: str) -> List[int]:
    column = "daily_summary_enabled" if kind == "daily" else "monthly_summary_enabled"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT chat_id FROM bankroll_settings WHERE {column}=TRUE;")
            rows = cur.fetchall()
    return [int(r["chat_id"]) for r in rows]

def d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))

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

def fmt_money(value: Any, currency: str) -> str:
    amount = money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, cents = f"{amount:.2f}".split(".")
    return f"{sign}{currency} {int(whole):,}".replace(",", ".") + f",{cents}"

def fmt_num(value: Any, places: int = 2) -> str:
    return f"{d(value):.{places}f}".replace(".", ",")

def normalize_status(text: str) -> str:
    raw = text.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "G":"GREEN","WIN":"GREEN","GANHOU":"GREEN","GREEN":"GREEN",
        "R":"RED","LOSS":"RED","PERDEU":"RED","RED":"RED",
        "VOID":"VOID","DEVOLVIDA":"VOID","DEVOLVIDO":"VOID",
        "HG":"HALF_GREEN","HALFGREEN":"HALF_GREEN","HALF_GREEN":"HALF_GREEN",
        "HR":"HALF_RED","HALFRED":"HALF_RED","HALF_RED":"HALF_RED",
        "P":"PENDING","PENDENTE":"PENDING","PENDING":"PENDING",
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

def local_now() -> datetime:
    return datetime.now(BANK_TZ)

def day_bounds(target: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(target, time.min, tzinfo=BANK_TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)

def month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=BANK_TZ)
    end = datetime(year + 1, 1, 1, tzinfo=BANK_TZ) if month == 12 else datetime(year, month + 1, 1, tzinfo=BANK_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def previous_month(ref: Optional[date] = None) -> Tuple[int, int]:
    ref = ref or local_now().date()
    prev = ref.replace(day=1) - timedelta(days=1)
    return prev.year, prev.month

def parse_month(args: List[str]) -> Tuple[int, int]:
    if not args:
        now = local_now()
        return now.year, now.month
    match = re.fullmatch(r"(\d{4})-(\d{2})", args[0].strip())
    if not match:
        raise ValueError("Use AAAA-MM, por exemplo 2026-06.")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError("Mês inválido.")
    return year, month

def weighted_win_rate(g: int, r: int, hg: int, hr: int) -> Decimal:
    total = g + r + hg + hr
    if total == 0:
        return Decimal("0")
    return ((Decimal(g) + Decimal(hg) * Decimal("0.5")) / Decimal(total) * 100).quantize(Decimal("0.01"))

def query_summary(chat_id: int, start: Optional[datetime] = None, end: Optional[datetime] = None) -> Dict[str, Any]:
    filters = ["chat_id=%s", "is_deleted=FALSE"]
    params: List[Any] = [chat_id]
    if start:
        filters.append("bet_date>=%s")
        params.append(start)
    if end:
        filters.append("bet_date<%s")
        params.append(end)
    where = " AND ".join(filters)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
            SELECT
                COUNT(*) AS total_registered,
                COUNT(*) FILTER (WHERE status!='PENDING') AS total_resolved,
                COUNT(*) FILTER (WHERE status='GREEN') AS greens,
                COUNT(*) FILTER (WHERE status='RED') AS reds,
                COUNT(*) FILTER (WHERE status='VOID') AS voids,
                COUNT(*) FILTER (WHERE status='HALF_GREEN') AS half_greens,
                COUNT(*) FILTER (WHERE status='HALF_RED') AS half_reds,
                COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                COALESCE(SUM(CASE WHEN status NOT IN ('VOID','PENDING') THEN stake ELSE 0 END),0) AS settled_staked,
                COALESCE(SUM(profit),0) AS profit,
                COALESCE(AVG(odds) FILTER (WHERE status NOT IN ('VOID','PENDING')),0) AS average_odds,
                COALESCE(AVG(stake) FILTER (WHERE status NOT IN ('VOID','PENDING')),0) AS average_stake
            FROM bets WHERE {where};
            """, tuple(params))
            row = dict(cur.fetchone())
    stake, profit = d(row["settled_staked"]), d(row["profit"])
    row["roi"] = profit / stake * 100 if stake > 0 else Decimal("0")
    row["win_rate"] = weighted_win_rate(
        int(row["greens"] or 0), int(row["reds"] or 0),
        int(row["half_greens"] or 0), int(row["half_reds"] or 0),
    )
    return row

def overall_summary(chat_id: int) -> Dict[str, Any]:
    settings = get_settings(chat_id)
    new = query_summary(chat_id)
    total_bets = int(settings["base_bets"] or 0) + int(new["total_resolved"] or 0)
    total_staked = d(settings["base_staked"]) + d(new["settled_staked"])
    total_profit = d(settings["base_profit"]) + d(new["profit"])
    roi = total_profit / total_staked * 100 if total_staked > 0 else Decimal("0")
    current_bankroll = d(settings["initial_bankroll"]) + total_profit
    return {
        "settings": settings, "new": new, "total_bets": total_bets,
        "total_staked": total_staked, "total_profit": total_profit,
        "roi": roi, "current_bankroll": current_bankroll,
    }

def bankroll_before(chat_id: int, before: datetime) -> Decimal:
    settings = get_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(SUM(profit),0) AS profit
            FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND bet_date<%s;
            """, (chat_id, before))
            profit = d(cur.fetchone()["profit"])
    return d(settings["initial_bankroll"]) + d(settings["base_profit"]) + profit


def top_sports(chat_id: int, start: datetime, end: datetime, limit: int = 3) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT sport, COUNT(*) AS total,
                   COALESCE(SUM(profit),0) AS profit
            FROM bets
            WHERE chat_id=%s AND is_deleted=FALSE
              AND bet_date>=%s AND bet_date<%s
            GROUP BY sport
            ORDER BY profit DESC
            LIMIT %s;
            """, (chat_id, start, end, limit))
            return [dict(r) for r in cur.fetchall()]

def build_daily_text(chat_id: int, target: date) -> str:
    settings = get_settings(chat_id)
    currency = settings["currency"]
    start, end = day_bounds(target)
    data = query_summary(chat_id, start, end)
    overall = overall_summary(chat_id)

    lines = [
        f"📅 RESUMO DO DIA — {target.strftime('%d/%m/%Y')}",
        "",
        f"🧾 Apostas feitas: {data['total_registered']}",
        f"✅ Greens: {data['greens']}",
        f"❌ Reds: {data['reds']}",
        f"🟢 Half-Greens: {data['half_greens']}",
        f"🔴 Half-Reds: {data['half_reds']}",
        f"⚪ Voids: {data['voids']}",
        f"⏳ Pendentes: {data['pending']}",
        "",
        f"💰 Apostado liquidado: {fmt_money(data['settled_staked'], currency)}",
        f"📈 Lucro do dia: {fmt_money(data['profit'], currency)}",
        f"📊 ROI do dia: {fmt_num(data['roi'])}%",
        f"🎯 Win Rate: {fmt_num(data['win_rate'])}%",
        f"🎲 Odd média: {fmt_num(data['average_odds'])}",
        f"🏦 Banca estimada: {fmt_money(overall['current_bankroll'], currency)}",
    ]

    sports = top_sports(chat_id, start, end, 1)
    if sports:
        lines += ["", f"🏆 Melhor esporte: {sports[0]['sport']} ({fmt_money(sports[0]['profit'], currency)})"]

    if int(data["total_registered"] or 0) == 0:
        lines += ["", "Nenhuma aposta foi registrada neste dia."]

    return "\n".join(lines)

def build_monthly_text(chat_id: int, year: int, month: int) -> str:
    settings = get_settings(chat_id)
    currency = settings["currency"]
    start, end = month_bounds(year, month)
    data = query_summary(chat_id, start, end)
    ending_bankroll = bankroll_before(chat_id, end)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT (bet_date AT TIME ZONE %s)::date AS local_day,
                   COALESCE(SUM(profit),0) AS profit
            FROM bets
            WHERE chat_id=%s AND is_deleted=FALSE
              AND bet_date>=%s AND bet_date<%s
            GROUP BY local_day ORDER BY local_day;
            """, (BANK_TIMEZONE_NAME, chat_id, start, end))
            days = [dict(r) for r in cur.fetchall()]

    best = max(days, key=lambda r: d(r["profit"]), default=None)
    worst = min(days, key=lambda r: d(r["profit"]), default=None)

    lines = [
        f"📆 RESUMO MENSAL — {month:02d}/{year}",
        "",
        f"🧾 Apostas feitas: {data['total_registered']}",
        f"✅ Greens: {data['greens']}",
        f"❌ Reds: {data['reds']}",
        f"🟢 Half-Greens: {data['half_greens']}",
        f"🔴 Half-Reds: {data['half_reds']}",
        f"⚪ Voids: {data['voids']}",
        f"⏳ Pendentes: {data['pending']}",
        "",
        f"💰 Apostado liquidado: {fmt_money(data['settled_staked'], currency)}",
        f"📈 Lucro mensal: {fmt_money(data['profit'], currency)}",
        f"📊 ROI mensal: {fmt_num(data['roi'])}%",
        f"🎯 Win Rate: {fmt_num(data['win_rate'])}%",
        f"🎲 Odd média: {fmt_num(data['average_odds'])}",
        f"💵 Stake média: {fmt_money(data['average_stake'], currency)}",
        f"🏦 Banca ao fim do mês: {fmt_money(ending_bankroll, currency)}",
    ]

    if best:
        lines += ["", f"🟢 Melhor dia: {best['local_day'].strftime('%d/%m')} ({fmt_money(best['profit'], currency)})"]
    if worst:
        lines += [f"🔴 Pior dia: {worst['local_day'].strftime('%d/%m')} ({fmt_money(worst['profit'], currency)})"]

    sports = top_sports(chat_id, start, end, 3)
    if sports:
        lines += ["", "🏆 MELHORES ESPORTES"]
        for i, row in enumerate(sports, 1):
            lines.append(f"{i}. {row['sport']} — {fmt_money(row['profit'], currency)}")

    lines += ["", "📈 O gráfico da evolução da banca é enviado junto."]
    return "\n".join(lines)

def generate_month_chart(chat_id: int, year: int, month: int) -> io.BytesIO:
    settings = get_settings(chat_id)
    currency = settings["currency"]
    start, end = month_bounds(year, month)
    starting = bankroll_before(chat_id, start)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT bet_date, profit
            FROM bets
            WHERE chat_id=%s AND is_deleted=FALSE
              AND bet_date>=%s AND bet_date<%s
            ORDER BY bet_date, id;
            """, (chat_id, start, end))
            rows = [dict(r) for r in cur.fetchall()]

    by_day: Dict[date, Decimal] = {}
    for row in rows:
        local_day = row["bet_date"].astimezone(BANK_TZ).date()
        by_day[local_day] = by_day.get(local_day, Decimal("0")) + d(row["profit"])

    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    dates, balances = [], []
    current = starting
    cursor_day = first

    while cursor_day <= last:
        current += by_day.get(cursor_day, Decimal("0"))
        dates.append(cursor_day)
        balances.append(float(current))
        cursor_day += timedelta(days=1)

    fig, ax = plt.subplots(figsize=(11, 5.8))
    ax.plot(dates, balances, marker="o", markersize=3.5, linewidth=2)
    ax.axhline(float(starting), linestyle="--", linewidth=1, alpha=0.6)
    ax.set_title(f"Evolução da banca — {month:02d}/{year}", fontsize=15, pad=15)
    ax.set_xlabel("Data")
    ax.set_ylabel(f"Banca ({currency})")
    ax.grid(True, alpha=0.25)

    if balances:
        minimum, maximum = min(balances), max(balances)
        padding = max((maximum - minimum) * 0.12, 10)
        ax.set_ylim(minimum - padding, maximum + padding)
        ax.annotate(
            f"{currency} {balances[-1]:,.2f}",
            xy=(dates[-1], balances[-1]),
            xytext=(8, 8),
            textcoords="offset points",
        )

    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    buffer.name = f"evolucao_banca_{year}_{month:02d}.png"
    return buffer

def summary_was_sent(chat_id: int, kind: str, key: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT 1 FROM summary_dispatch
            WHERE chat_id=%s AND summary_type=%s AND period_key=%s;
            """, (chat_id, kind, key))
            return cur.fetchone() is not None

def mark_summary_sent(chat_id: int, kind: str, key: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO summary_dispatch (chat_id, summary_type, period_key)
            VALUES (%s,%s,%s)
            ON CONFLICT (chat_id, summary_type, period_key) DO NOTHING;
            """, (chat_id, kind, key))

async def send_daily(application: Application, chat_id: int, target: date, mark: bool):
    key = target.isoformat()
    if mark and summary_was_sent(chat_id, "daily", key):
        return
    await application.bot.send_message(chat_id=chat_id, text=build_daily_text(chat_id, target))
    if mark:
        mark_summary_sent(chat_id, "daily", key)

async def send_monthly(application: Application, chat_id: int, year: int, month: int, mark: bool):
    key = f"{year:04d}-{month:02d}"
    if mark and summary_was_sent(chat_id, "monthly", key):
        return

    text = build_monthly_text(chat_id, year, month)
    chart = generate_month_chart(chat_id, year, month)

    await application.bot.send_message(chat_id=chat_id, text=text)
    await application.bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(chart, filename=chart.name),
        caption=f"📈 Evolução da banca — {month:02d}/{year}",
    )
    if mark:
        mark_summary_sent(chat_id, "monthly", key)

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    target = local_now().date()
    for chat_id in subscriber_chat_ids("daily"):
        try:
            await send_daily(context.application, chat_id, target, True)
        except Exception:
            logger.exception("Erro no resumo diário para %s", chat_id)

async def monthly_job(context: ContextTypes.DEFAULT_TYPE):
    now = local_now()
    if now.day != 1:
        return
    year, month = previous_month(now.date())
    for chat_id in subscriber_chat_ids("monthly"):
        try:
            await send_monthly(context.application, chat_id, year, month, True)
        except Exception:
            logger.exception("Erro no resumo mensal para %s", chat_id)

async def catchup_job(context: ContextTypes.DEFAULT_TYPE):
    yesterday = local_now().date() - timedelta(days=1)
    for chat_id in subscriber_chat_ids("daily"):
        try:
            await send_daily(context.application, chat_id, yesterday, True)
        except Exception:
            logger.exception("Erro no catch-up diário para %s", chat_id)

    year, month = previous_month()
    for chat_id in subscriber_chat_ids("monthly"):
        try:
            await send_monthly(context.application, chat_id, year, month, True)
        except Exception:
            logger.exception("Erro no catch-up mensal para %s", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    await update.message.reply_text(
        "🤖 GESTOR DE BANCA 2.0\n\n"
        "Seu chat foi cadastrado para receber resumos automáticos.\n\n"
        "/add — registrar aposta\n"
        "/resumo — resumo geral\n"
        "/historico — últimas apostas\n"
        "/resumodia — resumo de hoje\n"
        "/resumomes — resumo mensal + gráfico\n"
        "/grafico — gráfico mensal\n"
        "/setresumo — cadastrar histórico base\n"
        "/banca — definir banca inicial\n"
        "/settle — liquidar aposta pendente\n"
        "/delete — remover aposta errada\n"
        "/exportar — baixar CSV\n"
        "/resumos — ativar/pausar automações\n"
        "/help — instruções"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 COMO USAR\n\n"
        "/add Esporte | Evento | Mercado | Odd | Stake | Status | Nota opcional\n"
        "Exemplo:\n"
        "/add Futebol | Arsenal x Chelsea | Match Odds | 2.10 | 50 | GREEN\n\n"
        "Status: GREEN, RED, VOID, HALF_GREEN, HALF_RED ou PENDING\n\n"
        "/setresumo 90 | 3413.93 | 109.60\n"
        "/banca 1000\n"
        "/historico 20\n"
        "/settle 15 | GREEN\n"
        "/delete 15\n"
        "/resumodia\n"
        "/resumodia 2026-06-14\n"
        "/resumomes 2026-06\n"
        "/grafico 2026-06\n"
        "/resumos on\n"
        "/resumos off"
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    try:
        raw = update.message.text.replace("/add", "", 1).strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) not in (6, 7):
            await update.message.reply_text(
                "Formato inválido.\n"
                "/add Esporte | Evento | Mercado | Odd | Stake | Status | Nota opcional"
            )
            return

        sport, event, market = parts[0], parts[1], parts[2]
        odds = parse_decimal(parts[3])
        stake = money(parse_decimal(parts[4]))
        status = normalize_status(parts[5])
        notes = parts[6] if len(parts) == 7 and parts[6] else None

        if odds <= 1:
            raise ValueError("A odd precisa ser maior que 1.00.")
        if stake <= 0:
            raise ValueError("A stake precisa ser maior que zero.")

        profit = calculate_profit(odds, stake, status)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO bets (
                    chat_id,sport,event,market,odds,stake,status,profit,notes
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, bet_date;
                """, (
                    chat_id, sport, event, market, odds,
                    stake, status, profit, notes,
                ))
                row = cur.fetchone()

        settings = get_settings(chat_id)
        result = "⏳ Pendente" if status == "PENDING" else fmt_money(profit, settings["currency"])

        await update.message.reply_text(
            "✅ APOSTA REGISTRADA\n\n"
            f"🆔 ID: {row['id']}\n"
            f"🏆 Esporte: {sport}\n"
            f"📌 Evento: {event}\n"
            f"🎯 Mercado: {market}\n"
            f"🎲 Odd: {fmt_num(odds)}\n"
            f"💵 Stake: {fmt_money(stake, settings['currency'])}\n"
            f"📍 Status: {status}\n"
            f"📈 Resultado: {result}"
        )
    except Exception as error:
        logger.exception("Erro no /add")
        await update.message.reply_text(f"Erro ao registrar aposta:\n{error}")

def parse_setresumo(raw: str) -> Tuple[int, Decimal, Decimal]:
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3:
            raise ValueError("Use /setresumo 90 | 3413.93 | 109.60")
        return int(parts[0]), money(parse_decimal(parts[1])), money(parse_decimal(parts[2]))

    bets = re.search(r"Bets\s*:\s*(\d+)", raw, re.I)
    staked = re.search(r"Apostado\s*:\s*(?:R\$|\$)?\s*([\d.,]+)", raw, re.I)
    profit = re.search(r"Lucro\s*:\s*(?:R\$|\$)?\s*(-?[\d.,]+)", raw, re.I)
    if not bets or not staked or not profit:
        raise ValueError("Não consegui ler. Use /setresumo 90 | 3413.93 | 109.60")
    return (
        int(bets.group(1)),
        money(parse_decimal(staked.group(1))),
        money(parse_decimal(profit.group(1))),
    )

async def setresumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    try:
        raw = update.message.text.replace("/setresumo", "", 1).strip()
        if not raw:
            await update.message.reply_text("Use /setresumo 90 | 3413.93 | 109.60")
            return

        bets, staked, profit = parse_setresumo(raw)
        if bets < 0 or staked < 0:
            raise ValueError("Bets e apostado não podem ser negativos.")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE bankroll_settings
                SET base_bets=%s, base_staked=%s, base_profit=%s, updated_at=NOW()
                WHERE chat_id=%s;
                """, (bets, staked, profit, chat_id))

        settings = get_settings(chat_id)
        await update.message.reply_text(
            "✅ HISTÓRICO BASE SALVO\n\n"
            f"🧾 Bets antigas: {bets}\n"
            f"💰 Apostado antigo: {fmt_money(staked, settings['currency'])}\n"
            f"📈 Lucro antigo: {fmt_money(profit, settings['currency'])}\n\n"
            "As novas apostas serão somadas a estes valores."
        )
    except Exception as error:
        await update.message.reply_text(f"Erro ao salvar histórico base:\n{error}")

async def banca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    try:
        if not context.args:
            s = get_settings(chat_id)
            await update.message.reply_text(
                f"💰 Banca inicial: {fmt_money(s['initial_bankroll'], s['currency'])}\n"
                "Para alterar: /banca 1000"
            )
            return
        value = money(parse_decimal(context.args[0]))
        if value <= 0:
            raise ValueError("A banca deve ser maior que zero.")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE bankroll_settings
                SET initial_bankroll=%s, updated_at=NOW()
                WHERE chat_id=%s;
                """, (value, chat_id))
        s = get_settings(chat_id)
        await update.message.reply_text(f"✅ Banca inicial atualizada para {fmt_money(value, s['currency'])}")
    except Exception as error:
        await update.message.reply_text(f"Erro ao configurar banca:\n{error}")

async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = overall_summary(chat_id)
    s, n = data["settings"], data["new"]
    await update.message.reply_text(
        "📊 RESUMO GERAL\n\n"
        f"🧾 Bets: {data['total_bets']}\n"
        f"💰 Apostado: {fmt_money(data['total_staked'], s['currency'])}\n"
        f"📈 Lucro: {fmt_money(data['total_profit'], s['currency'])}\n"
        f"📊 ROI: {fmt_num(data['roi'])}%\n"
        f"🏦 Banca estimada: {fmt_money(data['current_bankroll'], s['currency'])}\n\n"
        "APOSTAS NOVAS ARMAZENADAS\n"
        f"✅ Greens: {n['greens']}\n"
        f"❌ Reds: {n['reds']}\n"
        f"⚪ Voids: {n['voids']}\n"
        f"🟢 Half-Greens: {n['half_greens']}\n"
        f"🔴 Half-Reds: {n['half_reds']}\n"
        f"⏳ Pendentes: {n['pending']}\n"
        f"🎯 Win Rate: {fmt_num(n['win_rate'])}%\n"
        f"🎲 Odd média: {fmt_num(n['average_odds'])}"
    )

async def historico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    limit = HISTORY_LIMIT
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            pass
    limit = max(1, min(limit, 30))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT id,bet_date,sport,event,market,odds,stake,status,profit
            FROM bets
            WHERE chat_id=%s AND is_deleted=FALSE
            ORDER BY id DESC LIMIT %s;
            """, (chat_id, limit))
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        await update.message.reply_text("Nenhuma aposta registrada ainda.")
        return

    s = get_settings(chat_id)
    text = f"📋 ÚLTIMAS {len(rows)} APOSTAS\n\n"
    for row in rows:
        dt = row["bet_date"].astimezone(BANK_TZ)
        text += (
            f"🆔 ID {row['id']} — {dt.strftime('%d/%m/%Y %H:%M')}\n"
            f"🏆 {row['sport']} | {row['market']}\n"
            f"📌 {row['event']}\n"
            f"🎲 Odd {fmt_num(row['odds'])} | Stake {fmt_money(row['stake'], s['currency'])}\n"
            f"📍 {row['status']} | Resultado {fmt_money(row['profit'], s['currency'])}\n"
            "━━━━━━━━━━━━━━\n"
        )
    await update.message.reply_text(text)

async def delete_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Use /delete ID")
        return
    try:
        bet_id = int(context.args[0])
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE bets SET is_deleted=TRUE, updated_at=NOW()
                WHERE id=%s AND chat_id=%s AND is_deleted=FALSE RETURNING id;
                """, (bet_id, chat_id))
                row = cur.fetchone()
        await update.message.reply_text(
            f"🗑 Aposta ID {bet_id} removida." if row else "Aposta não encontrada."
        )
    except Exception as error:
        await update.message.reply_text(f"Erro ao remover aposta:\n{error}")

async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        raw = update.message.text.replace("/settle", "", 1).strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 2:
            await update.message.reply_text("Use /settle ID | STATUS")
            return
        bet_id, status = int(parts[0]), normalize_status(parts[1])
        if status == "PENDING":
            raise ValueError("Escolha um status resolvido.")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                SELECT odds,stake FROM bets
                WHERE id=%s AND chat_id=%s AND is_deleted=FALSE;
                """, (bet_id, chat_id))
                bet = cur.fetchone()
                if not bet:
                    await update.message.reply_text("Aposta não encontrada.")
                    return
                profit = calculate_profit(d(bet["odds"]), d(bet["stake"]), status)
                cur.execute("""
                UPDATE bets SET status=%s, profit=%s, updated_at=NOW()
                WHERE id=%s AND chat_id=%s;
                """, (status, profit, bet_id, chat_id))

        s = get_settings(chat_id)
        await update.message.reply_text(
            f"✅ Aposta ID {bet_id} liquidada como {status}.\n"
            f"Resultado: {fmt_money(profit, s['currency'])}"
        )
    except Exception as error:
        await update.message.reply_text(f"Erro ao liquidar aposta:\n{error}")

async def resumodia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        target = datetime.strptime(context.args[0], "%Y-%m-%d").date() if context.args else local_now().date()
        await update.message.reply_text(build_daily_text(chat_id, target))
    except Exception as error:
        await update.message.reply_text(f"Erro:\n{error}\nUse /resumodia ou /resumodia 2026-06-14")

async def resumomes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        year, month = parse_month(context.args)
        await send_monthly(context.application, chat_id, year, month, False)
    except Exception as error:
        logger.exception("Erro no /resumomes")
        await update.message.reply_text(f"Erro:\n{error}\nUse /resumomes ou /resumomes 2026-06")

async def grafico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        year, month = parse_month(context.args)
        chart = generate_month_chart(chat_id, year, month)
        await update.message.reply_photo(
            photo=InputFile(chart, filename=chart.name),
            caption=f"📈 Evolução da banca — {month:02d}/{year}",
        )
    except Exception as error:
        await update.message.reply_text(f"Erro ao gerar gráfico:\n{error}")

async def exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT id,bet_date,sport,event,market,odds,stake,status,profit,notes
            FROM bets WHERE chat_id=%s AND is_deleted=FALSE ORDER BY id;
            """, (chat_id,))
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        await update.message.reply_text("Nenhuma aposta para exportar.")
        return

    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(["id","data","esporte","evento","mercado","odd","stake","status","lucro","notas"])
    for row in rows:
        writer.writerow([
            row["id"], row["bet_date"].astimezone(BANK_TZ).isoformat(),
            row["sport"], row["event"], row["market"], row["odds"],
            row["stake"], row["status"], row["profit"], row["notes"] or "",
        ])

    buffer = io.BytesIO(text.getvalue().encode("utf-8-sig"))
    buffer.name = f"historico_{local_now().strftime('%Y%m%d_%H%M')}.csv"
    await update.message.reply_document(
        document=InputFile(buffer, filename=buffer.name),
        caption="📥 Histórico completo de apostas.",
    )

async def resumos_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    if not context.args:
        s = get_settings(chat_id)
        await update.message.reply_text(
            "🔔 RESUMOS AUTOMÁTICOS\n\n"
            f"Diário: {'ATIVO' if s['daily_summary_enabled'] else 'PAUSADO'}\n"
            f"Mensal: {'ATIVO' if s['monthly_summary_enabled'] else 'PAUSADO'}\n\n"
            "Use /resumos on ou /resumos off"
        )
        return

    option = context.args[0].lower()
    if option not in {"on", "off"}:
        await update.message.reply_text("Use /resumos on ou /resumos off")
        return
    enabled = option == "on"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE bankroll_settings
            SET daily_summary_enabled=%s, monthly_summary_enabled=%s, updated_at=NOW()
            WHERE chat_id=%s;
            """, (enabled, enabled, chat_id))
    await update.message.reply_text("✅ Resumos automáticos " + ("ativados." if enabled else "pausados."))

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_settings(chat_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now;")
            db_now = cur.fetchone()["now"]

    await update.message.reply_text(
        "📡 STATUS DO GESTOR\n\n"
        "✅ Bot online\n"
        "✅ PostgreSQL conectado\n"
        f"🕒 Banco: {db_now.astimezone(BANK_TZ).strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"🌎 Fuso: {BANK_TIMEZONE_NAME}\n"
        f"📅 Resumo diário: {DAILY_HOUR:02d}:{DAILY_MINUTE:02d}\n"
        f"📆 Resumo mensal: dia 1 às {MONTHLY_HOUR:02d}:{MONTHLY_MINUTE:02d}"
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro não tratado.", exc_info=context.error)

def main():
    criar_banco()
    threading.Thread(target=run_flask, daemon=True, name="bank-flask").start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("setresumo", setresumo))
    app.add_handler(CommandHandler("banca", banca))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(CommandHandler("historico", historico))
    app.add_handler(CommandHandler("delete", delete_bet))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("resumodia", resumodia))
    app.add_handler(CommandHandler("resumomes", resumomes))
    app.add_handler(CommandHandler("grafico", grafico))
    app.add_handler(CommandHandler("exportar", exportar))
    app.add_handler(CommandHandler("resumos", resumos_config))
    app.add_handler(CommandHandler("status", status_command))
    app.add_error_handler(error_handler)

    if app.job_queue is None:
        raise RuntimeError("JobQueue indisponível. Instale python-telegram-bot[job-queue].")

    app.job_queue.run_daily(
        daily_job,
        time=time(DAILY_HOUR, DAILY_MINUTE, tzinfo=BANK_TZ),
        name="bank-daily-summary",
    )
    app.job_queue.run_daily(
        monthly_job,
        time=time(MONTHLY_HOUR, MONTHLY_MINUTE, tzinfo=BANK_TZ),
        name="bank-monthly-summary-check",
    )
    app.job_queue.run_once(catchup_job, when=30, name="bank-summary-catchup")

    logger.info("GESTOR DE BANCA 2.0 ONLINE")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
