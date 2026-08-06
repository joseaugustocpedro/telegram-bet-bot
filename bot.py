from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

try:
    from .config import HISTORY_LIMIT, TIMEZONE, bot_token
    from .database import (
        add_transaction,
        create_bet,
        current_bankroll,
        ensure_settings,
        export_csv_to_tempfile,
        find_recent_duplicate,
        fmt_money,
        fmt_num,
        get_pending_bets,
        get_recent_bets,
        get_settings,
        init_db,
        money,
        normalize_status,
        parse_decimal,
        risk_warnings,
        set_initial_bankroll,
        set_base_summary,
        settle_bet,
        soft_delete_bet,
        summary,
    )
    from .security import dashboard_url
    from .dashboard_engine import dashboard_snapshot
    from .bulk_import import (
        add_historical_command,
        add_multiple_command,
        audit_database_command,
        deduplicate_command,
        import_single,
        initialize_bulk_schema,
        last_batch_command,
        repair_dates_command,
        repair_profits_command,
        reset_base_summary_command,
        undo_batch_command,
        validate_multiple_command,
        DuplicateBet,
    )
except ImportError:
    from config import HISTORY_LIMIT, TIMEZONE, bot_token
    from database import (
        add_transaction,
        create_bet,
        current_bankroll,
        ensure_settings,
        export_csv_to_tempfile,
        find_recent_duplicate,
        fmt_money,
        fmt_num,
        get_pending_bets,
        get_recent_bets,
        get_settings,
        init_db,
        money,
        normalize_status,
        parse_decimal,
        risk_warnings,
        set_initial_bankroll,
        set_base_summary,
        settle_bet,
        soft_delete_bet,
        summary,
    )
    from security import dashboard_url
    from dashboard_engine import dashboard_snapshot
    from bulk_import import (
        add_historical_command,
        add_multiple_command,
        audit_database_command,
        deduplicate_command,
        import_single,
        initialize_bulk_schema,
        last_batch_command,
        repair_dates_command,
        repair_profits_command,
        reset_base_summary_command,
        undo_batch_command,
        validate_multiple_command,
        DuplicateBet,
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("telegram-bankroll-v3-lite")


def _profile(update: Update) -> tuple[int, str | None, str | None]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        raise ValueError("Chat não identificado.")
    display_name = user.full_name if user else None
    username = user.username if user else None
    ensure_settings(chat.id, display_name, username)
    return chat.id, display_name, username


def _dashboard_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Abrir Dashboard", url=dashboard_url(chat_id))],
        [
            InlineKeyboardButton("📌 Resumo", callback_data="menu:summary"),
            InlineKeyboardButton("⏳ Pendentes", callback_data="menu:pending"),
        ],
        [InlineKeyboardButton("❓ Como usar", callback_data="menu:help")],
    ])


def _settle_markup(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Green", callback_data=f"settle:{bet_id}:GREEN"),
            InlineKeyboardButton("❌ Red", callback_data=f"settle:{bet_id}:RED"),
            InlineKeyboardButton("⚪ Void", callback_data=f"settle:{bet_id}:VOID"),
        ],
        [
            InlineKeyboardButton("🟢 Half Green", callback_data=f"settle:{bet_id}:HALF_GREEN"),
            InlineKeyboardButton("🔴 Half Red", callback_data=f"settle:{bet_id}:HALF_RED"),
        ],
    ])


