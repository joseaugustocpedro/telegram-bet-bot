from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

try:
    from .config import TIMEZONE
    from .database import (
        calculate_profit,
        d,
        ensure_settings,
        fmt_money,
        fmt_num,
        get_conn,
        get_settings,
        money,
        normalize_status,
        parse_decimal,
    )
    from .security import dashboard_url
except ImportError:
    from config import TIMEZONE
    from database import (
        calculate_profit,
        d,
        ensure_settings,
        fmt_money,
        fmt_num,
        get_conn,
        get_settings,
        money,
        normalize_status,
        parse_decimal,
    )
    from security import dashboard_url

logger = logging.getLogger("bankroll-bulk-import-v9")
_SCHEMA_READY = False

MAX_BATCH_LINES = 80
SETTLED_STATUSES = {"GREEN", "RED", "VOID", "HALF_GREEN", "HALF_RED"}
DATE_PATTERNS = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%y %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)
PLACED_RE = re.compile(r"(?:aposta|registrad[ao])\s+em\s+(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})", re.I)
RESULT_RE = re.compile(r"(?:evento|resultado|liquidada?)\s+em\s+(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})", re.I)
EXTERNAL_ID_RE = re.compile(r"\bID\s*[:#-]?\s*(\d{5,})\b", re.I)


@dataclass(frozen=True)
class ParsedBet:
    sport: str
    event: str
    market: str
    odds: Decimal
    stake: Decimal
    status: str
    tipster: str | None
    bookmaker: str | None
    external_id: str | None
    notes: str | None
    placed_at: datetime
    result_at: datetime | None


class DuplicateBet(ValueError):
    def __init__(self, existing_id: int, external_id: str | None = None):
        self.existing_id = existing_id
        self.external_id = external_id
        label = f"ID externo {external_id}" if external_id else "mesmos dados"
        super().__init__(f"duplicada ({label}); já existe na aposta {existing_id}")


