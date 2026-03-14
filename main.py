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

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "baccarat_sim.db")

ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):uzisgod
    try:
        ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip()}
    except Exception:
        ADMIN_IDS = set()

ROUND_SECONDS = 60
DECKS = 8

STARTING_POINTS = 200000
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

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

RESULT_TEXT = {"P": "PLAYER", "B": "BANKER", "T": "TIE"}

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS shoe(
            chat_id INTEGER PRIMARY KEY,
            cards TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS road_history(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, round_id)
        );

        CREATE TABLE IF NOT EXISTS round_state(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            closes_at TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_claims(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            PRIMARY KEY(chat_id, user_id, day_utc)
        );

        CREATE TABLE IF NOT EXISTS spin_claims(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id, day_utc)
        );

        CREATE TABLE IF NOT EXISTS activity(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            rewarded_steps INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id, day_utc)
        );

        CREATE TABLE IF NOT EXISTS picks(
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, round_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS pick_stats(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            max_streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        """)
        conn.commit()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def ensure_user(user_id: int, username: str | None):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, points) VALUES(?,?,?)",
                (user_id, username or "", STARTING_POINTS)
            )
        else:
            conn.execute(
                "UPDATE users SET username=? WHERE user_id=?",
                (username or row["username"] or "", user_id)
            )
        conn.commit()

def get_points(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["points"]) if row else 0

def set_points(user_id: int, points: int):
    with db() as conn:
        conn.execute("UPDATE users SET points=? WHERE user_id=?", (points, user_id))
        conn.commit()

def add_points(user_id: int, delta: int):
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (delta, user_id))
        conn.commit()

def card_value(rank: str) -> int:
    if rank == "A":
        return 1
    if rank in ("10", "J", "Q", "K"):
        return 0
    return int(rank)

def create_shoe():
    deck = []
    for _ in range(DECKS):
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
                "INSERT INTO shoe(chat_id, cards, position) VALUES(?,?,0)",
                (chat_id, json.dumps(deck))
            )
            conn.commit()
            return deck, 0
        return json.loads(row["cards"]), int(row["position"])

def set_shoe(chat_id: int, deck, pos: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO shoe(chat_id, cards, position) VALUES(?,?,?)",
            (chat_id, json.dumps(deck), pos)
        )
        conn.commit()

def draw_card(chat_id: int):
    deck, pos = get_shoe(chat_id)
    if pos >= len(deck) - 6:
        deck = create_shoe()
        pos = 0
    card = deck[pos]
    pos += 1
    set_shoe(chat_id, deck, pos)
    return card

def shoe_remaining(chat_id: int) -> int:
    deck, pos = get_shoe(chat_id)
    return max(0, len(deck) - pos)

def hand_total(hand) -> int:
    return sum(card_value(r) for r, _s in hand) % 10

def play_baccarat(chat_id: int):
    player = [draw_card(chat_id), draw_card(chat_id)]
    banker = [draw_card(chat_id), draw_card(chat_id)]

    p = hand_total(player)
    b = hand_total(banker)

    if p in (8, 9) or b in (8, 9):
        return player, banker, p, b

    player_third = None
    if p <= 5:
        player_third = draw_card(chat_id)
        player.append(player_third)
        p = hand_total(player)

    if player_third is None:
        if b <= 5:
            banker.append(draw_card(chat_id))
            b = hand_total(banker)
    else:
        third_val = card_value(player_third[0])
        if b <= 2:
            banker.append(draw_card(chat_id))
        elif b == 3 and third_val != 8:
            banker.append(draw_card(chat_id))
        elif b == 4 and 2 <= third_val <= 7:
            banker.append(draw_card(chat_id))
        elif b == 5 and 4 <= third_val <= 7:
            banker.append(draw_card(chat_id))
        elif b == 6 and 6 <= third_val <= 7:
            banker.append(draw_card(chat_id))
        b = hand_total(banker)

    return player, banker, p, b

def result_from_totals(p: int, b: int) -> str:
    if p > b:
        return "P"
    if b > p:
        return "B"
    return "T"

def fmt_hand(hand) -> str:
    return " ".join([f"{r}{s}" for r, s in hand])

def next_round_id(chat_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(round_id) AS m FROM road_history WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
        m = row["m"]
        return 1 if m is None else int(m) + 1

def save_road(chat_id: int, round_id: int, result: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO road_history(chat_id, round_id, result, created_at) VALUES(?,?,?,?)",
            (chat_id, round_id, result, now_iso())
        )
        conn.commit()

def build_road(chat_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT result FROM road_history WHERE chat_id=? ORDER BY round_id",
            (chat_id,)
        ).fetchall()
    return [r["result"] for r in rows]

def draw_road_image(chat_id: int):
    results = build_road(chat_id)

    max_cols = 40
    if len(results) > max_cols:
        results = results[-max_cols:]

    cell = 50
    rows = 6
    cols = max(max_cols, len(results))

    img = Image.new("RGB", (cols * cell, rows * cell), "white")
    draw = ImageDraw.Draw(img)

    for c in range(cols + 1):
        x = c * cell
        draw.line((x, 0, x, rows * cell), fill="black", width=1)

    for r in range(rows + 1):
        y = r * cell
        draw.line((0, y, cols * cell, y), fill="black", width=1)

    col = -1
    row = 0
    last = None
    tie_marks = []

    for result in results:
        if result == "T":
            if col >= 0:
                tie_marks.append((col, max(0, row - 1)))
            continue

        if result != last:
            col += 1
            row = 0
        else:
            row += 1
            if row >= rows:
                row = rows - 1
                col += 1

        if col >= cols:
            break

        cx = col * cell + cell // 2
        cy = row * cell + cell // 2
        radius = 16
        color = "blue" if result == "P" else "red"

        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=color,
            width=3
        )
        last = result

    for c, r in tie_marks:
        if c >= cols:
            continue
        x0 = c * cell + 10
        y0 = r * cell + cell - 10
        x1 = c * cell + cell - 10
        y1 = r * cell + 10
        draw.line((x0, y0, x1, y1), fill="green", width=3)

    path = f"/tmp/road_{chat_id}.png"
    img.save(path)
    return path

def set_round_state(chat_id: int, round_id: int, status: str, closes_at: str | None):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO round_state(chat_id, round_id, status, closes_at) VALUES(?,?,?,?)",
            (chat_id, round_id, status, closes_at)
        )
        conn.commit()

def get_round_state(chat_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM round_state WHERE chat_id=?",
            (chat_id,)
        ).fetchone()

def ensure_pick_stats(chat_id: int, user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pick_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO pick_stats(chat_id, user_id, hit_count, total_count, streak, max_streak) VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, 0, 0, 0, 0)
            )
            conn.commit()

def save_pick(chat_id: int, round_id: int, user_id: int, choice: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO picks(chat_id, round_id, user_id, choice, created_at) VALUES(?,?,?,?,?)",
            (chat_id, round_id, user_id, choice, now_iso())
        )
        conn.commit()

def get_picks(chat_id: int, round_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM picks WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchall()

def clear_picks(chat_id: int, round_id: int):
    with db() as conn:
        conn.execute(
            "DELETE FROM picks WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        )
        conn.commit()

def update_pick_stats(chat_id: int, user_id: int, hit: bool):
    ensure_pick_stats(chat_id, user_id)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pick_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()

        hit_count = int(row["hit_count"])
        total_count = int(row["total_count"])
        streak = int(row["streak"])
        max_streak = int(row["max_streak"])

        total_count += 1
        if hit:
            hit_count += 1
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

        conn.execute(
            """
            UPDATE pick_stats
            SET hit_count=?, total_count=?, streak=?, max_streak=?
            WHERE chat_id=? AND user_id=?
            """,
            (hit_count, total_count, streak, max_streak, chat_id, user_id)
        )
        conn.commit()

def get_pick_stat(chat_id: int, user_id: int):
    ensure_pick_stats(chat_id, user_id)
    with db() as conn:
        return conn.execute(
            "SELECT * FROM pick_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()

def get_pick_rank(chat_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT ps.user_id, ps.hit_count, ps.total_count, ps.streak, ps.max_streak, u.username
            FROM pick_stats ps
            LEFT JOIN users u ON u.user_id = ps.user_id
            WHERE ps.chat_id=?
            ORDER BY ps.hit_count DESC, ps.max_streak DESC, ps.total_count DESC
            LIMIT 10
            """,
            (chat_id,)
        ).fetchall()

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user
    ensure_user(u.id, u.username)

    day = utc_day()
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_claims WHERE chat_id=? AND user_id=? AND day_utc=?",
            (chat.id, u.id, day)
        ).fetchone()
        if row:
            await update.effective_message.reply_text("✅ 오늘은 이미 받았어!")
            return

        conn.execute(
            "INSERT INTO daily_claims(chat_id, user_id, day_utc) VALUES(?,?,?)",
            (chat.id, u.id, day)
        )
        conn.commit()

    add_points(u.id, DAILY_REWARD)
    await update.effective_message.reply_text(f"🎁 출석 보상 +{DAILY_REWARD}")