def _summary_text(chat_id: int, period: str = "all") -> str:
    period_map = {"1": "24h", "24": "24h", "24h": "24h", "7": "7", "30": "30", "90": "90", "365": "365", "all": "all"}
    selected = period_map.get(str(period).lower(), "all")
    data = dashboard_snapshot(chat_id, selected, "result")
    currency = data["profile"]["currency"]
    current = data["summary"]
    global_state = data["global"]
    period_label = data["period"]["label"]
    imported_note = ""
    if selected == "all" and data["consolidated"]["imported_bets"]:
        imported_note = (
            f"\n📦 Histórico consolidado: {data['consolidated']['imported_bets']} aposta(s), "
            f"{fmt_money(data['consolidated']['imported_profit'], currency)} de resultado"
        )
    return (
        f"📊 RESUMO — {period_label.upper()}\n\n"
        f"🏦 Saldo atual: {fmt_money(global_state['current_bankroll'], currency)}\n"
        f"📈 Lucro do período: {fmt_money(current['profit'], currency)}\n"
        f"💵 Stake válida: {fmt_money(current['invested'], currency)}\n"
        f"🔄 Volume total: {fmt_money(current['turnover'], currency)}\n"
        f"📊 ROI: {fmt_num(current['roi'])}%\n"
        f"🎯 Taxa de acerto: {fmt_num(current['win_rate'])}%\n"
        f"✅ Concluídas: {current['concluded']} "
        f"({current['greens']}G / {current['reds']}R / {current['voids']}V)\n"
        f"⏳ Pendentes agora: {global_state['pending']}\n"
        f"⚠️ Exposição aberta: {fmt_money(global_state['open_exposure'], currency)}"
        f"{imported_note}"
    )