def initialize_bulk_schema() -> None:
    """Adiciona metadados leves para importação, auditoria e desfazer lote uma vez por processo."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for statement in (
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS result_at TIMESTAMPTZ;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS external_id TEXT;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS import_batch_id TEXT;",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS source TEXT;",
            ):
                cur.execute(statement)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_chat_external_id "
                "ON bets(chat_id, bookmaker, external_id) "
                "WHERE is_deleted=FALSE AND external_id IS NOT NULL;"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_chat_import_batch "
                "ON bets(chat_id, import_batch_id) "
                "WHERE is_deleted=FALSE AND import_batch_id IS NOT NULL;"
            )
    _SCHEMA_READY = True


def _dashboard_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📊 Abrir Dashboard", url=dashboard_url(chat_id))]])


def _chat_id(update: Update) -> int:
    if update.effective_chat is None:
        raise ValueError("Chat não identificado.")
    chat_id = int(update.effective_chat.id)
    user = update.effective_user
    ensure_settings(chat_id, user.full_name if user else None, user.username if user else None)
    return chat_id


def parse_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw or raw in {"-", "—", "N/A", "NA", "PENDING", "PENDENTE"}:
        return None
    # ISO completo com timezone.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for pattern in DATE_PATTERNS:
        try:
            parsed = datetime.strptime(raw, pattern)
            return parsed.replace(tzinfo=TIMEZONE).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"data inválida: {value}. Use DD/MM/AAAA HH:MM")


def _looks_like_datetime(value: str) -> bool:
    try:
        return parse_datetime(value) is not None or value.strip() in {"-", "—"}
    except ValueError:
        return False


def extract_note_metadata(notes: str | None) -> tuple[datetime | None, datetime | None, str | None]:
    text = notes or ""
    placed_match = PLACED_RE.search(text)
    result_match = RESULT_RE.search(text)
    external_match = EXTERNAL_ID_RE.search(text)
    placed = parse_datetime(placed_match.group(1)) if placed_match else None
    result = parse_datetime(result_match.group(1)) if result_match else None
    external_id = external_match.group(1) if external_match else None
    return placed, result, external_id


def _clean_optional(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or text.upper() in {"-", "N/A", "NA", "NÃO INFORMADO", "NAO INFORMADO"}:
        return None
    return text


def _strip_command_prefix(line: str) -> str:
    clean = line.strip()
    clean = re.sub(r"^/(?:addmultipla|addmulti|validarmultipla|addhistorico)\s*", "", clean, flags=re.I)
    clean = re.sub(r"^/add\s+", "", clean, flags=re.I)
    return clean.strip()


def parse_bet_line(line: str, *, now: datetime | None = None) -> ParsedBet:
    raw = _strip_command_prefix(line)
    parts = [item.strip() for item in raw.split("|")]
    if len(parts) < 6:
        raise ValueError("faltam campos; separe os dados com |")

    placed_at: datetime | None = None
    result_at: datetime | None = None
    external_id: str | None = None
    tipster: str | None = None
    bookmaker: str | None = None
    notes: str | None = None

    # Formato histórico completo:
    # DATA_APOSTA | DATA_RESULTADO | ESPORTE | EVENTO | MERCADO | ODD | STAKE |
    # STATUS | TIPSTER | CASA | ID_EXTERNO | NOTA
    explicit_dates = len(parts) >= 10 and _looks_like_datetime(parts[0]) and (
        _looks_like_datetime(parts[1]) or parts[1].strip() in {"-", "—"}
    )
    if explicit_dates:
        if not 10 <= len(parts) <= 12:
            raise ValueError("formato histórico aceita de 10 a 12 campos")
        placed_at = parse_datetime(parts[0])
        result_at = parse_datetime(parts[1])
        sport, event, market = parts[2], parts[3], parts[4]
        odds, stake, status = parse_decimal(parts[5]), money(parse_decimal(parts[6])), normalize_status(parts[7])
        tipster = _clean_optional(parts[8]) if len(parts) >= 9 else None
        bookmaker = _clean_optional(parts[9]) if len(parts) >= 10 else None
        external_id = _clean_optional(parts[10]) if len(parts) >= 11 else None
        notes = _clean_optional(parts[11]) if len(parts) >= 12 else None
    else:
        # Formato /add já conhecido. Também aceita ID externo antes da nota:
        # ESPORTE | EVENTO | MERCADO | ODD | STAKE | STATUS | TIPSTER | CASA | ID | NOTA
        if not 6 <= len(parts) <= 10:
            raise ValueError("formato /add aceita de 6 a 10 campos")
        sport, event, market = parts[0], parts[1], parts[2]
        odds, stake, status = parse_decimal(parts[3]), money(parse_decimal(parts[4])), normalize_status(parts[5])
        if len(parts) == 7:
            notes = _clean_optional(parts[6])
        elif len(parts) >= 8:
            tipster = _clean_optional(parts[6])
            bookmaker = _clean_optional(parts[7])
            if len(parts) == 9:
                # Mantém compatibilidade: o 9º campo normalmente é Nota. Se for só um ID,
                # tratamos como ID externo.
                candidate = _clean_optional(parts[8])
                if candidate and re.fullmatch(r"\d{5,}", candidate):
                    external_id = candidate
                else:
                    notes = candidate
            elif len(parts) == 10:
                external_id = _clean_optional(parts[8])
                notes = _clean_optional(parts[9])

        note_placed, note_result, note_external = extract_note_metadata(notes)
        placed_at = note_placed
        result_at = note_result
        external_id = external_id or note_external

    if not sport.strip() or not event.strip() or not market.strip():
        raise ValueError("esporte, evento e mercado são obrigatórios")
    if odds <= 1:
        raise ValueError("odd precisa ser maior que 1,00")
    if stake <= 0:
        raise ValueError("stake precisa ser maior que zero")

    current = now or datetime.now(timezone.utc)
    placed_at = placed_at or current
    if status == "PENDING":
        result_at = None
    elif result_at is None:
        # Para histórico resolvido sem hora de liquidação, a data da aposta é um fallback
        # explícito e auditável; o dashboard sinaliza datas estimadas.
        result_at = placed_at

    return ParsedBet(
        sport=sport.strip(),
        event=event.strip(),
        market=market.strip(),
        odds=d(odds),
        stake=money(stake),
        status=status,
        tipster=tipster,
        bookmaker=bookmaker,
        external_id=external_id,
        notes=notes,
        placed_at=placed_at,
        result_at=result_at,
    )


def _find_duplicate(cur, chat_id: int, bet: ParsedBet) -> dict[str, Any] | None:
    if bet.external_id:
        cur.execute(
            """
            SELECT id FROM bets
            WHERE chat_id=%s AND is_deleted=FALSE AND external_id=%s
              AND COALESCE(LOWER(bookmaker),'')=COALESCE(LOWER(%s),'')
            ORDER BY id LIMIT 1;
            """,
            (chat_id, bet.external_id, bet.bookmaker),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
    cur.execute(
        """
        SELECT id FROM bets
        WHERE chat_id=%s AND is_deleted=FALSE
          AND LOWER(event)=LOWER(%s) AND LOWER(market)=LOWER(%s)
          AND odds=%s AND stake=%s AND status=%s
          AND ABS(EXTRACT(EPOCH FROM (bet_date-%s))) <= 120
        ORDER BY id LIMIT 1;
        """,
        (chat_id, bet.event, bet.market, bet.odds, bet.stake, bet.status, bet.placed_at),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def insert_parsed_bet(
    cur,
    chat_id: int,
    bet: ParsedBet,
    *,
    batch_id: str | None,
    source: str,
    force: bool = False,
) -> dict[str, Any]:
    duplicate = _find_duplicate(cur, chat_id, bet)
    if duplicate and not force:
        raise DuplicateBet(int(duplicate["id"]), bet.external_id)
    profit = calculate_profit(bet.odds, bet.stake, bet.status)
    settled_at = bet.result_at if bet.status in SETTLED_STATUSES else None
    cur.execute(
        """
        INSERT INTO bets (
            chat_id, bet_date, sport, event, market, odds, stake, status, profit,
            tipster, bookmaker, notes, settled_at, result_at,
            external_id, import_batch_id, source
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, profit, bet_date, result_at;
        """,
        (
            chat_id, bet.placed_at, bet.sport, bet.event, bet.market, bet.odds,
            bet.stake, bet.status, profit, bet.tipster, bet.bookmaker, bet.notes,
            settled_at, bet.result_at, bet.external_id, batch_id, source,
        ),
    )
    return dict(cur.fetchone())


def import_single(chat_id: int, payload: str, *, source: str = "SINGLE") -> tuple[ParsedBet, dict[str, Any]]:
    initialize_bulk_schema()
    bet = parse_bet_line(payload)
    force = "FORCAR" in (bet.notes or "").upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = insert_parsed_bet(cur, chat_id, bet, batch_id=None, source=source, force=force)
    return bet, row


def _message_lines(text: str | None) -> list[str]:
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    first, *rest = raw.split("\n")
    if first.lstrip().startswith("/"):
        after_command = re.sub(r"^/\w+(?:@\w+)?\s*", "", first, count=1).strip()
        lines = ([after_command] if after_command else []) + rest
    else:
        lines = raw.split("\n")
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _parse_batch(lines: Iterable[str]) -> tuple[list[tuple[int, ParsedBet]], list[tuple[int, str]]]:
    parsed: list[tuple[int, ParsedBet]] = []
    errors: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        try:
            parsed.append((number, parse_bet_line(line)))
        except ValueError as exc:
            errors.append((number, str(exc)))
    return parsed, errors


def _summary_for_rows(rows: Iterable[ParsedBet]) -> tuple[Decimal, Decimal]:
    total_stake = Decimal("0")
    total_profit = Decimal("0")
    for bet in rows:
        total_stake += bet.stake
        total_profit += calculate_profit(bet.odds, bet.stake, bet.status)
    return money(total_stake), money(total_profit)


async def validate_multiple_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    lines = _message_lines(update.effective_message.text if update.effective_message else "")
    if not lines:
        await update.effective_message.reply_text(_multi_help("validarmultipla"))
        return
    if len(lines) > MAX_BATCH_LINES:
        await update.effective_message.reply_text(f"Máximo de {MAX_BATCH_LINES} linhas por mensagem.")
        return

    initialize_bulk_schema()
    parsed, errors = _parse_batch(lines)
    unique_keys: set[tuple[str, ...]] = set()
    duplicates_in_message: list[tuple[int, str]] = []
    candidate_pairs: list[tuple[int, ParsedBet]] = []
    for number, bet in parsed:
        if bet.external_id:
            key = ("external", (bet.bookmaker or "").lower(), bet.external_id)
        else:
            key = (
                "content", bet.event.lower(), bet.market.lower(), str(bet.odds),
                str(bet.stake), bet.status, bet.placed_at.isoformat(),
            )
        if key in unique_keys:
            duplicates_in_message.append((number, "repetida dentro da própria mensagem"))
            continue
        unique_keys.add(key)
        candidate_pairs.append((number, bet))

    duplicates_in_database: list[tuple[int, str]] = []
    valid: list[ParsedBet] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for number, bet in candidate_pairs:
                duplicate = _find_duplicate(cur, chat_id, bet)
                if duplicate:
                    duplicates_in_database.append((number, f"já existe na aposta {duplicate['id']}"))
                else:
                    valid.append(bet)

    stake, profit = _summary_for_rows(valid)
    settings = get_settings(chat_id)
    text = (
        "🔎 VALIDAÇÃO DO LOTE\n\n"
        f"✅ Novas e válidas: {len(valid)}\n"
        f"♻️ Repetidas na mensagem: {len(duplicates_in_message)}\n"
        f"🗃 Já existentes no banco: {len(duplicates_in_database)}\n"
        f"❌ Linhas inválidas: {len(errors)}\n"
        f"💵 Stake que seria inserida: {fmt_money(stake, settings['currency'])}\n"
        f"📈 Resultado que seria inserido: {fmt_money(profit, settings['currency'])}"
    )
    details = errors[:6] + duplicates_in_message[:4] + duplicates_in_database[:6]
    if details:
        text += "\n\nDetalhes:\n" + "\n".join(f"• Linha {n}: {err}" for n, err in details)
    text += "\n\nNada foi salvo. Quando estiver correto, troque /validarmultipla por /addmultipla."
    await update.effective_message.reply_text(text)


async def add_multiple_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    message = update.effective_message
    lines = _message_lines(message.text if message else "")
    if not lines:
        await message.reply_text(_multi_help("addmultipla"))
        return
    if len(lines) > MAX_BATCH_LINES:
        await message.reply_text(f"Máximo de {MAX_BATCH_LINES} linhas por mensagem.")
        return

    initialize_bulk_schema()
    batch_id = uuid.uuid4().hex
    parsed, parse_errors = _parse_batch(lines)
    inserted: list[ParsedBet] = []
    duplicates: list[tuple[int, str]] = []
    db_errors: list[tuple[int, str]] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for number, bet in parsed:
                cur.execute(f"SAVEPOINT row_{number}")
                try:
                    force = "FORCAR" in (bet.notes or "").upper()
                    insert_parsed_bet(cur, chat_id, bet, batch_id=batch_id, source="MULTI", force=force)
                    inserted.append(bet)
                    cur.execute(f"RELEASE SAVEPOINT row_{number}")
                except DuplicateBet as exc:
                    cur.execute(f"ROLLBACK TO SAVEPOINT row_{number}")
                    duplicates.append((number, str(exc)))
                except Exception as exc:  # uma linha ruim não cancela as válidas
                    cur.execute(f"ROLLBACK TO SAVEPOINT row_{number}")
                    logger.exception("Erro ao inserir linha %s do lote", number)
                    db_errors.append((number, str(exc)))

    settings = get_settings(chat_id)
    stake, profit = _summary_for_rows(inserted)
    all_errors = parse_errors + db_errors
    text = (
        "✅ LOTE PROCESSADO\n\n"
        f"🧾 Lote: {batch_id[:10]}\n"
        f"✅ Inseridas: {len(inserted)}\n"
        f"♻️ Duplicadas ignoradas: {len(duplicates)}\n"
        f"❌ Com erro: {len(all_errors)}\n"
        f"💵 Stake inserida: {fmt_money(stake, settings['currency'])}\n"
        f"📈 Resultado inserido: {fmt_money(profit, settings['currency'])}"
    )
    details = duplicates[:5] + all_errors[:5]
    if details:
        text += "\n\nDetalhes:\n" + "\n".join(f"• Linha {n}: {err}" for n, err in details)
    if inserted:
        text += f"\n\n↩️ Para desfazer: /desfazerlote {batch_id[:10]}"
    await message.reply_text(text, reply_markup=_dashboard_markup(chat_id))


async def add_historical_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    message = update.effective_message
    raw = message.text.split(maxsplit=1)[1].strip() if message and message.text and " " in message.text else ""
    if not raw:
        await message.reply_text(_historical_help())
        return
    try:
        bet, row = import_single(chat_id, raw, source="HISTORICAL")
        settings = get_settings(chat_id)
        await message.reply_text(
            "✅ APOSTA HISTÓRICA REGISTRADA\n\n"
            f"🆔 ID {row['id']}\n"
            f"📅 Aposta: {bet.placed_at.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}\n"
            f"🏁 Resultado: {(bet.result_at.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M') if bet.result_at else 'Pendente')}\n"
            f"📌 {bet.event}\n🎯 {bet.market}\n"
            f"Resultado financeiro: {fmt_money(row['profit'], settings['currency'])}",
            reply_markup=_dashboard_markup(chat_id),
        )
    except DuplicateBet as exc:
        await message.reply_text(f"♻️ Aposta não inserida: {exc}")
    except ValueError as exc:
        await message.reply_text(f"Não foi possível registrar:\n{exc}\n\n{_historical_help()}")


def _multi_help(command: str) -> str:
    return (
        f"Use /{command} e coloque uma aposta por linha.\n\n"
        "Aceita linhas /add já prontas:\n"
        "/add Futebol | Evento | Mercado | 1.80 | 30 | GREEN | Tipster | Casa | "
        "Aposta em 05/08/2026 10:30; evento em 05/08/2026 21:30; ID 123456\n\n"
        "Formato histórico mais preciso:\n"
        "DATA_APOSTA | DATA_RESULTADO | ESPORTE | EVENTO | MERCADO | ODD | STAKE | "
        "STATUS | TIPSTER | CASA | ID_EXTERNO | NOTA"
    )


def _historical_help() -> str:
    return (
        "Use:\n/addhistorico DATA_APOSTA | DATA_RESULTADO | ESPORTE | EVENTO | MERCADO | "
        "ODD | STAKE | STATUS | TIPSTER | CASA | ID_EXTERNO | NOTA\n\n"
        "Exemplo:\n/addhistorico 04/08/2026 10:04 | 04/08/2026 21:30 | Futebol | "
        "Remo vs. Santos | Chance dupla - Empate ou Santos | 1.50 | 30 | GREEN | - | "
        "EsportivaBet | 5260665710 | odd turbinada"
    )


def _profit_formula_sql() -> str:
    return """
        ROUND((CASE status
            WHEN 'GREEN' THEN (odds-1)*stake
            WHEN 'RED' THEN -stake
            WHEN 'HALF_GREEN' THEN ((odds-1)*stake)/2
            WHEN 'HALF_RED' THEN -stake/2
            ELSE 0 END)::numeric, 2)
    """


async def audit_database_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    initialize_bulk_schema()
    formula = _profit_formula_sql()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                       COUNT(*) FILTER (WHERE status<>'PENDING') AS resolved,
                       COUNT(*) FILTER (WHERE status<>'PENDING' AND result_at IS NULL AND settled_at IS NULL) AS no_result_date,
                       COUNT(*) FILTER (WHERE external_id IS NULL OR external_id='') AS no_external_id,
                       COUNT(*) FILTER (WHERE status<>'PENDING' AND ABS(COALESCE(profit,0)-({formula}))>0.009) AS bad_profit
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE;
                """,
                (chat_id,),
            )
            row = dict(cur.fetchone())
            cur.execute(
                """
                SELECT COUNT(*) AS duplicate_groups, COALESCE(SUM(qty-1),0) AS extra_rows
                FROM (
                    SELECT bookmaker, external_id, COUNT(*) qty
                    FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND external_id IS NOT NULL
                    GROUP BY bookmaker, external_id HAVING COUNT(*)>1
                ) x;
                """,
                (chat_id,),
            )
            dup = dict(cur.fetchone())
            cur.execute(
                "SELECT COUNT(DISTINCT import_batch_id) AS batches FROM bets "
                "WHERE chat_id=%s AND is_deleted=FALSE AND import_batch_id IS NOT NULL;",
                (chat_id,),
            )
            batches = int(cur.fetchone()["batches"] or 0)
    settings = get_settings(chat_id)
    base_bets = int(settings.get("base_bets") or 0)
    base_staked = money(settings.get("base_staked") or 0)
    base_profit = money(settings.get("base_profit") or 0)
    await update.effective_message.reply_text(
        "🧪 AUDITORIA DA BASE\n\n"
        f"Apostas ativas: {row['total']}\n"
        f"Resolvidas: {row['resolved']} | Pendentes: {row['pending']}\n"
        f"Sem data de resultado: {row['no_result_date']}\n"
        f"Sem ID externo: {row['no_external_id']}\n"
        f"Lucros divergentes: {row['bad_profit']}\n"
        f"Grupos duplicados por ID: {dup['duplicate_groups']} ({dup['extra_rows']} linha(s) extra)\n"
        f"Lotes importados: {batches}\n\n"
        f"📦 Resumo base: {base_bets} aposta(s) | {fmt_money(base_staked, settings['currency'])} | "
        f"{fmt_money(base_profit, settings['currency'])}\n"
        "Se essas mesmas apostas foram importadas em detalhes, use /zerarbase para evitar dupla contagem.\n\n"
        "Correções seguras:\n"
        "• /corrigirdatas — prévia das datas encontradas nas notas\n"
        "• /corrigirlucros — prévia dos lucros divergentes\n"
        "• /deduplicar — prévia de IDs repetidos",
        reply_markup=_dashboard_markup(chat_id),
    )


