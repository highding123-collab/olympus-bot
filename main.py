import os
import sqlite3
import random
import asyncio
import json
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
DB_PATH = "casino.db"

# =========================
# 기본 설정
# =========================
STARTING_POINTS = 200000
ROUND_SECONDS = 60

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

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]

# =========================
# DB
# =========================
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
            points INTEGER
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

        conn.execute("""
        CREATE TABLE IF NOT EXISTS shoe(
            chat_id INTEGER PRIMARY KEY,
            cards TEXT,
            position INTEGER
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_claims(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            PRIMARY KEY(chat_id, user_id, day)
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS spin_claims(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            used INTEGER,
            PRIMARY KEY(chat_id, user_id, day)
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS activity(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            msg_count INTEGER,
            rewarded_steps INTEGER,
            PRIMARY KEY(chat_id, user_id, day)
        )
        """)

        conn.commit()

# =========================
# 유저 관리
# =========================
def ensure_user(uid, username):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, points) VALUES(?,?,?)",
                (uid, username or "", STARTING_POINTS)
            )
            conn.commit()

def get_points(uid):
    with db() as conn:
        r = conn.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        return r["points"] if r else 0

def set_points(uid, p):
    with db() as conn:
        conn.execute("UPDATE users SET points=? WHERE user_id=?", (p, uid))
        conn.commit()

# =========================
# 슈 엔진 (8덱 유지)
# =========================
def card_value(rank):
    if rank == "A": return 1
    if rank in ["10","J","Q","K"]: return 0
    return int(rank)

def create_shoe():
    deck = []
    for _ in range(8):
        for s in SUIT:
            for r in RANK:
                deck.append((r, s))
    random.shuffle(deck)
    return deck

def get_shoe(chat_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM shoe WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            deck = create_shoe()
            conn.execute(
                "INSERT INTO shoe(chat_id, cards, position) VALUES(?,?,0)",
                (chat_id, json.dumps(deck))
            )
            conn.commit()
            return deck, 0
        return json.loads(row["cards"]), row["position"]

def draw_card(chat_id):
    deck, pos = get_shoe(chat_id)

    if pos >= len(deck) - 6:
        deck = create_shoe()
        pos = 0

    card = deck[pos]
    pos += 1

    with db() as conn:
        conn.execute(
            "UPDATE shoe SET cards=?, position=? WHERE chat_id=?",
            (json.dumps(deck), pos, chat_id)
        )
        conn.commit()

    return card

# =========================
# 바카라 엔진 (진짜 3카드 룰)
# =========================
def play_baccarat(chat_id):
    player = [draw_card(chat_id), draw_card(chat_id)]
    banker = [draw_card(chat_id), draw_card(chat_id)]

    def total(hand):
        return sum(card_value(r) for r, s in hand) % 10

    p_total = total(player)
    b_total = total(banker)

    # 내추럴
    if p_total in [8,9] or b_total in [8,9]:
        return player, banker, p_total, b_total

    # 플레이어 3카드
    third = None
    if p_total <= 5:
        third = draw_card(chat_id)
        player.append(third)
        p_total = total(player)

    # 뱅커 규칙
    if third is None:
        if b_total <= 5:
            banker.append(draw_card(chat_id))
            b_total = total(banker)
    else:
        v = card_value(third[0])
        if b_total <= 2 or \
           (b_total == 3 and v != 8) or \
           (b_total == 4 and 2 <= v <= 7) or \
           (b_total == 5 and 4 <= v <= 7) or \
           (b_total == 6 and 6 <= v <= 7):
            banker.append(draw_card(chat_id))
            b_total = total(banker)

    return player, banker, p_total, b_total
    # =========================
# 빅로드 생성
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


# =========================
# 카지노 UI 이미지
# =========================
def draw_casino_ui(chat_id, round_id, player, banker, p_total, b_total, result):
    grid, ties = build_road(chat_id)

    cell = 32
    grid_cols = max(len(grid), 20)
    grid_rows = 6

    header_h = 150
    width = grid_cols * cell
    height = header_h + (grid_rows * cell) + 40

    img = Image.new("RGB", (width, height), "#0e0e0e")
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, header_h], fill="#1a1a1a")

    def fmt_hand(hand):
        return " ".join([f"{r}{s}" for r, s in hand])

    draw.text((20, 20), f"🎲 BACCARAT — {round_id}회차", fill="#d4af37")
    draw.text((20, 60), f"🔵 PLAYER: {fmt_hand(player)} ({p_total})", fill="#4aa3ff")
    draw.text((20, 95), f"🔴 BANKER: {fmt_hand(banker)} ({b_total})", fill="#ff4a4a")
    draw.text((width - 320, 70), f"RESULT → {BET_CHOICES[result]}", fill="#ffffff")

    top = header_h + 20

    for col in range(grid_cols):
        for row in range(grid_rows):
            x0 = col * cell
            y0 = top + row * cell
            x1 = x0 + cell
            y1 = y0 + cell
            draw.rectangle([x0, y0, x1, y1], outline="#555555")

    for col in range(len(grid)):
        for row in range(grid_rows):
            val = grid[col][row]
            if not val:
                continue

            x0 = col * cell + 4
            y0 = top + row * cell + 4
            x1 = (col + 1) * cell - 4
            y1 = top + (row + 1) * cell - 4

            if val == "P":
                draw.ellipse([x0, y0, x1, y1], fill="#1f4fff", outline="white", width=2)
            elif val == "B":
                draw.ellipse([x0, y0, x1, y1], fill="#ff2a2a", outline="white", width=2)

            if (col, row) in ties:
                draw.line([x0, y1, x1, y0], fill="#00ff6a", width=3)

    return img


