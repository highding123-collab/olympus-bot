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

STARTING_POINTS = 500000
ROUND_SECONDS = 60

DAILY_REWARD = 100000
SPIN_DAILY_LIMIT = 5

SPIN_TABLE = [
    (5000, 10),
    (10000, 25),
    (15000, 30),
    (30000, 18),
    (50000, 12),
    (100000, 4),
    (1000000, 1),
]

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

# --- DICE (느낌표 전용) ---
DICE_ROUND_SECONDS = 20
DICE_PAYOUT = {"BIG": 2.0, "SMALL": 2.0, "EXACT": 6.0}

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

KST = ZoneInfo("Asia/Seoul")


# ================== DB ==================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
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
            status TEXT NOT NULL  -- OPEN, CLOSING, CLOSED
        );

        -- One bet per user per round
        CREATE TABLE IF NOT EXISTS bets(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,
            amount INTEGER NOT NULL,
            PRIMARY KEY(chat_id, round_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS house(
            chat_id INTEGER PRIMARY KEY,
            profit INTEGER DEFAULT 0,
            rounds INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS road_history(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            result TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shoe(
            chat_id INTEGER PRIMARY KEY,
            cards TEXT NOT NULL,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_claims(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            PRIMARY KEY(chat_id, user_id, day)
        );

        CREATE TABLE IF NOT EXISTS spin_claims(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id, day)
        );

        -- ===== DICE TABLES =====
        CREATE TABLE IF NOT EXISTS dice_rounds(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL,  -- OPEN, CLOSING, CLOSED
            ends_at INTEGER NOT NULL
        );

        -- One dice bet per user per dice round
        CREATE TABLE IF NOT EXISTS dice_bets(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,     -- BIG, SMALL, EXACT
            exact_value INTEGER,        -- for EXACT 1~6
            amount INTEGER NOT NULL,
            PRIMARY KEY(chat_id, round_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS dice_history(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            dice_value INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)
        conn.commit()


# ================== USER ==================

def ensure_user(uid: int, username: str | None):
    with db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, points) VALUES(?,?,?)",
                (uid, username or "", STARTING_POINTS)
            )
            conn.commit()


def get_points(uid: int) -> int:
    with db() as conn:
        r = conn.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(r["points"]) if r else 0


def credit(uid: int, amount: int):
    if amount <= 0:
        return
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (amount, uid))
        conn.commit()


def try_debit(uid: int, amount: int) -> bool:
    if amount <= 0:
        return False
    with db() as conn:
        cur = conn.execute(
            "UPDATE users SET points = points - ? WHERE user_id=? AND points >= ?",
            (amount, uid, amount)
        )
        conn.commit()
        return cur.rowcount == 1


# ================== SHOE ==================

def card_value(rank: str) -> int:
    if rank == "A":
        return 1
    if rank in ["10", "J", "Q", "K"]:
        return 0
    return int(rank)


def create_shoe():
    deck = []
    for _ in range(8):
        for s in SUIT:
            for r in RANK:
                deck.append((r, s))
    random.shuffle(deck)
    return deck


def get_shoe(chat_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM shoe WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            deck = create_shoe()
            conn.execute(
                "INSERT INTO shoe(chat_id, cards, position) VALUES(?,?,?)",
                (chat_id, json.dumps(deck), 0)
            )
            conn.commit()
            return deck, 0
        return json.loads(row["cards"]), int(row["position"])


def draw_card(chat_id: int):
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


# ================== BACCARAT ENGINE ==================

def play_baccarat(chat_id: int):
    player = [draw_card(chat_id), draw_card(chat_id)]
    banker = [draw_card(chat_id), draw_card(chat_id)]

    def total(hand):
        return sum(card_value(r) for r, _ in hand) % 10

    p = total(player)
    b = total(banker)

    # natural
    if p in (8, 9) or b in (8, 9):
        return player, banker, p, b

    third = None
    if p <= 5:
        third = draw_card(chat_id)
        player.append(third)
        p = total(player)

    if third is None:
        if b <= 5:
            banker.append(draw_card(chat_id))
            b = total(banker)
    else:
        v = card_value(third[0])
        if (
            b <= 2 or
            (b == 3 and v != 8) or
            (b == 4 and 2 <= v <= 7) or
            (b == 5 and 4 <= v <= 7) or
            (b == 6 and 6 <= v <= 7)
        ):
            banker.append(draw_card(chat_id))
            b = total(banker)

    return player, banker, p, b


# ================== BIG ROAD ==================

def build_road(chat_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT result FROM road_history WHERE chat_id=? ORDER BY round_id",
            (chat_id,)
        ).fetchall()
    return [r["result"] for r in rows]


def draw_road_image_bytes(chat_id: int) -> BytesIO:
    results = build_road(chat_id)
    MAX_RESULTS = 200
    if len(results) > MAX_RESULTS:
        results = results[-MAX_RESULTS:]

    cell = 30
    cols = 40
    img = Image.new("RGB", (cols * cell, 6 * cell + 20), "#111")
    draw = ImageDraw.Draw(img)

    col = -1
    row = 0
    last = None

    for r in results:
        if r == "T":
            continue
        if r != last:
            col += 1
            row = 0
        if col >= cols:
            break

        x0 = col * cell + 5
        y0 = row * cell + 5
        x1 = x0 + 20
        y1 = y0 + 20
        color = "#1f4fff" if r == "P" else "#ff2a2a"
        draw.ellipse([x0, y0, x1, y1], fill=color)
        row += 1
        last = r

    bio = BytesIO()
    bio.name = f"road_{chat_id}.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


# ================== CARD REVEAL GIF (NO TTF DEPENDENCY) ==================

def make_reveal_gif(player, banker, p, b, result) -> BytesIO:
    """
    truetype 폰트가 없어도 '무조건' 카드 랭크/무늬가 보이게:
    - 텍스트는 load_default()로 찍은 뒤 NEAREST 확대(픽셀처럼 크게)
    - 무늬(♠♥♦♣)는 폰트가 아니라 도형으로 직접 그림
    """
    W, H = 900, 520
    bg = "#0b1220"
    table = "#0f2a1c"

    base_font = ImageFont.load_default()

    def draw_big_text(img: Image.Image, x: int, y: int, text: str, scale: int = 6, fill="#111111"):
        tmp = Image.new("RGBA", (260, 90), (0, 0, 0, 0))
        d = ImageDraw.Draw(tmp)
        d.text((0, 0), text, font=base_font, fill=fill)
        tmp = tmp.resize((tmp.size[0] * scale, tmp.size[1] * scale), resample=Image.NEAREST)
        img.paste(tmp, (x, y), tmp)

    def draw_suit(draw: ImageDraw.ImageDraw, cx: int, cy: int, suit: str):
        red = suit in ("♥", "♦")
        color = "#ef4444" if red else "#111111"

        if suit == "♦":
            pts = [(cx, cy - 26), (cx + 22, cy), (cx, cy + 26), (cx - 22, cy)]
            draw.polygon(pts, fill=color)

        elif suit == "♥":
            draw.ellipse([cx - 22, cy - 24, cx, cy - 2], fill=color)
            draw.ellipse([cx, cy - 24, cx + 22, cy - 2], fill=color)
            draw.polygon([(cx - 24, cy - 8), (cx + 24, cy - 8), (cx, cy + 30)], fill=color)

        elif suit == "♣":
            draw.ellipse([cx - 10, cy - 34, cx + 10, cy - 14], fill=color)
            draw.ellipse([cx - 26, cy - 16, cx - 6, cy + 4], fill=color)
            draw.ellipse([cx + 6, cy - 16, cx + 26, cy + 4], fill=color)
            draw.polygon([(cx - 6, cy + 4), (cx + 6, cy + 4), (cx, cy + 30)], fill=color)

        elif suit == "♠":
            draw.ellipse([cx - 22, cy - 4, cx, cy + 18], fill=color)
            draw.ellipse([cx, cy - 4, cx + 22, cy + 18], fill=color)
            draw.polygon([(cx - 24, cy + 10), (cx + 24, cy + 10), (cx, cy - 26)], fill=color)
            draw.polygon([(cx - 6, cy + 18), (cx + 6, cy + 18), (cx, cy + 44)], fill=color)

    def base_frame(title_text=None, highlight=None):
        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle([30, 40, W - 30, H - 40], radius=30, fill=table, outline="#1f2937", width=4)
        draw_big_text(img, 55, 55, "PLAYER", scale=5, fill="#60a5fa")
        draw_big_text(img, W - 275, 55, "BANKER", scale=5, fill="#fb7185")

        if title_text:
            draw_big_text(img, W // 2 - 130, 58, title_text, scale=4, fill="#fbbf24")

        if highlight == "P":
            draw.rounded_rectangle([40, 45, W // 2 - 20, H - 50], radius=28, outline="#60a5fa", width=6)
        elif highlight == "B":
            draw.rounded_rectangle([W // 2 + 20, 45, W - 40, H - 50], radius=28, outline="#fb7185", width=6)
        elif highlight == "T":
            draw_big_text(img, W // 2 - 30, 410, "TIE", scale=6, fill="#fbbf24")

        return img, draw

    def draw_card_face(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, r: str, s: str, face_up=True):
        cw, ch = 110, 155
        if face_up:
            draw.rounded_rectangle([x, y, x + cw, y + ch], radius=12, fill="#f8fafc", outline="#94a3b8", width=3)
            draw_big_text(img, x + 10, y + 8, str(r), scale=7, fill="#111111")
            draw_suit(draw, x + 34, y + 90, s)
        else:
            draw.rounded_rectangle([x, y, x + cw, y + ch], radius=12, fill="#1e293b", outline="#64748b", width=3)
            for i in range(0, cw, 12):
                draw.line([x + i, y, x, y + i], fill="#334155", width=2)
                draw.line([x + cw - i, y + ch, x + cw, y + ch - i], fill="#334155", width=2)

    px0, py0 = 70, 130
    bx0, by0 = W - 70 - (110 * 3 + 20 * 2), 130
    gap = 20

    reveal_steps = []
    if len(player) >= 1: reveal_steps.append(("P", 0))
    if len(banker) >= 1: reveal_steps.append(("B", 0))
    if len(player) >= 2: reveal_steps.append(("P", 1))
    if len(banker) >= 2: reveal_steps.append(("B", 1))
    if len(player) >= 3: reveal_steps.append(("P", 2))
    if len(banker) >= 3: reveal_steps.append(("B", 2))

    frames, durations = [], []
    shown_p, shown_b = set(), set()

    # frame 0: all back
    img, draw = base_frame("Revealing...", None)
    for i in range(3):
        if i < len(player):
            draw_card_face(img, draw, px0 + i * (110 + gap), py0, "?", "♠", face_up=False)
        if i < len(banker):
            draw_card_face(img, draw, bx0 + i * (110 + gap), by0, "?", "♠", face_up=False)
    frames.append(img)
    durations.append(500)

    # reveal one by one
    for side, idx in reveal_steps:
        (shown_p if side == "P" else shown_b).add(idx)

        img, draw = base_frame("Revealing...", None)

        for i in range(len(player)):
            r, s = player[i]
            draw_card_face(img, draw, px0 + i * (110 + gap), py0, r, s, face_up=(i in shown_p))

        for i in range(len(banker)):
            r, s = banker[i]
            draw_card_face(img, draw, bx0 + i * (110 + gap), by0, r, s, face_up=(i in shown_b))

        draw_big_text(img, 55, 320, f"TOTAL: {p}", scale=5, fill="#e2e8f0")
        draw_big_text(img, W - 270, 320, f"TOTAL: {b}", scale=5, fill="#e2e8f0")

        frames.append(img)
        durations.append(450)

    # final frame
    highlight = result if result in ("P", "B") else "T"
    img, draw = base_frame("RESULT", highlight)

    for i in range(len(player)):
        r, s = player[i]
        draw_card_face(img, draw, px0 + i * (110 + gap), py0, r, s, face_up=True)

    for i in range(len(banker)):
        r, s = banker[i]
        draw_card_face(img, draw, bx0 + i * (110 + gap), by0, r, s, face_up=True)

    draw_big_text(img, 55, 320, f"TOTAL: {p}", scale=5, fill="#e2e8f0")
    draw_big_text(img, W - 270, 320, f"TOTAL: {b}", scale=5, fill="#e2e8f0")
    draw_big_text(img, W // 2 - 200, 410, f"RESULT: {BET_CHOICES.get(result, result)}", scale=5, fill="#fbbf24")

    frames.append(img)
    durations.append(1400)

    bio = BytesIO()
    bio.name = "reveal.gif"
    frames[0].save(
        bio,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    bio.seek(0)
    return bio


# ================== BACCARAT SETTLEMENT ==================

async def settle_round(app: Application, chat_id: int, round_id: int):
    # guard: settle only once
    with db() as conn:
        r = conn.execute(
            "SELECT status FROM rounds WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchone()
        if not r or r["status"] != "OPEN":
            return
        conn.execute("UPDATE rounds SET status='CLOSING' WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        conn.commit()

    player, banker, p, b = play_baccarat(chat_id)

    if p > b:
        result = "P"
    elif b > p:
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
    lines = [f"🎲 결과: {BET_CHOICES.get(result, result)}  (P:{p} / B:{b})"]

    for bet in bets:
        uid = int(bet["user_id"])
        choice = bet["choice"]
        amt = int(bet["amount"])
        total_bet += amt

        if result == "T":
            if choice == "T":
                payout = int(amt * PAYOUTS["T"])
                credit(uid, payout)
                total_payout += payout
                lines.append(f"🎯 {uid} +{payout}")
            else:
                # refund and count in payout for correct house accounting
                credit(uid, amt)
                total_payout += amt
                lines.append(f"↩️ {uid} 환급 +{amt}")
            continue

        if choice == result:
            payout = int(amt * PAYOUTS[result])
            credit(uid, payout)
            total_payout += payout
            lines.append(f"✅ {uid} +{payout}")
        else:
            lines.append(f"❌ {uid} -{amt}")

    with db() as conn:
        row = conn.execute("SELECT * FROM house WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO house(chat_id, profit, rounds) VALUES(?,?,?)", (chat_id, 0, 0))
            profit = 0
            rounds = 0
        else:
            profit = int(row["profit"])
            rounds = int(row["rounds"])

        profit += (total_bet - total_payout)
        rounds += 1

        conn.execute("UPDATE house SET profit=?, rounds=? WHERE chat_id=?", (profit, rounds, chat_id))
        conn.execute("INSERT INTO road_history(chat_id, round_id, result) VALUES(?,?,?)", (chat_id, round_id, result))
        conn.execute("DELETE FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        conn.execute("UPDATE rounds SET status='CLOSED' WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        conn.commit()

    # 1) reveal gif
    reveal_gif = make_reveal_gif(player, banker, p, b, result)
    await app.bot.send_animation(chat_id, animation=reveal_gif)

    # 2) big road
    road_img = draw_road_image_bytes(chat_id)
    await app.bot.send_photo(chat_id, photo=road_img)

    # 3) settlement text
    msg = "\n".join(lines)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(생략)"
    await app.bot.send_message(chat_id, msg)


async def delayed_settle(app: Application, chat_id: int, rid: int):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(app, chat_id, rid)


# ================== BACCARAT COMMANDS ==================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    with db() as conn:
        r = conn.execute("SELECT round_id, status FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if r and r["status"] == "OPEN":
            await update.message.reply_text(f"이미 라운드 {r['round_id']} 진행중이야. ({ROUND_SECONDS}초 마감)")
            return

        rid = 1 if not r else int(r["round_id"]) + 1
        conn.execute("INSERT OR REPLACE INTO rounds(chat_id, round_id, status) VALUES(?,?,?)", (chat.id, rid, "OPEN"))
        conn.commit()

    asyncio.create_task(delayed_settle(context.application, chat.id, rid))
    await update.message.reply_text(f"라운드 {rid} 시작!  /bet <금액> <P|B|T>   (마감 {ROUND_SECONDS}초)")


async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    if len(context.args) < 2:
        await update.message.reply_text("사용법: /bet <금액> <P|B|T>")
        return

    try:
        amt = int(context.args[0])
    except ValueError:
        await update.message.reply_text("금액은 숫자로 입력해줘.")
        return

    choice = context.args[1].upper()
    if choice not in BET_CHOICES:
        await update.message.reply_text("선택은 P/B/T 중 하나야.")
        return
    if amt <= 0:
        await update.message.reply_text("금액은 1 이상이어야 해.")
        return

    with db() as conn:
        r = conn.execute("SELECT round_id, status FROM rounds WHERE chat_id=?", (chat.id,)).fetchone()
        if not r or r["status"] != "OPEN":
            await update.message.reply_text("지금은 라운드가 열려있지 않아. /start 로 시작해줘.")
            return
        rid = int(r["round_id"])

        exists = conn.execute(
            "SELECT 1 FROM bets WHERE chat_id=? AND round_id=? AND user_id=?",
            (chat.id, rid, u.id)
        ).fetchone()
        if exists:
            await update.message.reply_text("이번 라운드에는 이미 베팅했어.")
            return

    if not try_debit(u.id, amt):
        await update.message.reply_text("잔액 부족")
        return

    with db() as conn:
        conn.execute(
            "INSERT INTO bets(chat_id, round_id, user_id, choice, amount) VALUES(?,?,?,?,?)",
            (chat.id, rid, u.id, choice, amt)
        )
        conn.commit()

    await update.message.reply_text(f"베팅 완료 ✅  {amt} / {BET_CHOICES[choice]}   (잔액: {get_points(u.id)})")


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    today = datetime.now(KST).strftime("%Y-%m-%d")

    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_claims WHERE chat_id=? AND user_id=? AND day=?",
            (chat.id, u.id, today)
        ).fetchone()
        if row:
            await update.message.reply_text("이미 오늘 출석 보상 받았어.")
            return
        conn.execute("INSERT INTO daily_claims(chat_id, user_id, day) VALUES(?,?,?)", (chat.id, u.id, today))
        conn.commit()

    credit(u.id, DAILY_REWARD)
    await update.message.reply_text(f"출석 보상 +{DAILY_REWARD}  (잔액: {get_points(u.id)})")


async def cmd_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    today = datetime.now(KST).strftime("%Y-%m-%d")

    with db() as conn:
        row = conn.execute(
            "SELECT used FROM spin_claims WHERE chat_id=? AND user_id=? AND day=?",
            (chat.id, u.id, today)
        ).fetchone()
        used = int(row["used"]) if row else 0

        if used >= SPIN_DAILY_LIMIT:
            await update.message.reply_text("오늘 룰렛은 다 썼어.")
            return

        rewards = [r for r, w in SPIN_TABLE]
        weights = [w for r, w in SPIN_TABLE]
        prize = random.choices(rewards, weights=weights, k=1)[0]

        if row:
            conn.execute(
                "UPDATE spin_claims SET used=? WHERE chat_id=? AND user_id=? AND day=?",
                (used + 1, chat.id, u.id, today)
            )
        else:
            conn.execute(
                "INSERT INTO spin_claims(chat_id, user_id, day, used) VALUES(?,?,?,?)",
                (chat.id, u.id, today, 1)
            )
        conn.commit()

    credit(u.id, prize)
    await update.message.reply_text(
        f"룰렛 🎰 +{prize}  (남은 횟수: {SPIN_DAILY_LIMIT - (used + 1)} / 잔액: {get_points(u.id)})"
    )


async def cmd_road(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    road_img = draw_road_image_bytes(chat.id)
    await update.message.reply_photo(photo=road_img)


async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username)
    await update.message.reply_text(f"잔액: {get_points(u.id)}")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("SELECT user_id, username, points FROM users ORDER BY points DESC LIMIT 10").fetchall()

    if not rows:
        await update.message.reply_text("랭킹 데이터가 없어.")
        return

    lines = ["🏆 TOP 10"]
    for i, r in enumerate(rows, 1):
        name = r["username"] or str(r["user_id"])
        lines.append(f"{i}. {name} ({r['user_id']}): {r['points']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_house(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    with db() as conn:
        row = conn.execute("SELECT profit, rounds FROM house WHERE chat_id=?", (chat.id,)).fetchone()

    if not row:
        await update.message.reply_text("하우스 기록이 아직 없어.")
        return

    await update.message.reply_text(f"🏦 하우스\n누적 수익: {row['profit']}\n진행 라운드: {row['rounds']}")


# ================== DICE (GIF) ==================

def _die_frame(value: int, size: int = 300) -> Image.Image:
    img = Image.new("RGB", (size, size), "#0b1220")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([22, 22, size - 22, size - 22], radius=42, fill="#f8fafc", outline="#94a3b8", width=6)

    def dot(cx, cy, r=18):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#111111")

    x1, x2, x3 = int(size * 0.30), int(size * 0.50), int(size * 0.70)
    y1, y2, y3 = int(size * 0.30), int(size * 0.50), int(size * 0.70)

    pips = {
        1: [(x2, y2)],
        2: [(x1, y1), (x3, y3)],
        3: [(x1, y1), (x2, y2), (x3, y3)],
        4: [(x1, y1), (x3, y1), (x1, y3), (x3, y3)],
        5: [(x1, y1), (x3, y1), (x2, y2), (x1, y3), (x3, y3)],
        6: [(x1, y1), (x3, y1), (x1, y2), (x3, y2), (x1, y3), (x3, y3)],
    }
    for cx, cy in pips[value]:
        dot(cx, cy)
    return img


def make_dice_gif(final_value: int) -> BytesIO:
    # 흔들리는 느낌: 랜덤 몇 프레임 + 마지막 결과 프레임
    frames = []
    durations = []

    for _ in range(7):
        frames.append(_die_frame(random.randint(1, 6)))
        durations.append(120)

    frames.append(_die_frame(final_value))
    durations.append(900)

    bio = BytesIO()
    bio.name = "dice.gif"
    frames[0].save(
        bio,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    bio.seek(0)
    return bio


def get_dice_state(chat_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM dice_rounds WHERE chat_id=?", (chat_id,)).fetchone()


def dice_win(bet_type: str, exact_value: int | None, dice_value: int) -> bool:
    if bet_type == "BIG":
        return dice_value >= 4
    if bet_type == "SMALL":
        return dice_value <= 3
    if bet_type == "EXACT":
        return exact_value == dice_value
    return False


async def settle_dice_round(app: Application, chat_id: int, rid: int):
    # settle only once
    with db() as conn:
        r = conn.execute(
            "SELECT status FROM dice_rounds WHERE chat_id=? AND round_id=?",
            (chat_id, rid)
        ).fetchone()
        if not r or r["status"] != "OPEN":
            return
        conn.execute("UPDATE dice_rounds SET status='CLOSING' WHERE chat_id=? AND round_id=?", (chat_id, rid))
        conn.commit()

        bets = conn.execute(
            "SELECT * FROM dice_bets WHERE chat_id=? AND round_id=?",
            (chat_id, rid)
        ).fetchall()

    dice_value = random.randint(1, 6)

    lines = [f"🎲 다이스 결과: {dice_value}"]
    total_bet = 0
    total_payout = 0
    winners = 0

    for bet in bets:
        uid = int(bet["user_id"])
        bet_type = bet["bet_type"]
        exact_value = bet["exact_value"]
        amt = int(bet["amount"])
        total_bet += amt

        if dice_win(bet_type, exact_value, dice_value):
            payout = int(amt * DICE_PAYOUT[bet_type])
            credit(uid, payout)
            total_payout += payout
            winners += 1
            if bet_type == "EXACT":
                lines.append(f"✅ {uid} EXACT({exact_value}) +{payout}")
            else:
                lines.append(f"✅ {uid} {bet_type} +{payout}")
        else:
            if bet_type == "EXACT":
                lines.append(f"❌ {uid} EXACT({exact_value}) -{amt}")
            else:
                lines.append(f"❌ {uid} {bet_type} -{amt}")

    # DB 정리
    now_ts = int(datetime.now().timestamp())
    with db() as conn:
        conn.execute(
            "INSERT INTO dice_history(chat_id, round_id, dice_value, created_at) VALUES(?,?,?,?)",
            (chat_id, rid, dice_value, now_ts)
        )
        conn.execute("DELETE FROM dice_bets WHERE chat_id=? AND round_id=?", (chat_id, rid))
        conn.execute("UPDATE dice_rounds SET status='CLOSED' WHERE chat_id=? AND round_id=?", (chat_id, rid))
        conn.commit()

    gif = make_dice_gif(dice_value)
    await app.bot.send_animation(chat_id, animation=gif)

    summary = f"✅ 당첨 {winners}명 | 지급합 {total_payout:,} | 총배팅 {total_bet:,}"
    msg = "\n".join([summary] + lines)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(생략)"
    await app.bot.send_message(chat_id, msg)


async def delayed_dice_settle(app: Application, chat_id: int, rid: int, ends_at: int):
    now = int(datetime.now().timestamp())
    wait = max(0, ends_at - now)
    await asyncio.sleep(wait)
    await settle_dice_round(app, chat_id, rid)


async def dice_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # !dice_start
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    r = get_dice_state(chat.id)
    if r and r["status"] == "OPEN":
        remain = max(0, int(r["ends_at"]) - int(datetime.now().timestamp()))
        await update.message.reply_text(f"이미 다이스 라운드 {r['round_id']} 진행중! (남은 {remain}s)")
        return

    rid = 1 if not r else int(r["round_id"]) + 1
    ends_at = int(datetime.now().timestamp()) + DICE_ROUND_SECONDS

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dice_rounds(chat_id, round_id, status, ends_at) VALUES(?,?,?,?)",
            (chat.id, rid, "OPEN", ends_at)
        )
        conn.commit()

    asyncio.create_task(delayed_dice_settle(context.application, chat.id, rid, ends_at))

    await update.message.reply_text(
        "🎲 다이스 시작!\n"
        f"라운드 {rid} / 마감 {DICE_ROUND_SECONDS}초\n"
        "!dice_bet BIG 1000 | !dice_bet SMALL 1000 | !dice_bet EXACT 3 1000"
    )


async def dice_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # !dice_stop : 현재 OPEN 라운드를 CLOSED로 바꾸고 베팅은 그대로 삭제(정리)
    chat = update.effective_chat
    r = get_dice_state(chat.id)
    if not r or r["status"] != "OPEN":
        await update.message.reply_text("열려있는 다이스 라운드가 없어.")
        return

    rid = int(r["round_id"])
    with db() as conn:
        conn.execute("UPDATE dice_rounds SET status='CLOSED' WHERE chat_id=? AND round_id=?", (chat.id, rid))
        conn.execute("DELETE FROM dice_bets WHERE chat_id=? AND round_id=?", (chat.id, rid))
        conn.commit()

    await update.message.reply_text("🛑 다이스 라운드 중지 + 베팅 초기화 완료. 다시: !dice_start")


async def dice_round_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    r = get_dice_state(chat.id)
    if not r:
        await update.message.reply_text("다이스 라운드 없음. !dice_start 로 시작해줘.")
        return
    remain = max(0, int(r["ends_at"]) - int(datetime.now().timestamp()))
    await update.message.reply_text(f"🎲 다이스 라운드 {r['round_id']}\n상태: {r['status']}\n남은시간: {remain}s")


async def dice_bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    """
    !dice_bet BIG 1000
    !dice_bet SMALL 1000
    !dice_bet EXACT 3 1000
    """
    u = update.effective_user
    chat = update.effective_chat
    ensure_user(u.id, u.username)

    r = get_dice_state(chat.id)
    if not r or r["status"] != "OPEN":
        await update.message.reply_text("지금은 다이스 라운드가 열려있지 않아. !dice_start")
        return

    rid = int(r["round_id"])

    if len(parts) not in (3, 4):
        await update.message.reply_text(
            "사용법:\n"
            "!dice_bet BIG 1000\n"
            "!dice_bet SMALL 1000\n"
            "!dice_bet EXACT 3 1000"
        )
        return

    bet_type = parts[1].upper()
    exact_value = None

    try:
        if bet_type in ("BIG", "SMALL"):
            amount = int(parts[2])
        elif bet_type == "EXACT":
            exact_value = int(parts[2])
            amount = int(parts[3])
            if not (1 <= exact_value <= 6):
                await update.message.reply_text("EXACT 값은 1~6만 가능")
                return
        else:
            await update.message.reply_text("종류는 BIG / SMALL / EXACT 중 하나야.")
            return
    except ValueError:
        await update.message.reply_text("숫자는 숫자로 입력해줘.")
        return

    if amount <= 0:
        await update.message.reply_text("금액은 1 이상이어야 해.")
        return

    # 이미 베팅했는지 먼저 체크
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM dice_bets WHERE chat_id=? AND round_id=? AND user_id=?",
            (chat.id, rid, u.id)
        ).fetchone()
    if exists:
        await update.message.reply_text("이번 다이스 라운드에는 이미 베팅했어. (라운드당 1회)")
        return

    # 포인트 차감
    if not try_debit(u.id, amount):
        await update.message.reply_text("잔액 부족")
        return

    # DB 기록
    with db() as conn:
        conn.execute(
            "INSERT INTO dice_bets(chat_id, round_id, user_id, bet_type, exact_value, amount) VALUES(?,?,?,?,?,?)",
            (chat.id, rid, u.id, bet_type, exact_value, amount)
        )
        conn.commit()

    desc = f"EXACT({exact_value})" if bet_type == "EXACT" else bet_type
    await update.message.reply_text(f"다이스 베팅 완료 ✅  {amount} / {desc}   (잔액: {get_points(u.id)})")


# ================== ! MESSAGE ROUTER ==================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("!"):
        return

    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "!dice_start":
        await dice_start_cmd(update, context)
    elif cmd == "!dice_stop":
        await dice_stop_cmd(update, context)
    elif cmd == "!dice_round":
        await dice_round_cmd(update, context)
    elif cmd == "!dice_bet":
        await dice_bet_cmd(update, context, parts)
    elif cmd == "!dice_help":
        await update.message.reply_text(
            "🎲 다이스 명령어 (! 전용)\n"
            "!dice_start\n"
            "!dice_bet BIG 1000\n"
            "!dice_bet SMALL 1000\n"
            "!dice_bet EXACT 3 1000\n"
            "!dice_round\n"
            "!dice_stop"
        )
    else:
        await update.message.reply_text("알 수 없는 !명령어야. !dice_help 를 쳐봐.")


# ================== MAIN ==================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    # 바카라 (/)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("road", cmd_road))
    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("house", cmd_house))

    # 다이스 (!)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()
