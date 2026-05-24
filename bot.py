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

    total, apostado, lucro = cursor.fetchone()

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


# ==================================
# MAIN
# ==================================

async def main():

    criar_banco()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("add", add)
    )

    app.add_handler(
        CommandHandler("resumo", resumo)
    )

    print("BOT ONLINE")

    await app.run_polling()


if __name__ == "__main__":

    import asyncio

    asyncio.run(main())
