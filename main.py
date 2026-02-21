import os
import sqlite3
import random
import asyncio
import json
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from PIL import Image, ImageDraw, ImageFont

# ================== CONFIG ==================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "casino.db"

STARTING_POINTS = 200_000
ROUND_SECONDS = 60

DAILY_REWARD = 10_000
SPIN_DAILY_LIMIT = 3

DICE_ROUND_SECONDS = 20

DICE_PAYOUT = {
    "BIG": 2.0,
    "SMALL": 2.0,
    "EXACT": 6.0,
}

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

KST = ZoneInfo("Asia/Seoul")

# ================== DB ==================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rounds(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bets(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,
            amount INTEGER NOT NULL,
            PRIMARY KEY(chat_id, round_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS dice_rounds(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            ends_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dice_bets(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,
            exact_value INTEGER,
            amount INTEGER NOT NULL,
            PRIMARY KEY(chat_id, round_id, user_id)
        );
        """)
        conn.commit()

# ================== USER ==================

def ensure_user(uid, username):
    with db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, points) VALUES(?,?,?)",
                (uid, username or "", STARTING_POINTS),
            )
            conn.commit()


def get_points(uid):
    with db() as conn:
        r = conn.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(r["points"]) if r else 0


def credit(uid, amount):
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (amount, uid))
        conn.commit()


def try_debit(uid, amount):
    with db() as conn:
        cur = conn.execute(
            "UPDATE users SET points = points - ? WHERE user_id=? AND points >= ?",
            (amount, uid, amount),
        )
        conn.commit()
        return cur.rowcount == 1

# ================== DICE GIF ==================

def make_dice_gif(value):
    frames = []
    size = 300

    for _ in range(6):  # 흔들리는 효과
        v = random.randint(1, 6)
        frames.append(draw_die_frame(v, size))

    frames.append(draw_die_frame(value, size))

    bio = BytesIO()
    bio.name = "dice.gif"
    frames[0].save(
        bio,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=200,
        loop=0,
    )
    bio.seek(0)
    return bio


def draw_die_frame(value, size):
    img = Image.new("RGB", (size, size), "#1e293b")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [30, 30, size - 30, size - 30],
        radius=40,
        fill="white",
        outline="#94a3b8",
        width=6,
    )

    def dot(x, y):
        r = 18
        draw.ellipse([x - r, y - r, x + r, y + r], fill="black")

    x = [size * 0.3, size * 0.5, size * 0.7]
    y = [size * 0.3, size * 0.5, size * 0.7]

    pips = {
        1: [(x[1], y[1])],
        2: [(x[0], y[0]), (x[2], y[2])],
        3: [(x[0], y[0]), (x[1], y[1]), (x[2], y[2])],
        4: [(x[0], y[0]), (x[2], y[0]), (x[0], y[2]), (x[2], y[2])],
        5: [(x[0], y[0]), (x[2], y[0]), (x[1], y[1]), (x[0], y[2]), (x[2], y[2])],
        6: [
            (x[0], y[0]),
            (x[2], y[0]),
            (x[0], y[1]),
            (x[2], y[1]),
            (x[0], y[2]),
            (x[2], y[2]),
        ],
    }

    for cx, cy in pips[value]:
        dot(int(cx), int(cy))

    return img

# ================== DICE LOGIC ==================

def get_dice_round(chat_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM dice_rounds WHERE chat_id=?", (chat_id,)
        ).fetchone()


def roll_die():
    return random.randint(1, 6)


async def settle_dice(app, chat_id, rid):
    with db() as conn:
        r = conn.execute(
            "SELECT status FROM dice_rounds WHERE chat_id=? AND round_id=?",
            (chat_id, rid),
        ).fetchone()
        if not r or r["status"] != "OPEN":
            return

        conn.execute(
            "UPDATE dice_rounds SET status='CLOSED' WHERE chat_id=? AND round_id=?",
            (chat_id, rid),
        )
        conn.commit()

        bets = conn.execute(
            "SELECT * FROM dice_bets WHERE chat_id=? AND round_id=?",
            (chat_id, rid),
        ).fetchall()

    value = roll_die()
    lines = [f"🎲 다이스 결과: {value}"]

    for b in bets:
        uid = b["user_id"]
        bet_type = b["bet_type"]
        exact = b["exact_value"]
        amt = b["amount"]

        win = (
            (bet_type == "BIG" and value >= 4)
            or (bet_type == "SMALL" and value <= 3)
            or (bet_type == "EXACT" and value == exact)
        )

        if win:
            payout = int(amt * DICE_PAYOUT[bet_type])
            credit(uid, payout)
            lines.append(f"✅ {uid} +{payout}")
        else:
            lines.append(f"❌ {uid} -{amt}")

    with db() as conn:
        conn.execute(
            "DELETE FROM dice_bets WHERE chat_id=? AND round_id=?",
            (chat_id, rid),
        )
        conn.commit()

    gif = make_dice_gif(value)
    await app.bot.send_animation(chat_id, animation=gif)
    await app.bot.send_message(chat_id, "\n".join(lines))

# ================== ! COMMAND ROUTER ==================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("!"):
        return

    parts = text.split()
    cmd = parts[0].lower()
    chat = update.effective_chat
    user = update.effective_user
    ensure_user(user.id, user.username)

    if cmd == "!dice_start":
        r = get_dice_round(chat.id)
        rid = 1 if not r else r["round_id"] + 1
        ends = int(datetime.now().timestamp()) + DICE_ROUND_SECONDS

        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dice_rounds(chat_id, round_id, status, ends_at) VALUES(?,?,?,?)",
                (chat.id, rid, "OPEN", ends),
            )
            conn.commit()

        asyncio.create_task(asyncio.sleep(DICE_ROUND_SECONDS)).add_done_callback(
            lambda f: asyncio.create_task(settle_dice(context.application, chat.id, rid))
        )

        await update.message.reply_text(
            f"🎲 다이스 {rid} 시작! 마감 {DICE_ROUND_SECONDS}초"
        )

    elif cmd == "!dice_bet":
        r = get_dice_round(chat.id)
        if not r or r["status"] != "OPEN":
            await update.message.reply_text("!dice_start 먼저 해줘")
            return

        rid = r["round_id"]

        if len(parts) not in (3, 4):
            await update.message.reply_text("!dice_bet BIG 1000 / SMALL 1000 / EXACT 3 1000")
            return

        bet_type = parts[1].upper()
        exact = None

        if bet_type == "EXACT":
            exact = int(parts[2])
            amount = int(parts[3])
        else:
            amount = int(parts[2])

        if not try_debit(user.id, amount):
            await update.message.reply_text("잔액 부족")
            return

        with db() as conn:
            conn.execute(
                "INSERT INTO dice_bets(chat_id, round_id, user_id, bet_type, exact_value, amount) VALUES(?,?,?,?,?,?)",
                (chat.id, rid, user.id, bet_type, exact, amount),
            )
            conn.commit()

        await update.message.reply_text("다이스 베팅 완료")

# ================== MAIN ==================

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # 바카라 명령어 유지
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("road", cmd_road))
    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("house", cmd_house))

    # 다이스 ! 전용
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()