def _help_text() -> str:
    return (
        "📚 COMO USAR — GESTOR V9\n\n"
        "PAINEL E RESUMOS\n"
        "• /dashboard — painel completo\n"
        "• /resumo — resumo geral\n"
        "• /resumo24h, /resumo7d, /resumo30d, /resumo90d e /resumoano\n"
        "• /pendentes — liquidar com botões\n"
        "• /historico 10 — últimas apostas\n\n"
        "CADASTRO\n"
        "• /add Esporte | Evento | Mercado | Odd | Stake | Status | Tipster | Casa | Nota\n"
        "• /addhistorico DATA_APOSTA | DATA_RESULTADO | Esporte | Evento | Mercado | Odd | Stake | Status | Tipster | Casa | ID | Nota\n"
        "• /addmultipla — várias apostas em uma mensagem\n"
        "• /validarmultipla — confere o lote sem salvar\n"
        "• /ultimolote e /desfazerlote CODIGO\n\n"
        "MANUTENÇÃO DA BASE\n"
        "• /auditarbase — datas, lucros e duplicidades\n"
        "• /corrigirdatas — prévia; use CONFIRMAR para aplicar\n"
        "• /corrigirlucros — prévia; use CONFIRMAR para aplicar\n"
        "• /deduplicar — prévia; use CONFIRMAR para aplicar\n"
        "• /zerarbase — remove dupla contagem do /setresumo, com confirmação\n\n"
        "BANCA\n"
        "• /banca 1000 | /deposito 100 | nota | /saque 50 | nota\n"
        "• /setresumo 90 | 3413,93 | 109,60\n"
        "• /exportar — baixar histórico CSV\n\n"
        "Status: GREEN, RED, VOID, HALF_GREEN, HALF_RED ou PENDING."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, display_name, _ = _profile(update)
    data = summary(chat_id, "all")
    first_name = (display_name or "").split(" ")[0] or "apostador"
    await update.effective_message.reply_text(
        f"👋 Olá, {first_name}!\n\n"
        f"Sua banca está em {fmt_money(data['current_bankroll'], data['currency'])}.\n"
        "Use os botões abaixo para gerenciar e analisar suas apostas.",
        reply_markup=_dashboard_markup(chat_id),
    )


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(
        "📊 Abra o dashboard para ver saldo, ROI, drawdown, gráficos, esportes, tipsters e histórico.",
        reply_markup=_dashboard_markup(chat_id),
    )


async def resumo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id), reply_markup=_dashboard_markup(chat_id))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _profile(update)
    await update.effective_message.reply_text(_help_text())


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    message = update.effective_message
    try:
        raw = message.text.split(maxsplit=1)[1].strip() if message.text and " " in message.text else ""
        if not raw:
            raise ValueError(
                "Formato: /add Esporte | Evento | Mercado | Odd | Stake | Status | Tipster | Casa | Nota"
            )
        bet, row = import_single(chat_id, raw, source="SINGLE")
        settings = get_settings(chat_id)
        result = "Pendente" if bet.status == "PENDING" else fmt_money(row["profit"], settings["currency"])
        text = (
            f"✅ Aposta registrada — ID {row['id']}\n\n"
            f"🏆 {bet.sport}\n📌 {bet.event}\n🎯 {bet.market}\n"
            f"🎲 Odd {fmt_num(bet.odds)} | Stake {fmt_money(bet.stake, settings['currency'])}\n"
            f"📍 {bet.status} | Resultado: {result}\n"
            f"📅 Aposta: {bet.placed_at.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"
        )
        if bet.result_at:
            text += f"\n🏁 Resultado: {bet.result_at.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"
        if bet.external_id:
            text += f"\n🔖 ID externo: {bet.external_id}"
        warnings = risk_warnings(chat_id, bet.stake) if bet.status == "PENDING" else []
        if warnings:
            text += "\n\n⚠️ ALERTA DE RISCO\n" + "\n".join(f"• {item}" for item in warnings)
        markup = _settle_markup(int(row["id"])) if bet.status == "PENDING" else _dashboard_markup(chat_id)
        await message.reply_text(text, reply_markup=markup)
    except DuplicateBet as exc:
        await message.reply_text(f"♻️ Aposta não inserida: {exc}")
    except (ValueError, IndexError) as exc:
        await message.reply_text(f"Não foi possível registrar:\n{exc}")
    except Exception:
        logger.exception("Erro ao registrar aposta")
        await message.reply_text("Ocorreu um erro ao registrar a aposta.")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    rows = get_pending_bets(chat_id, 10)
    if not rows:
        await update.effective_message.reply_text("✅ Nenhuma aposta pendente.", reply_markup=_dashboard_markup(chat_id))
        return
    settings = get_settings(chat_id)
    await update.effective_message.reply_text(f"⏳ Você tem {len(rows)} aposta(s) pendente(s):")
    for row in rows:
        text = (
            f"🆔 ID {row['id']}\n"
            f"🏆 {row['sport']} — {row['event']}\n"
            f"🎯 {row['market']}\n"
            f"🎲 Odd {fmt_num(row['odds'])} | Stake {fmt_money(row['stake'], settings['currency'])}"
        )
        await update.effective_message.reply_text(text, reply_markup=_settle_markup(int(row["id"])))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    try:
        limit = int(context.args[0]) if context.args else HISTORY_LIMIT
        limit = max(1, min(30, limit))
    except ValueError:
        await update.effective_message.reply_text("Use /historico 10")
        return
    rows = get_recent_bets(chat_id, limit)
    if not rows:
        await update.effective_message.reply_text("Nenhuma aposta registrada.")
        return
    settings = get_settings(chat_id)
    chunks: list[str] = []
    current = f"📋 ÚLTIMAS {len(rows)} APOSTAS\n\n"
    for row in rows:
        dt = row["bet_date"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        item = (
            f"🆔 {row['id']} — {dt}\n"
            f"{row['sport']} | {row['market']}\n"
            f"{row['event']}\n"
            f"Odd {fmt_num(row['odds'])} | {fmt_money(row['stake'], settings['currency'])}\n"
            f"{row['status']} | {fmt_money(row['profit'], settings['currency'])}\n"
            "━━━━━━━━━━━━━━\n"
        )
        if len(current) + len(item) > 3800:
            chunks.append(current)
            current = item
        else:
            current += item
    chunks.append(current)
    for chunk in chunks:
        await update.effective_message.reply_text(chunk)


async def banca_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    if not context.args:
        settings = get_settings(chat_id)
        await update.effective_message.reply_text(
            f"Banca inicial: {fmt_money(settings['initial_bankroll'], settings['currency'])}\n"
            f"Saldo atual: {fmt_money(current_bankroll(chat_id), settings['currency'])}\n\n"
            "Para alterar: /banca 1000"
        )
        return
    try:
        amount = money(parse_decimal(" ".join(context.args)))
        set_initial_bankroll(chat_id, amount)
        settings = get_settings(chat_id)
        await update.effective_message.reply_text(
            f"✅ Banca inicial definida em {fmt_money(amount, settings['currency'])}."
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


async def _transaction_command(update: Update, kind: str) -> None:
    chat_id, _, _ = _profile(update)
    message = update.effective_message
    try:
        raw = message.text.split(maxsplit=1)[1].strip() if message.text and " " in message.text else ""
        parts = [part.strip() for part in raw.split("|", 1)]
        amount = money(parse_decimal(parts[0]))
        note = parts[1] if len(parts) == 2 and parts[1] else None
        transaction_id = add_transaction(chat_id, kind, amount, note)
        settings = get_settings(chat_id)
        label = "Depósito" if kind == "DEPOSIT" else "Saque"
        await message.reply_text(
            f"✅ {label} registrado — ID {transaction_id}\n"
            f"Valor: {fmt_money(amount, settings['currency'])}\n"
            f"Saldo atual: {fmt_money(current_bankroll(chat_id), settings['currency'])}",
            reply_markup=_dashboard_markup(chat_id),
        )
    except (ValueError, IndexError) as exc:
        command = "/deposito" if kind == "DEPOSIT" else "/saque"
        await message.reply_text(f"Use {command} 100 | descrição opcional\n{exc}")


async def deposito_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _transaction_command(update, "DEPOSIT")


async def saque_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _transaction_command(update, "WITHDRAWAL")


async def settle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    try:
        raw = update.effective_message.text.split(maxsplit=1)[1]
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 2:
            raise ValueError("Use /settle ID | STATUS")
        row = settle_bet(chat_id, int(parts[0]), parts[1])
        if not row:
            await update.effective_message.reply_text("Aposta pendente não encontrada.")
            return
        settings = get_settings(chat_id)
        await update.effective_message.reply_text(
            f"✅ Aposta ID {row['id']} liquidada como {row['status']}.\n"
            f"Resultado: {fmt_money(row['profit'], settings['currency'])}\n"
            f"Banca: {fmt_money(current_bankroll(chat_id), settings['currency'])}",
            reply_markup=_dashboard_markup(chat_id),
        )
    except (ValueError, IndexError) as exc:
        await update.effective_message.reply_text(str(exc))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    try:
        bet_id = int(context.args[0])
        removed = soft_delete_bet(chat_id, bet_id)
        await update.effective_message.reply_text(
            f"🗑 Aposta ID {bet_id} removida." if removed else "Aposta não encontrada."
        )
    except (ValueError, IndexError):
        await update.effective_message.reply_text("Use /delete ID")


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    path: str | None = None
    try:
        path = export_csv_to_tempfile(chat_id)
        with open(path, "rb") as file:
            await update.effective_message.reply_document(
                document=file,
                filename="historico-apostas.csv",
                caption="📄 Histórico exportado.",
            )
    except Exception:
        logger.exception("Erro na exportação")
        await update.effective_message.reply_text("Não foi possível exportar o histórico.")
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def setresumo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    try:
        raw = update.effective_message.text.split(maxsplit=1)[1]
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 3:
            raise ValueError("Use /setresumo QUANTIDADE | TOTAL_APOSTADO | LUCRO")
        base_bets = int(parts[0])
        base_staked = parse_decimal(parts[1])
        base_profit = parse_decimal(parts[2])
        set_base_summary(chat_id, base_bets, base_staked, base_profit)
        settings = get_settings(chat_id)
        await update.effective_message.reply_text(
            "✅ Histórico base atualizado.\n"
            f"Apostas: {base_bets}\n"
            f"Total apostado: {fmt_money(base_staked, settings['currency'])}\n"
            f"Lucro: {fmt_money(base_profit, settings['currency'])}",
            reply_markup=_dashboard_markup(chat_id),
        )
    except (ValueError, IndexError) as exc:
        await update.effective_message.reply_text(str(exc))


async def resumodia_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "1"), reply_markup=_dashboard_markup(chat_id))