async def cmd_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user
    ensure_user(u.id, u.username)

    day = utc_day()
    with db() as conn:
        row = conn.execute(
            "SELECT used FROM spin_claims WHERE chat_id=? AND user_id=? AND day_utc=?",
            (chat.id, u.id, day)
        ).fetchone()

        used = int(row["used"]) if row else 0
        if used >= SPIN_DAILY_LIMIT:
            await update.effective_message.reply_text("⛔ 오늘 룰렛은 다 썼어!")
            return

        rewards = [r for r, w in SPIN_TABLE]
        weights = [w for r, w in SPIN_TABLE]
        prize = random.choices(rewards, weights=weights, k=1)[0]

        if row:
            conn.execute(
                "UPDATE spin_claims SET used=? WHERE chat_id=? AND user_id=? AND day_utc=?",
                (used + 1, chat.id, u.id, day)
            )
        else:
            conn.execute(
                "INSERT INTO spin_claims(chat_id, user_id, day_utc, used) VALUES(?,?,?,1)",
                (chat.id, u.id, day)
            )
        conn.commit()

    add_points(u.id, prize)
    await update.effective_message.reply_text(f"🎰 룰렛 보상 +{prize}")

async def on_message_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    day = utc_day()
    with db() as conn:
        row = conn.execute(
            "SELECT msg_count, rewarded_steps FROM activity WHERE chat_id=? AND user_id=? AND day_utc=?",
            (chat.id, u.id, day)
        ).fetchone()

        if row:
            msg_count = int(row["msg_count"]) + 1
            rewarded_steps = int(row["rewarded_steps"])
            conn.execute(
                "UPDATE activity SET msg_count=? WHERE chat_id=? AND user_id=? AND day_utc=?",
                (msg_count, chat.id, u.id, day)
            )
        else:
            msg_count = 1
            rewarded_steps = 0
            conn.execute(
                "INSERT INTO activity(chat_id, user_id, day_utc, msg_count, rewarded_steps) VALUES(?,?,?,?,?)",
                (chat.id, u.id, day, 1, 0)
            )

        steps = min(msg_count // ACTIVITY_STEP, ACTIVITY_MAX_STEPS_PER_DAY)
        if steps > rewarded_steps:
            gain = (steps - rewarded_steps) * ACTIVITY_REWARD
            add_points(u.id, gain)
            conn.execute(
                "UPDATE activity SET rewarded_steps=? WHERE chat_id=? AND user_id=? AND day_utc=?",
                (steps, chat.id, u.id, day)
            )
        conn.commit()

HELP_TEXT = (
    "🃏 Baccarat Practice Simulator\n"
    "— Simulator —\n"
    "• /deal\n"
    "• /auto\n"
    "• /pick P|B|T\n"
    "• /road\n"
    "• /shoe\n"
    "• /reset_shoe\n"
    "• /reset_road\n\n"
    "— Points —\n"
    "• /daily\n"
    "• /spin\n"
    "• /me\n"
    "• /rank\n\n"
    "— Pick Stats —\n"
    "• /mystats\n"
    "• /pickrank\n\n"
    "— Admin —\n"
    "• /giveid USER_ID AMOUNT"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘 👥")
        return
    await update.effective_message.reply_text(HELP_TEXT)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username)
    await update.effective_message.reply_text(
        f"🙋 @{u.username or u.id}\n💰 포인트: {get_points(u.id)}"
    )

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text("랭킹 데이터 없음")
        return

    lines = ["🏆 포인트 TOP10"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or "익명"
        lines.append(f"{i}. {name} - {r['points']}")
    await update.effective_message.reply_text("\n".join(lines))

async def cmd_giveid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id not in ADMIN_IDS:
        await update.effective_message.reply_text("관리자 전용")
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text("사용법: /giveid USER_ID AMOUNT")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("숫자 형식 확인")
        return

    if amount <= 0:
        await update.effective_message.reply_text("금액은 1 이상")
        return

    ensure_user(target_id, str(target_id))
    add_points(target_id, amount)
    await update.effective_message.reply_text(f"✅ {target_id} 에게 +{amount}")

async def cmd_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘 👥")
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text("사용법: /pick P|B|T")
        return

    choice = context.args[0].upper()
    if choice not in ("P", "B", "T"):
        await update.effective_message.reply_text("P / B / T 중 하나만 가능")
        return

    ensure_user(u.id, u.username)

    state = get_round_state(chat.id)
    if not state or state["status"] != "OPEN":
        await update.effective_message.reply_text("예측 가능한 자동 라운드가 없어. /auto 먼저!")
        return

    rid = int(state["round_id"])
    save_pick(chat.id, rid, u.id, choice)
    await update.effective_message.reply_text(f"📝 예측 저장: {RESULT_TEXT[choice]} ({choice})")

async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    u = update.effective_user
    ensure_pick_stats(chat.id, u.id)

    stat = get_pick_stat(chat.id, u.id)
    total = int(stat["total_count"])
    hits = int(stat["hit_count"])
    streak = int(stat["streak"])
    max_streak = int(stat["max_streak"])
    acc = 0.0 if total == 0 else (hits / total) * 100

    await update.effective_message.reply_text(
        f"📊 예측 통계\n"
        f"적중: {hits}\n"
        f"총 예측: {total}\n"
        f"적중률: {acc:.2f}%\n"
        f"연속 적중: {streak}\n"
        f"최고 연속 적중: {max_streak}"
    )

async def cmd_pickrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    rows = get_pick_rank(chat.id)

    if not rows:
        await update.effective_message.reply_text("예측 랭킹 데이터 없음")
        return

    lines = ["🎯 예측 TOP10"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or str(r["user_id"])
        total = int(r["total_count"])
        hits = int(r["hit_count"])
        acc = 0.0 if total == 0 else (hits / total) * 100
        lines.append(f"{i}. {name} - 적중 {hits}/{total} ({acc:.1f}%) | 🔥{r['max_streak']}")

    await update.effective_message.reply_text("\n".join(lines))

async def do_deal(application: Application, chat_id: int):
    rid = next_round_id(chat_id)
    player, banker, p, b = play_baccarat(chat_id)
    result = result_from_totals(p, b)

    save_road(chat_id, rid, result)

    pick_lines = []
    picks = get_picks(chat_id, rid)
    for pick in picks:
        hit = pick["choice"] == result
        update_pick_stats(chat_id, int(pick["user_id"]), hit)
        label = "✅ 적중" if hit else "❌ 실패"
        pick_lines.append(f"{pick['user_id']} - {pick['choice']} → {label}")

    clear_picks(chat_id, rid)

    lines = [
        f"🎲 Hand #{rid}",
        f"🔵 PLAYER: {fmt_hand(player)}  ({p})",
        f"🔴 BANKER: {fmt_hand(banker)}  ({b})",
        f"➡️ RESULT: {RESULT_TEXT[result]} ({result})",
        f"🧱 Shoe remaining: {shoe_remaining(chat_id)}",
    ]

    if pick_lines:
        lines.append("")
        lines.append("🎯 예측 결과")
        lines.extend(pick_lines)

    await application.bot.send_message(chat_id=chat_id, text="\n".join(lines))

    try:
        path = draw_road_image(chat_id)
        await application.bot.send_photo(chat_id=chat_id, photo=open(path, "rb"))
    except Exception:
        pass

async def cmd_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘 👥")
        return
    await do_deal(context.application, update.effective_chat.id)

async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘 👥")
        return

    chat_id = update.effective_chat.id
    state = get_round_state(chat_id)
    if state and state["status"] == "OPEN":
        await update.effective_message.reply_text("⏳ 이미 자동 실행 예약돼 있어!")
        return

    rid = next_round_id(chat_id)
    closes_ts = datetime.now(timezone.utc).timestamp() + ROUND_SECONDS
    closes_iso = datetime.fromtimestamp(closes_ts, tz=timezone.utc).isoformat()
    set_round_state(chat_id, rid, "OPEN", closes_iso)

    await update.effective_message.reply_text(
        f"⏱ Hand #{rid}\n{ROUND_SECONDS}초 후 자동 실행\n예측하려면: /pick P|B|T"
    )

    async def _task():
        await asyncio.sleep(ROUND_SECONDS)
        cur = get_round_state(chat_id)
        if cur and cur["status"] == "OPEN" and int(cur["round_id"]) == rid:
            set_round_state(chat_id, rid, "IDLE", None)
            await do_deal(context.application, chat_id)

    context.application.create_task(_task())

async def cmd_road(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    results = build_road(chat_id)
    if not results:
        await update.effective_message.reply_text("로드 기록 없음. /deal 먼저!")
        return

    path = draw_road_image(chat_id)
    await update.effective_message.reply_photo(photo=open(path, "rb"))

async def cmd_shoe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remain = shoe_remaining(chat_id)
    await update.effective_message.reply_text(f"🧱 Shoe remaining: {remain}")

async def cmd_reset_shoe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    deck = create_shoe()
    set_shoe(chat_id, deck, 0)
    await update.effective_message.reply_text("✅ Shoe reset 완료")

async def cmd_reset_road(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute("DELETE FROM road_history WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM round_state WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM picks WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM pick_stats WHERE chat_id=?", (chat_id,))
        conn.commit()
    await update.effective_message.reply_text("✅ Road / Pick 기록 초기화 완료")

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 비어있음")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("deal", cmd_deal))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("pick", cmd_pick))
    app.add_handler(CommandHandler("road", cmd_road))
    app.add_handler(CommandHandler("shoe", cmd_shoe))
    app.add_handler(CommandHandler("reset_shoe", cmd_reset_shoe))
    app.add_handler(CommandHandler("reset_road", cmd_reset_road))

    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("pickrank", cmd_pickrank))
    app.add_handler(CommandHandler("giveid", cmd_giveid))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_activity))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
