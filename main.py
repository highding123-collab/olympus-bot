import os
import sqlite3
import random
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from PIL import Image, ImageDraw

# =========================
# ENV
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "points.db")

# =========================
# GAME CONFIG
# =========================
STARTING_POINTS = 200000
ROUND_SECONDS = 60

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
RESULT_WEIGHTS = {"P": 44.62, "B": 45.86, "T": 9.52}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

# 연승 보너스
STREAK_BONUS_START = 2
STREAK_BONUS_STEP = 0.02
STREAK_BONUS_MAX = 0.20

# 포인트 벌기
DAILY_REWARD = 10000
SPIN_DAILY_LIMIT = 3

SPIN_TABLE = [
    (0, 10),
    (500, 25),
    (1000, 30),
    (3000, 18),
    (10000, 12),
    (50000, 4),
    (100000, 1),
]

ACTIVITY_STEP = 10
ACTIVITY_REWARD = 500
ACTIVITY_MAX_STEPS_PER_DAY = 20

# =========================
# DB
# =========================
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER,
            win_streak INTEGER DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rounds(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER,
            status TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS bets(
            chat_id INTEGER,
            round_id INTEGER,
            user_id INTEGER,
            choice TEXT,
            amount INTEGER
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS house(
            chat_id INTEGER PRIMARY KEY,
            profit INTEGER DEFAULT 0,
            rounds INTEGER DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS road_history(
            chat_id INTEGER,
            round_id INTEGER,
            result TEXT
        )
        """)

        conn.commit()

# =========================
# 유저
# =========================
def ensure_user(uid, username):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, points, win_streak) VALUES(?,?,?,0)",
                (uid, username or "", STARTING_POINTS)
            )
            conn.commit()

def get_points(uid):
    with db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(row["points"]) if row else 0

def set_points(uid, p):
    with db() as conn:
        conn.execute("UPDATE users SET points=? WHERE user_id=?", (p, uid))
        conn.commit()

def get_streak(uid):
    with db() as conn:
        row = conn.execute("SELECT win_streak FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(row["win_streak"]) if row else 0

def set_streak(uid, s):
    with db() as conn:
        conn.execute("UPDATE users SET win_streak=? WHERE user_id=?", (s, uid))
        conn.commit()

# =========================
# GAME
# =========================
def weighted_result():
    keys = list(RESULT_WEIGHTS.keys())
    weights = list(RESULT_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]

# =========================
# 라운드 정산
# =========================
async def settle_round(app, chat_id, round_id):
    result = weighted_result()

    with db() as conn:
        bets = conn.execute(
            "SELECT * FROM bets WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchall()

    total_bet = 0
    total_payout = 0

    for b in bets:
        uid = b["user_id"]
        choice = b["choice"]
        amount = b["amount"]
        total_bet += amount

        # 타이 처리
        if result == "T":
            if choice == "T":
                payout = int(amount * PAYOUTS["T"])
                total_payout += payout
                set_points(uid, get_points(uid) + payout)
            else:
                # 원금 반환
                set_points(uid, get_points(uid) + amount)
            continue

        # 일반 처리
        if choice == result:
            streak = get_streak(uid) + 1
            set_streak(uid, streak)

            mult = PAYOUTS[result]
            payout = int(amount * mult)
            total_payout += payout

            set_points(uid, get_points(uid) + payout)
        else:
            set_streak(uid, 0)

    # 하우스 계산
    with db() as conn:
        row = conn.execute("SELECT profit, rounds FROM house WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO house(chat_id, profit, rounds) VALUES(?,?,?)", (chat_id, 0, 0))
            conn.commit()
            profit = 0
            rounds = 0
        else:
            profit = row["profit"]
            rounds = row["rounds"]

        profit += (total_bet - total_payout)
        rounds += 1

        conn.execute(
            "UPDATE house SET profit=?, rounds=? WHERE chat_id=?",
            (profit, rounds, chat_id)
        )

        conn.execute(
            "INSERT INTO road_history(chat_id, round_id, result) VALUES(?,?,?)",
            (chat_id, round_id, result)
        )

        conn.execute("DELETE FROM bets WHERE chat_id=?", (chat_id,))
        conn.execute("UPDATE rounds SET status='CLOSED' WHERE chat_id=?", (chat_id,))
        conn.commit()

    await app.bot.send_message(chat_id, f"🎲 라운드 #{round_id} 결과: {BET_CHOICES[result]}({result})")

# =========================
# 빅로드 이미지
# =========================
def build_road(chat_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT result FROM road_history WHERE chat_id=? ORDER BY round_id",
            (chat_id,)
        ).fetchall()

    results = [r["result"] for r in rows]

    grid = []
    ties = {}
    col = -1
    last = None
    row_index = 0

    for r in results:
        if r == "T":
            if col >= 0:
                ties[(col, row_index-1)] = 1
            continue

        if r != last:
            col += 1
            row_index = 0

        if len(grid) <= col:
            grid.append([""] * 6)

        grid[col][row_index] = r
        row_index += 1
        last = r

    return grid, ties

async def cmd_road(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    grid, ties = build_road(chat_id)

    if not grid:
        await update.message.reply_text("기록 없음")
        return

    cell = 40
    width = max(len(grid), 20) * cell
    height = 6 * cell

    img = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(img)

    for col in range(len(grid)):
        for row in range(6):
            val = grid[col][row]
            if not val:
                continue
            x0 = col*cell+5
            y0 = row*cell+5
            x1 = (col+1)*cell-5
            y1 = (row+1)*cell-5

            if val == "P":
                draw.ellipse([x0,y0,x1,y1], fill="blue")
            elif val == "B":
                draw.ellipse([x0,y0,x1,y1], fill="red")

            if (col,row) in ties:
                draw.line([x0,y1,x1,y0], fill="green", width=3)

    path = f"road_{chat_id}.png"
    img.save(path)
    await update.message.reply_photo(photo=open(path, "rb"))

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("그룹에서 사용해줘!")
        return

    with db() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if not row or row["status"] == "CLOSED":
            round_id = 1 if not row else row["round_id"] + 1
            conn.execute(
                "INSERT OR REPLACE INTO rounds(chat_id, round_id, status) VALUES(?,?,?)",
                (chat.id, round_id, "OPEN")
            )
            conn.commit()

            asyncio.create_task(
                close_round_after_delay(context.application, chat.id, round_id)
            )

            await update.message.reply_text(f"🆕 라운드 #{round_id} 시작!")

async def close_round_after_delay(app, chat_id, round_id):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(app, chat_id, round_id)

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user

    ensure_user(u.id, u.username)

    if len(context.args) != 2:
        await update.message.reply_text("사용법: /bet 금액 P|B|T")
        return

    amount = int(context.args[0])
    choice = context.args[1].upper()

    if choice not in BET_CHOICES:
        return

    cur = get_points(u.id)
    if amount > cur:
        await update.message.reply_text("잔액 부족")
        return

    with db() as conn:
        r = conn.execute("SELECT round_id FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if not r:
            await update.message.reply_text("라운드 없음. /start")
            return
        round_id = r["round_id"]

        conn.execute(
            "INSERT INTO bets(chat_id, round_id, user_id, choice, amount) VALUES(?,?,?,?,?)",
            (chat.id, round_id, u.id, choice, amount)
        )
        conn.commit()

    set_points(u.id, cur - amount)
    await update.message.reply_text("베팅 완료")

# =========================
# MAIN
# =========================
def main():
    if not TOKEN:
        raise RuntimeError("TOKEN 없음")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("road", cmd_road))

    app.run_polling()

if __name__ == "__main__":
    main()