def _rows_with_metadata(chat_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, notes, bet_date, settled_at, result_at, external_id
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND notes IS NOT NULL
                ORDER BY id;
                """,
                (chat_id,),
            )
            return [dict(row) for row in cur.fetchall()]


async def repair_dates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    initialize_bulk_schema()
    confirm = bool(context.args and context.args[0].upper() == "CONFIRMAR")
    candidates: list[tuple[dict[str, Any], datetime | None, datetime | None, str | None]] = []
    for row in _rows_with_metadata(chat_id):
        placed, result, external_id = extract_note_metadata(row.get("notes"))
        if placed or result or (external_id and not row.get("external_id")):
            candidates.append((row, placed, result, external_id))
    if not confirm:
        await update.effective_message.reply_text(
            "🗓 PRÉVIA DE CORREÇÃO DE DATAS\n\n"
            f"Registros com metadados aproveitáveis nas notas: {len(candidates)}\n\n"
            "Nada foi alterado. Para aplicar:\n/corrigirdatas CONFIRMAR\n\n"
            "A data 'Aposta em' vira bet_date; 'evento em/resultado em' vira result_at e settled_at."
        )
        return
    updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for row, placed, result, external_id in candidates:
                if row["status"] == "PENDING":
                    result = None
                elif result is None:
                    result = placed
                cur.execute(
                    """
                    UPDATE bets SET
                        bet_date=COALESCE(%s, bet_date),
                        settled_at=CASE WHEN status='PENDING' THEN NULL ELSE COALESCE(%s, %s, settled_at, bet_date) END,
                        result_at=CASE WHEN status='PENDING' THEN NULL ELSE COALESCE(%s, %s, result_at, bet_date) END,
                        external_id=COALESCE(external_id, %s),
                        updated_at=NOW()
                    WHERE id=%s AND chat_id=%s;
                    """,
                    (placed, result, placed, result, placed, external_id, row["id"], chat_id),
                )
                updated += int(cur.rowcount or 0)
    await update.effective_message.reply_text(
        f"✅ Datas/metadados corrigidos em {updated} aposta(s).\n"
        "Agora rode /auditarbase e depois /deduplicar."
    )


async def repair_profits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    confirm = bool(context.args and context.args[0].upper() == "CONFIRMAR")
    formula = _profit_formula_sql()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS qty FROM bets WHERE chat_id=%s AND is_deleted=FALSE "
                f"AND status<>'PENDING' AND ABS(COALESCE(profit,0)-({formula}))>0.009;",
                (chat_id,),
            )
            qty = int(cur.fetchone()["qty"] or 0)
            if confirm and qty:
                cur.execute(
                    f"UPDATE bets SET profit=({formula}), updated_at=NOW() "
                    "WHERE chat_id=%s AND is_deleted=FALSE AND status<>'PENDING' "
                    f"AND ABS(COALESCE(profit,0)-({formula}))>0.009;",
                    (chat_id,),
                )
    if confirm:
        await update.effective_message.reply_text(f"✅ Lucro recalculado em {qty} aposta(s).")
    else:
        await update.effective_message.reply_text(
            f"🧮 Há {qty} aposta(s) com lucro divergente.\n"
            "Nada foi alterado. Para aplicar: /corrigirlucros CONFIRMAR"
        )


async def deduplicate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    initialize_bulk_schema()
    confirm = bool(context.args and context.args[0].upper() == "CONFIRMAR")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(bookmaker,''), external_id, ARRAY_AGG(id ORDER BY id) ids
                FROM bets
                WHERE chat_id=%s AND is_deleted=FALSE AND external_id IS NOT NULL
                GROUP BY COALESCE(bookmaker,''), external_id HAVING COUNT(*)>1;
                """,
                (chat_id,),
            )
            groups = [dict(row) for row in cur.fetchall()]
            extras = sum(max(0, len(row["ids"]) - 1) for row in groups)
            if confirm:
                for row in groups:
                    remove_ids = list(row["ids"])[1:]
                    if remove_ids:
                        cur.execute(
                            "UPDATE bets SET is_deleted=TRUE, deleted_at=NOW(), updated_at=NOW() "
                            "WHERE chat_id=%s AND id=ANY(%s);",
                            (chat_id, remove_ids),
                        )
    if confirm:
        await update.effective_message.reply_text(f"✅ {extras} registro(s) duplicado(s) removido(s).")
    else:
        await update.effective_message.reply_text(
            f"♻️ Foram encontrados {len(groups)} grupo(s), totalizando {extras} linha(s) extra.\n"
            "Nada foi alterado. Para manter a aposta mais antiga e remover as cópias:\n"
            "/deduplicar CONFIRMAR"
        )


