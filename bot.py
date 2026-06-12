from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

import sqlite3
from datetime import datetime
import os

TOKEN = os.environ["BOT_TOKEN"]

DB_NAME = "bets.db"


# ==================================
# BANCO
# ==================================

def conectar():
    return sqlite3.connect(DB_NAME)


def criar_banco():

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS apostas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT,
        esporte TEXT,
        evento TEXT,
        mercado TEXT,
        odd REAL,
        stake REAL,
        status TEXT,
        lucro REAL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS resumo_base (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        bets_base INTEGER NOT NULL DEFAULT 0,
        apostado_base REAL NOT NULL DEFAULT 0,
        lucro_base REAL NOT NULL DEFAULT 0
    )
    """)

    cursor.execute("""
    INSERT OR IGNORE INTO resumo_base (
        id,
        bets_base,
        apostado_base,
        lucro_base
    )
    VALUES (1, 0, 0, 0)
    """)

    conn.commit()
    conn.close()

# ==================================
# LUCRO
# ==================================

def calcular_lucro(odd, stake, status):

    status = status.upper()

    if status == "GREEN":
        return round((odd - 1) * stake, 2)

    if status == "RED":
        return round(-stake, 2)

    return 0


# ==================================
# START
# ==================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    texto = """
🤖 BOT DE APOSTAS

Comandos:

/add

/resumo

EXEMPLO:

/add Futebol | Arsenal vs Chelsea | Match Odds | 2.10 | 50 | GREEN
"""

    await update.message.reply_text(texto)


# ==================================
# ADD BET
# ==================================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        mensagem = update.message.text.replace("/add", "").strip()

        partes = [p.strip() for p in mensagem.split("|")]

        esporte = partes[0]
        evento = partes[1]
        mercado = partes[2]
        odd = float(partes[3])
        stake = float(partes[4])
        status = partes[5]

        lucro = calcular_lucro(odd, stake, status)

        conn = conectar()

        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO apostas (
            data,
            esporte,
            evento,
            mercado,
            odd,
            stake,
            status,
            lucro
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%d/%m/%Y"),
            esporte,
            evento,
            mercado,
            odd,
            stake,
            status,
            lucro
        ))

        conn.commit()

        conn.close()

        await update.message.reply_text(
            f"✅ Bet registrada!\n\n"
            f"💰 Resultado: R$ {lucro:.2f}"
        )

    except Exception as erro:

        await update.message.reply_text(
            f"Erro:\n{erro}"
        )


# ==================================
# RESUMO
# ==================================
async def setresumo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        mensagem = update.message.text.replace("/setresumo", "").strip()

        partes = [p.strip() for p in mensagem.split("|")]

        if len(partes) != 3:
            await update.message.reply_text(
                "Formato inválido.\n\nUse:\n"
                "/setresumo 90 | 3413.93 | 109.60"
            )
            return

        bets_base = int(partes[0])
        apostado_base = float(partes[1].replace(",", "."))
        lucro_base = float(partes[2].replace(",", "."))

        conn = conectar()
        cursor = conn.cursor()

        cursor.execute("""
        UPDATE resumo_base
        SET
            bets_base = ?,
            apostado_base = ?,
            lucro_base = ?
        WHERE id = 1
        """, (
            bets_base,
            apostado_base,
            lucro_base
        ))

        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ Histórico base salvo!\n\n"
            f"🧾 Bets antigas: {bets_base}\n"
            f"💰 Apostado antigo: R$ {apostado_base:.2f}\n"
            f"📈 Lucro antigo: R$ {lucro_base:.2f}"
        )

    except Exception as erro:
        await update.message.reply_text(
            f"Erro ao salvar resumo base:\n{erro}"
        )

async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        COUNT(*),
        COALESCE(SUM(stake),0),
        COALESCE(SUM(lucro),0)
    FROM apostas
    """)

    novas_bets, novo_apostado, novo_lucro = cursor.fetchone()

    cursor.execute("""
    SELECT
        bets_base,
        apostado_base,
        lucro_base
    FROM resumo_base
    WHERE id = 1
    """)

    bets_base, apostado_base, lucro_base = cursor.fetchone()

    conn.close()

    total = bets_base + novas_bets
    apostado = apostado_base + novo_apostado
    lucro = lucro_base + novo_lucro

    roi = (
        lucro / apostado * 100
        if apostado > 0 else 0
    )

    texto = f'''
📊 RESUMO

🧾 Bets: {total}

💰 Apostado: R$ {apostado:.2f}

📈 Lucro: R$ {lucro:.2f}

📊 ROI: {roi:.2f}%
'''

    await update.message.reply_text(texto)
    conn.close()


from flask import Flask
import threading

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot online"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


def main():

    criar_banco()

    threading.Thread(target=run_web).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(CommandHandler("setresumo", setresumo))

    print("BOT ONLINE")

    app.run_polling()


if __name__ == "__main__":
    main()
