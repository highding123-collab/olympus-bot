import os
import sqlite3
from datetime import datetime
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DB = "points.db"

STARTING_POINTS = 1000
BET_CHOICES = ["A", "B", "C"]
PAYOUT = 2

ADMIN_IDS = []  # 나중에 네 텔레그램 ID 넣으면 관리자 기능 사용 가능


def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 1000
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            chat_id INTEGER,
            user_id INTEGER,
            amount INTEGER,
            choice TEXT
        )
        """)


def ensure_user(user):
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, points) VALUES (?, ?, ?)",
                (user.id, user.username or "", STARTING_POINTS)
            )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    with db() as conn:
        points = conn.execute(
            "SELECT points FROM users WHERE user_id=?",
            (update.effective_user.id,)
        ).fetchone()[0]
    await update.message.reply_text(f"💰 현재 포인트: {points}")


async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.message.reply_text("그룹에서만 사용 가능")

    ensure_user(update.effective_user)

    if len(context.args) != 2:
        return await update.message.reply_text("/bet 100 A")

    amount = int(context.args[0])
    choice = context.args[1].upper()

    if choice not in BET_CHOICES:
        return await update.message.reply_text("A / B / C 중 선택")

    with db() as conn:
        points = conn.execute(
            "SELECT points FROM users WHERE user_id=?",
            (update.effective_user.id,)
        ).fetchone()[0]

        if points < amount:
            return await update.message.reply_text("포인트 부족")

        conn.execute(
            "UPDATE users SET points = points - ? WHERE user_id=?",
            (amount, update.effective_user.id)
        )

        conn.execute(
            "INSERT INTO bets VALUES (?, ?, ?, ?)",
            (update.effective_chat.id,
             update.effective_user.id,
             amount,
             choice)
        )

    await update.message.reply_text(f"🎲 {amount} 포인트를 {choice}에 베팅 완료")


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("관리자만 사용 가능")

    if len(context.args) != 1:
        return await update.message.reply_text("/close A")

    result = context.args[0].upper()

    with db() as conn:
        bets = conn.execute(
            "SELECT user_id, amount, choice FROM bets WHERE chat_id=?",
            (update.effective_chat.id,)
        ).fetchall()

        for user_id, amount, choice in bets:
            if choice == result:
                conn.execute(
                    "UPDATE users SET points = points + ? WHERE user_id=?",
                    (amount * PAYOUT, user_id)
                )

        conn.execute(
            "DELETE FROM bets WHERE chat_id=?",
            (update.effective_chat.id,)
        )

    await update.message.reply_text(f"🎉 결과: {result} 정산 완료")


async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

    text = "🏆 랭킹\n"
    for i, (name, pts) in enumerate(rows, start=1):
        text += f"{i}. {name} - {pts}\n"

    await update.message.reply_text(text)


def main():
    if not TOKEN:
        raise Exception("토큰 없음")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("bet", bet))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("rank", rank))

    app.run_polling()


if __name__ == "__main__":
    main()