async def reset_base_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zera apenas o resumo consolidado criado por /setresumo; não apaga apostas."""
    chat_id = _chat_id(update)
    confirm = bool(context.args and context.args[0].upper() == "CONFIRMAR")
    settings = get_settings(chat_id)
    currency = settings["currency"]
    base_bets = int(settings.get("base_bets") or 0)
    base_staked = money(settings.get("base_staked") or 0)
    base_profit = money(settings.get("base_profit") or 0)
    if not confirm:
        await update.effective_message.reply_text(
            "📦 RESUMO BASE CONSOLIDADO\n\n"
            f"Apostas: {base_bets}\n"
            f"Stake: {fmt_money(base_staked, currency)}\n"
            f"Resultado: {fmt_money(base_profit, currency)}\n\n"
            "Esse bloco veio do /setresumo e é somado às apostas detalhadas. "
            "Se você estiver importando novamente essas mesmas apostas por prints, haverá dupla contagem.\n\n"
            "Para zerar somente esse resumo, sem apagar apostas: /zerarbase CONFIRMAR"
        )
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bankroll_settings SET base_bets=0, base_staked=0, base_profit=0, updated_at=NOW() "
                "WHERE chat_id=%s;",
                (chat_id,),
            )
    await update.effective_message.reply_text(
        "✅ Resumo base zerado. Nenhuma aposta detalhada foi apagada.\n"
        "Abra um link novo com /dashboard e confira os totais."
    )


async def last_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    initialize_bulk_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT import_batch_id, COUNT(*) qty, COALESCE(SUM(stake),0) stake,
                       COALESCE(SUM(profit),0) profit, MAX(created_at) created_at
                FROM bets WHERE chat_id=%s AND is_deleted=FALSE AND import_batch_id IS NOT NULL
                GROUP BY import_batch_id ORDER BY MAX(created_at) DESC LIMIT 1;
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    if not row:
        await update.effective_message.reply_text("Nenhum lote importado.")
        return
    settings = get_settings(chat_id)
    await update.effective_message.reply_text(
        "🧾 ÚLTIMO LOTE\n\n"
        f"Código: {row['import_batch_id'][:10]}\n"
        f"Apostas: {row['qty']}\n"
        f"Stake: {fmt_money(row['stake'], settings['currency'])}\n"
        f"Resultado: {fmt_money(row['profit'], settings['currency'])}\n\n"
        f"Para desfazer: /desfazerlote {row['import_batch_id'][:10]}"
    )


async def undo_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    initialize_bulk_schema()
    if not context.args:
        await update.effective_message.reply_text("Use /desfazerlote CODIGO")
        return
    prefix = context.args[0].strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT import_batch_id FROM bets WHERE chat_id=%s AND is_deleted=FALSE "
                "AND LOWER(import_batch_id) LIKE %s LIMIT 2;",
                (chat_id, prefix + "%"),
            )
            matches = [row["import_batch_id"] for row in cur.fetchall()]
            if len(matches) != 1:
                await update.effective_message.reply_text(
                    "Código não encontrado ou ambíguo. Use /ultimolote e copie o código exibido."
                )
                return
            cur.execute(
                "UPDATE bets SET is_deleted=TRUE, deleted_at=NOW(), updated_at=NOW() "
                "WHERE chat_id=%s AND import_batch_id=%s AND is_deleted=FALSE;",
                (chat_id, matches[0]),
            )
            removed = int(cur.rowcount or 0)
    await update.effective_message.reply_text(f"↩️ Lote desfeito: {removed} aposta(s) removida(s).")
