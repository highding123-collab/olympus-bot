import os
import sqlite3
import random
import asyncio
import json
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import PIL
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from PIL import Image, ImageDraw, ImageFont

# ================== CONFIG ==================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "casino.db"

STARTING_POINTS = 200_000
ROUND_SECONDS = 60

DAILY_REWARD = 10_000
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

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

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

        -- One bet per user per round (simple & safe)
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


# ================== CARD REVEAL GIF (FIXED FONTS) ==================

def _load_font(size: int) -> ImageFont.ImageFont:
    """
    확실히 글자/문양(♠♥♦♣) 보이게 Pillow 내장 DejaVuSans 경로를 우선 탐색.
    """
    pil_font = os.path.join(os.path.dirname(PIL.__file__), "fonts", "DejaVuSans.ttf")
    candidates = [
        pil_font,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_with_shadow(draw: ImageDraw.ImageDraw, xy, text: str, font, fill: str,
                      shadow: str = "#000000", offset: int = 2):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def make_reveal_gif(player, banker, p, b, result) -> BytesIO:
    W, H = 900, 520
    bg = "#0b1220"
    table = "#0f2a1c"

    font_big = _load_font(52)   # 크게!
    font_mid = _load_font(34)
    font_small = _load_font(22)

    def suit_color(s):
        return "#ff4b4b" if s in ["♥", "♦"] else "#111111"

    def draw_card_face(draw, x, y, r, s, face_up=True):
        cw, ch = 100, 145
        if face_up:
            draw.rounded_rectangle(
                [x, y, x + cw, y + ch],
                radius=12, fill="#f8fafc", outline="#94a3b8", width=3
            )
            # rank + suit (shadow for readability)
            _text_with_shadow(draw, (x + 12, y + 10), f"{r}", font_mid, "#111111", shadow="#cbd5e1", offset=1)
            _text_with_shadow(draw, (x + 12, y + 55), f"{s}", font_big, suit_color(s), shadow="#cbd5e1", offset=1)
        else:
            draw.rounded_rectangle(
                [x, y, x + cw, y + ch],
                radius=12, fill="#1e293b", outline="#64748b", width=3
            )
            for i in range(0, cw, 12):
                draw.line([x + i, y, x, y + i], fill="#334155", width=2)
                draw.line([x + cw - i, y + ch, x + cw, y + ch - i], fill="#334155", width=2)

    def base_frame(title_text=None, highlight=None):
        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle([30, 40, W - 30, H - 40], radius=30, fill=table, outline="#1f2937", width=4)
        _text_with_shadow(draw, (55, 60), "PLAYER", font_mid, "#60a5fa", shadow="#000000", offset=2)
        _text_with_shadow(draw, (W - 260, 60), "BANKER", font_mid, "#fb7185", shadow="#000000", offset=2)

        if title_text:
            _text_with_shadow(draw, (W // 2 - 120, 62), title_text, font_small, "#fbbf24", shadow="#000000", offset=2)

        if highlight == "P":
            draw.rounded_rectangle([40, 45, W // 2 - 20, H - 50], radius=28, outline="#60a5fa", width=6)
        elif highlight == "B":
            draw.rounded_rectangle([W // 2 + 20, 45, W - 40, H - 50], radius=28, outline="#fb7185", width=6)
        elif highlight == "T":
            _text_with_shadow(draw, (W // 2 - 40, 420), "TIE", font_mid, "#fbbf24", shadow="#000000", offset=2)

        return img, draw

    # layout
    px0, py0 = 70, 130
    bx0, by0 = W - 70 - (100 * 3 + 20 * 2), 130
    gap = 20

    # reveal order
    reveal_steps = []
    if len(player) >= 1: reveal_steps.append(("P", 0))
    if len(banker) >= 1: reveal_steps.append(("B", 0))
    if len(player) >= 2: reveal_steps.append(("P", 1))
    if len(banker) >= 2: reveal_steps.append(("B", 1))
    if len(player) >= 3: reveal_steps.append(("P", 2))
    if len(banker) >= 3: reveal_steps.append(("B", 2))

    frames = []
    durations = []

    shown_p = set()
    shown_b = set()

    # frame 0: all back
    img, draw = base_frame("Revealing...", None)
    for i in range(3):
        if i < len(player):
            draw_card_face(draw, px0 + i * (100 + gap), py0, "?", "?", face_up=False)
        if i < len(banker):
            draw_card_face(draw, bx0 + i * (100 + gap), by0, "?", "?", face_up=False)
    frames.append(img)
    durations.append(500)

    # reveal one by one
    for side, idx in reveal_steps:
        if side == "P":
            shown_p.add(idx)
        else:
            shown_b.add(idx)

        img, draw = base_frame("Revealing...", None)

        for i in range(len(player)):
            r, s = player[i]
            draw_card_face(draw, px0 + i * (100 + gap), py0, r, s, face_up=(i in shown_p))

        for i in range(len(banker)):
            r, s = banker[i]
            draw_card_face(draw, bx0 + i * (100 + gap), by0, r, s, face_up=(i in shown_b))

        _text_with_shadow(draw, (55, 320), f"TOTAL: {p}", font_mid, "#e2e8f0", shadow="#000000", offset=2)
        _text_with_shadow(draw, (W - 260, 320), f"TOTAL: {b}", font_mid, "#e2e8f0", shadow="#000000", offset=2)

        frames.append(img)
        durations.append(450)

    # final frame
    highlight = result if result in ("P", "B") else "T"
    img, draw = base_frame("RESULT", highlight)

    for i in range(len(player)):
        r, s = player[i]
        draw_card_face(draw, px0 + i * (100 + gap), py0, r, s, face_up=True)

    for i in range(len(banker)):
        r, s = banker[i]
        draw_card_face(draw, bx0 + i * (100 + gap), by0, r, s, face_up=True)

    _text_with_shadow(draw, (55, 320), f"TOTAL: {p}", font_mid, "#e2e8f0", shadow="#000000", offset=2)
    _text_with_shadow(draw, (W - 260, 320), f"TOTAL: {b}", font_mid, "#e2e8f0", shadow="#000000", offset=2)

    result_text = f"RESULT: {BET_CHOICES.get(result, result)}"
    _text_with_shadow(draw, (W // 2 - 190, 420), result_text, font_mid, "#fbbf24", shadow="#000000", offset=3)

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


# ================== SETTLEMENT ==================

async def settle_round(app: Application, chat_id: int, round_id: int):
    # guard against double settlement
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
                # refund (and count it in payout for correct house accounting)
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

    # 3) settlement lines
    msg = "\n".join(lines)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(생략)"
    await app.bot.send_message(chat_id, msg)


async def delayed_settle(app: Application, chat_id: int, rid: int):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(app, chat_id, rid)


# ================== COMMANDS ==================

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


# ================== MAIN ==================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("road", cmd_road))

    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("house", cmd_house))

    app.run_polling()


if __name__ == "__main__":
    main()