async def resumomes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "30"), reply_markup=_dashboard_markup(chat_id))


async def resumo24h_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "24h"), reply_markup=_dashboard_markup(chat_id))


async def resumo7d_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "7"), reply_markup=_dashboard_markup(chat_id))


async def resumo30d_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "30"), reply_markup=_dashboard_markup(chat_id))


async def resumo90d_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "90"), reply_markup=_dashboard_markup(chat_id))


async def resumoano_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(_summary_text(chat_id, "365"), reply_markup=_dashboard_markup(chat_id))


async def resumos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    await update.effective_message.reply_text(
        "🔔 Nesta versão econômica, os resumos ficam disponíveis sob demanda para evitar agendadores extras na RAM.\n"
        "Use /resumodia, /resumomes ou abra o dashboard.",
        reply_markup=_dashboard_markup(chat_id),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, _, _ = _profile(update)
    settings = get_settings(chat_id)
    await update.effective_message.reply_text(
        "📡 STATUS\n\n"
        "✅ Bot conectado\n"
        "✅ PostgreSQL conectado\n"
        "✅ Dashboard disponível\n"
        "✅ Gráficos processados no navegador\n"
        f"🏦 Banca: {fmt_money(current_bankroll(chat_id), settings['currency'])}"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    chat_id, _, _ = _profile(update)
    data = query.data or ""
    if data == "menu:summary":
        await query.message.reply_text(_summary_text(chat_id), reply_markup=_dashboard_markup(chat_id))
        return
    if data == "menu:pending":
        rows = get_pending_bets(chat_id, 10)
        if not rows:
            await query.message.reply_text("✅ Nenhuma aposta pendente.")
            return
        settings = get_settings(chat_id)
        for row in rows:
            await query.message.reply_text(
                f"🆔 ID {row['id']}\n{row['event']}\n{row['market']}\n"
                f"Odd {fmt_num(row['odds'])} | {fmt_money(row['stake'], settings['currency'])}",
                reply_markup=_settle_markup(int(row["id"])),
            )
        return
    if data == "menu:help":
        await query.message.reply_text(_help_text())
        return
    if data.startswith("settle:"):
        try:
            _, bet_id_raw, status = data.split(":", 2)
            row = settle_bet(chat_id, int(bet_id_raw), status)
            if not row:
                await query.message.reply_text("Esta aposta já foi liquidada ou não foi encontrada.")
                return
            settings = get_settings(chat_id)
            await query.edit_message_text(
                f"✅ ID {row['id']} liquidada como {row['status']}\n"
                f"{row['event']} — {row['market']}\n"
                f"Resultado: {fmt_money(row['profit'], settings['currency'])}\n"
                f"Banca atual: {fmt_money(current_bankroll(chat_id), settings['currency'])}",
                reply_markup=_dashboard_markup(chat_id),
            )
        except Exception:
            logger.exception("Erro ao liquidar via callback")
            await query.message.reply_text("Não foi possível liquidar esta aposta.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro não tratado", exc_info=context.error)


def build_application(token: str | None = None) -> Application:
    init_db()
    initialize_bulk_schema()
    application = (
        ApplicationBuilder()
        .token(bot_token(token))
        .connection_pool_size(8)
        .get_updates_connection_pool_size(1)
        .concurrent_updates(False)
        .build()
    )
    handlers = {
        "start": start,
        "dashboard": dashboard_command,
        "grafico": dashboard_command,
        "resumo": resumo_command,
        "help": help_command,
        "add": add_command,
        "addmultipla": add_multiple_command,
        "addmulti": add_multiple_command,
        "validarmultipla": validate_multiple_command,
        "addhistorico": add_historical_command,
        "auditarbase": audit_database_command,
        "corrigirdatas": repair_dates_command,
        "corrigirlucros": repair_profits_command,
        "deduplicar": deduplicate_command,
        "zerarbase": reset_base_summary_command,
        "ultimolote": last_batch_command,
        "desfazerlote": undo_batch_command,
        "pendentes": pending_command,
        "historico": history_command,
        "banca": banca_command,
        "setresumo": setresumo_command,
        "resumodia": resumodia_command,
        "resumomes": resumomes_command,
        "resumo24h": resumo24h_command,
        "resumo7d": resumo7d_command,
        "resumo30d": resumo30d_command,
        "resumo90d": resumo90d_command,
        "resumoano": resumoano_command,
        "resumos": resumos_command,
        "deposito": deposito_command,
        "saque": saque_command,
        "settle": settle_command,
        "delete": delete_command,
        "exportar": export_command,
        "status": status_command,
    }
    for command, callback in handlers.items():
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    app = build_application()
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query"],
        close_loop=False,
    )


if __name__ == "__main__":
    main()