# =========================
# 라운드 정산 (타이 환급 포함)
# =========================
async def settle_round(app, chat_id, round_id):
    player, banker, p_total, b_total = play_baccarat(chat_id)

    if p_total > b_total:
        result = "P"
    elif b_total > p_total:
        result = "B"
    else:
        result = "T"

    with db() as conn:
        bets = conn.execute(
            "SELECT * FROM bets WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchall()

    total_bet = 0
    total_payout = 0
    lines = []

    for b in bets:
        uid = b["user_id"]
        choice = b["choice"]
        amount = b["amount"]
        total_bet += amount

        if result == "T":
            if choice == "T":
                payout = int(amount * PAYOUTS["T"])
                set_points(uid, get_points(uid) + payout)
                total_payout += payout
                lines.append(f"🎯 {uid} +{payout}")
            else:
                set_points(uid, get_points(uid) + amount)
                lines.append(f"↩️ {uid} 원금 반환")
            continue

        if choice == result:
            payout = int(amount * PAYOUTS[result])
            set_points(uid, get_points(uid) + payout)
            total_payout += payout
            lines.append(f"✅ {uid} +{payout}")
        else:
            lines.append(f"❌ {uid}")

    # 하우스 계산
    with db() as conn:
        row = conn.execute(
            "SELECT profit, rounds FROM house WHERE chat_id=?",
            (chat_id,)
        ).fetchone()

        if not row:
            profit = 0
            rounds = 0
            conn.execute(
                "INSERT INTO house(chat_id, profit, rounds) VALUES(?,?,?)",
                (chat_id, 0, 0)
            )
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

    img = draw_casino_ui(chat_id, round_id, player, banker, p_total, b_total, result)
    path = f"casino_{chat_id}.png"
    img.save(path)

    await app.bot.send_photo(chat_id, photo=open(path, "rb"))
    await app.bot.send_message(chat_id, "\n".join(lines)) 
# =========================
# 채팅 적립 시스템
# =========================
async def on_message_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    today = datetime.now().strftime("%Y-%m-%d")

    with db() as conn:
        row = conn.execute(
            "SELECT msg_count, rewarded_steps FROM activity WHERE chat_id=? AND user_id=? AND day=?",
            (chat.id, u.id, today)
        ).fetchone()

        if row:
            msg_count = row["msg_count"] + 1
            rewarded = row["rewarded_steps"]
            conn.execute(
                "UPDATE activity SET msg_count=? WHERE chat_id=? AND user_id=? AND day=?",
                (msg_count, chat.id, u.id, today)
            )
        else:
            msg_count = 1
            rewarded = 0
            conn.execute(
                "INSERT INTO activity VALUES(?,?,?,?,?)",
                (chat.id, u.id, today, 1, 0)
            )

        steps = min(msg_count // ACTIVITY_STEP, ACTIVITY_MAX_STEPS_PER_DAY)

        if steps > rewarded:
            gain = (steps - rewarded) * ACTIVITY_REWARD
            set_points(u.id, get_points(u.id) + gain)

            conn.execute(
                "UPDATE activity SET rewarded_steps=? WHERE chat_id=? AND user_id=? AND day=?",
                (steps, chat.id, u.id, today)
            )

        conn.commit()


# =========================
# 기본 명령어
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    with db() as conn:
        r = conn.execute("SELECT * FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        rid = 1 if not r else r["round_id"] + 1
        conn.execute(
            "INSERT OR REPLACE INTO rounds(chat_id, round_id, status) VALUES(?,?,?)",
            (chat.id, rid, "OPEN")
        )
        conn.commit()

    asyncio.create_task(delayed_settle(context.application, chat.id, rid))
    await update.message.reply_text(f"🎰 라운드 {rid} 시작 (60초 후 결과)")


async def delayed_settle(app, chat_id, round_id):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(app, chat_id, round_id)


async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    if len(context.args) < 2:
        return

    amount = int(context.args[0])
    choice = context.args[1].upper()

    cur = get_points(u.id)
    if amount > cur:
        await update.message.reply_text("잔액 부족")
        return

    with db() as conn:
        r = conn.execute("SELECT round_id FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if not r:
            return
        conn.execute(
            "INSERT INTO bets VALUES(?,?,?,?,?)",
            (chat.id, r["round_id"], u.id, choice, amount)
        )
        conn.commit()

    set_points(u.id, cur - amount)
    await update.message.reply_text("베팅 완료")


async def cmd_allin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    if len(context.args) < 1:
        return

    cur = get_points(u.id)
    if cur <= 0:
        return

    choice = context.args[0].upper()

    with db() as conn:
        r = conn.execute("SELECT round_id FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if not r:
            return
        conn.execute(
            "INSERT INTO bets VALUES(?,?,?,?,?)",
            (chat.id, r["round_id"], u.id, choice, cur)
        )
        conn.commit()

    set_points(u.id, 0)
    await update.message.reply_text("💎 올인 완료")


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username)
    p = get_points(u.id)
    await update.message.reply_text(f"💰 현재 포인트: {p}")


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

    text = "🏆 TOP 10\n"
    for i, r in enumerate(rows, start=1):
        text += f"{i}. {r['username']} - {r['points']}\n"

    await update.message.reply_text(text)


async def cmd_house(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    with db() as conn:
        row = conn.execute(
            "SELECT profit, rounds FROM house WHERE chat_id=?",
            (chat.id,)
        ).fetchone()

    if not row:
        await update.message.reply_text("하우스 기록 없음")
        return

    await update.message.reply_text(
        f"🏦 누적 수익: {row['profit']}\n🎲 누적 라운드: {row['rounds']}"
    )


async def cmd_road(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    grid, ties = build_road(chat.id)

    if not grid:
        await update.message.reply_text("기록 없음")
        return

    img = draw_casino_ui(chat.id, 0, [], [], 0, 0, "P")
    path = f"road_{chat.id}.png"
    img.save(path)
    await update.message.reply_photo(photo=open(path, "rb"))


# =========================
# MAIN
# =========================
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("allin", cmd_allin))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("house", cmd_house))
    app.add_handler(CommandHandler("road", cmd_road))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_activity))

    app.run_polling()


if __name__ == "__main__":
    main()
