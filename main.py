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

# =========================
# ENV
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "points.db")

# =========================
# GAME CONFIG (바카라 스타일)
# =========================
STARTING_POINTS = 200000

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
RESULT_WEIGHTS = {"P": 44.62, "B": 45.86, "T": 9.52}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

# 연승 보너스 배당
STREAK_BONUS_START = 2
STREAK_BONUS_STEP = 0.02
STREAK_BONUS_MAX = 0.20

# 라운드 자동 결과 시간(초)
ROUND_SECONDS = 60

# =========================
# 포인트 벌기 기능 설정
# =========================
DAILY_REWARD = 10000

SPIN_DAILY_LIMIT = 3
# (보상, 가중치) — 필요하면 숫자 바꿔도 됨
SPIN_TABLE = [
    (0, 10),
    (500, 25),
    (1000, 30),
    (3000, 18),
    (10000, 12),
    (50000, 4),
    (100000, 1),
]

# 채팅 적립: 메시지 N개마다 보상
ACTIVITY_STEP = 10
ACTIVITY_REWARD = 500
ACTIVITY_MAX_STEPS_PER_DAY = 20  # 하루 최대 200메시지(=20스텝)까지 보상

# =========================
# ADMIN
# =========================
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):
    try:
        ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip()}
    except:
        ADMIN_IDS = set()

# =========================
# DB
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL,
            win_streak INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            status TEXT NOT NULL,          -- OPEN / CLOSED
            created_at TEXT NOT NULL,
            closes_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,          -- P/B/T
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, round_id, user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS house (
            chat_id INTEGER PRIMARY KEY,
            profit INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """)

        # ✅ /daily (출석)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_claims (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id, day_utc)
        )
        """)

        # ✅ /spin (무료뽑기)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spin_claims (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, day_utc)
        )
        """)

        # ✅ 채팅 적립
        conn.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            rewarded_steps INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, day_utc)
        )
        """)

        conn.commit()

def fmt_points(n: int) -> str:
    return f"{n:,}"

def ensure_user(user_id: int, username: str | None):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, points, win_streak, updated_at) VALUES(?,?,?,?,?)",
                (user_id, username or "", STARTING_POINTS, 0, now_iso()),
            )
        else:
            conn.execute(
                "UPDATE users SET username=?, updated_at=? WHERE user_id=?",
                (username or (row["username"] or ""), now_iso(), user_id),
            )
        conn.commit()

def get_user_points(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["points"]) if row else 0

def set_user_points(user_id: int, points: int):
    with db() as conn:
        conn.execute("UPDATE users SET points=?, updated_at=? WHERE user_id=?",
                     (points, now_iso(), user_id))
        conn.commit()

def get_user_streak(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT win_streak FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["win_streak"]) if row else 0

def set_user_streak(user_id: int, streak: int):
    with db() as conn:
        conn.execute("UPDATE users SET win_streak=?, updated_at=? WHERE user_id=?",
                     (streak, now_iso(), user_id))
        conn.commit()

def ensure_house(chat_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM house WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO house(chat_id, profit, rounds, updated_at) VALUES(?,?,?,?)",
                (chat_id, 0, 0, now_iso())
            )
        conn.commit()

def add_house_profit(chat_id: int, delta: int):
    ensure_house(chat_id)
    with db() as conn:
        conn.execute(
            "UPDATE house SET profit = profit + ?, updated_at=? WHERE chat_id=?",
            (delta, now_iso(), chat_id)
        )
        conn.commit()

def inc_house_rounds(chat_id: int, delta: int = 1):
    ensure_house(chat_id)
    with db() as conn:
        conn.execute(
            "UPDATE house SET rounds = rounds + ?, updated_at=? WHERE chat_id=?",
            (delta, now_iso(), chat_id)
        )
        conn.commit()

def get_house(chat_id: int):
    ensure_house(chat_id)
    with db() as conn:
        return conn.execute("SELECT * FROM house WHERE chat_id=?", (chat_id,)).fetchone()

# =========================
# ROUND
# =========================
def get_round(chat_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM rounds WHERE chat_id=?", (chat_id,)).fetchone()

def open_new_round(chat_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE chat_id=?", (chat_id,)).fetchone()
        rid = 1 if row is None else int(row["round_id"]) + 1

        created = now_iso()
        closes_ts = datetime.now(timezone.utc).timestamp() + ROUND_SECONDS
        closes_iso = datetime.fromtimestamp(closes_ts, tz=timezone.utc).isoformat()

        conn.execute(
            "INSERT OR REPLACE INTO rounds(chat_id, round_id, status, created_at, closes_at) VALUES(?,?,?,?,?)",
            (chat_id, rid, "OPEN", created, closes_iso)
        )
        conn.commit()
        return rid

def close_round(chat_id: int):
    with db() as conn:
        conn.execute("UPDATE rounds SET status='CLOSED' WHERE chat_id=?", (chat_id,))
        conn.commit()

def is_round_open(chat_id: int) -> bool:
    row = get_round(chat_id)
    return bool(row and row["status"] == "OPEN")

def get_bets(chat_id: int, round_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bets WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchall()

def upsert_bet(chat_id: int, round_id: int, user_id: int, choice: str, amount: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bets(chat_id, round_id, user_id, choice, amount, created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, round_id, user_id, choice, amount, now_iso())
        )
        conn.commit()

# =========================
# GAME LOGIC
# =========================
def weighted_result() -> str:
    keys = list(RESULT_WEIGHTS.keys())
    weights = list(RESULT_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]

def streak_bonus_multiplier(streak: int) -> float:
    if streak < STREAK_BONUS_START:
        return 0.0
    bonus = (streak - STREAK_BONUS_START + 1) * STREAK_BONUS_STEP
    return min(bonus, STREAK_BONUS_MAX)

# =========================
# ASYNC ROUND TIMER (job_queue 안씀)
# =========================
_round_tasks: dict[tuple[int, int], asyncio.Task] = {}

async def settle_round(application: Application, chat_id: int, round_id: int):
    r = get_round(chat_id)
    if not r or int(r["round_id"]) != round_id or r["status"] != "OPEN":
        return

    close_round(chat_id)
    inc_house_rounds(chat_id, 1)

    result = weighted_result()
    bets = get_bets(chat_id, round_id)

    total_bet = sum(int(b["amount"]) for b in bets)
    total_payout = 0

    lines = []
    lines.append(f"🎲 라운드 #{round_id} 결과: {BET_CHOICES[result]}({result})")
    lines.append("정산 결과:")

    for b in bets:
        user_id = int(b["user_id"])
        choice = b["choice"]
        amount = int(b["amount"])

        if choice == result:
            streak = get_user_streak(user_id) + 1
            set_user_streak(user_id, streak)

            base = PAYOUTS[result]
            bonus = streak_bonus_multiplier(streak)
            mult = base + bonus

            payout = int(round(amount * mult))
            total_payout += payout

            cur = get_user_points(user_id)
            set_user_points(user_id, cur + payout)

            lines.append(f"✅ {user_id}: +{fmt_points(payout)}p (배당 {mult:.2f}x / 🔥연승 {streak})")
        else:
            set_user_streak(user_id, 0)
            lines.append(f"❌ {user_id}: -{fmt_points(amount)}p")

    house_delta = total_bet - total_payout
    add_house_profit(chat_id, house_delta)

    h = get_house(chat_id)
    lines.append("")
    lines.append("🏦 하우스 통계")
    lines.append(f"- 이번 라운드 수익: {fmt_points(house_delta)}p")
    lines.append(f"- 누적 수익: {fmt_points(int(h['profit']))}p")
    lines.append(f"- 누적 라운드: {int(h['rounds'])}")
    lines.append("")
    lines.append("➡️ 다음 라운드 베팅: /bet 금액 P|B|T  또는  /allin P|B|T")
    lines.append("🎁 포인트 벌기: /daily  /spin  (채팅 적립 자동)")

    await application.bot.send_message(chat_id=chat_id, text="\n".join(lines))

async def close_round_after_delay(application: Application, chat_id: int, round_id: int):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(application, chat_id, round_id)

async def ensure_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    ensure_house(chat_id)

    r = get_round(chat_id)
    if r is None or r["status"] != "OPEN":
        rid = open_new_round(chat_id)

        key = (chat_id, rid)
        t = context.application.create_task(close_round_after_delay(context.application, chat_id, rid))
        _round_tasks[key] = t

        await update.effective_message.reply_text(
            f"🆕 라운드 #{rid} 시작! (⏱ {ROUND_SECONDS}초 후 자동 결과)\n"
            f"베팅: /bet 금액 P|B|T  |  올인: /allin P|B|T\n"
            f"포인트 벌기: /daily /spin (채팅 적립은 자동)"
        )
        return rid

    return int(r["round_id"])

# =========================
# 포인트 벌기 기능
# =========================
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서만 가능!")
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    day = utc_day()
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM daily_claims WHERE chat_id=? AND user_id=? AND day_utc=?",
            (chat.id, u.id, day)
        ).fetchone()
        if exists:
            await update.effective_message.reply_text("✅ 오늘은 이미 출석 보상 받았어!")
            return

        conn.execute(
            "INSERT INTO daily_claims(chat_id, user_id, day_utc) VALUES(?,?,?)",
            (chat.id, u.id, day)
        )
        conn.commit()

    cur = get_user_points(u.id)
    set_user_points(u.id, cur + DAILY_REWARD)
    await update.effective_message.reply_text(f"🎁 출석 보상 +{fmt_points(DAILY_REWARD)}p (현재 {fmt_points(get_user_points(u.id))}p)")

async def cmd_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서만 가능!")
        return

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
            await update.effective_message.reply_text(f"⛔ 오늘 무료뽑기 {SPIN_DAILY_LIMIT}회 다 썼어!")
            return

        # 확률 뽑기
        rewards = [r for r, w in SPIN_TABLE]
        weights = [w for r, w in SPIN_TABLE]
        prize = random.choices(rewards, weights=weights, k=1)[0]

        if row:
            conn.execute(
                "UPDATE spin_claims SET used = used + 1 WHERE chat_id=? AND user_id=? AND day_utc=?",
                (chat.id, u.id, day)
            )
        else:
            conn.execute(
                "INSERT INTO spin_claims(chat_id, user_id, day_utc, used) VALUES(?,?,?,1)",
                (chat.id, u.id, day)
            )
        conn.commit()

    cur = get_user_points(u.id)
    set_user_points(u.id, cur + prize)
    remain = SPIN_DAILY_LIMIT - (used + 1)
    await update.effective_message.reply_text(
        f"🎰 무료뽑기 결과: +{fmt_points(prize)}p\n"
        f"남은 횟수: {remain}회 | 현재 {fmt_points(get_user_points(u.id))}p"
    )

async def on_message_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not update.message:
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
                (chat.id, u.id, day, msg_count, rewarded_steps)
            )

        steps = msg_count // ACTIVITY_STEP
        steps = min(steps, ACTIVITY_MAX_STEPS_PER_DAY)

        if steps > rewarded_steps:
            gain_steps = steps - rewarded_steps
            reward = gain_steps * ACTIVITY_REWARD

            # 포인트 지급
            cur = get_user_points(u.id)
            set_user_points(u.id, cur + reward)

            conn.execute(
                "UPDATE activity SET rewarded_steps=? WHERE chat_id=? AND user_id=? AND day_utc=?",
                (steps, chat.id, u.id, day)
            )

        conn.commit()

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘! 👥")
        return

    u = update.effective_user
    ensure_user(u.id, u.username)
    await ensure_round(update, context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 명령어 모음\n"
        "— 게임 —\n"
        "• /start  라운드 시작(없으면 생성)\n"
        "• /bet 금액 P|B|T  (예: /bet 1000 P)\n"
        "• /allin P|B|T  💎 올인\n"
        "• /me  내 포인트/연승\n"
        "• /round  현재 라운드\n"
        "• /rank  TOP10\n"
        "• /house  하우스 통계\n"
        "\n"
        "— 포인트 벌기 —\n"
        f"• /daily  하루 1회 +{DAILY_REWARD}p\n"
        f"• /spin  하루 {SPIN_DAILY_LIMIT}회 무료뽑기\n"
        f"• (자동) 채팅 {ACTIVITY_STEP}개마다 +{ACTIVITY_REWARD}p (하루 최대 {ACTIVITY_MAX_STEPS_PER_DAY*ACTIVITY_REWARD}p)\n"
        "\n"
        "👑 관리자\n"
        "• /give @username 10000  (ADMIN_IDS 설정 필요)"
    )
    await update.effective_message.reply_text(text)

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username)
    p = get_user_points(u.id)
    s = get_user_streak(u.id)
    await update.effective_message.reply_text(
        f"🙋 @{u.username or u.id}\n"
        f"• 포인트: {fmt_points(p)}p\n"
        f"• 🔥 연승: {s}"
    )

async def cmd_round(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    r = get_round(chat_id)
    if not r:
        await update.effective_message.reply_text("라운드 없음. /start 로 시작!")
        return
    await update.effective_message.reply_text(
        f"📊 현재 라운드 #{int(r['round_id'])}\n"
        f"• 상태: {r['status']}\n"
        f"• 마감(UTC): {r['closes_at']}\n"
        f"• 자동 결과: {ROUND_SECONDS}초"
    )

async def cmd_house(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    h = get_house(chat_id)
    await update.effective_message.reply_text(
        "🏦 하우스 통계\n"
        f"• 누적 수익: {fmt_points(int(h['profit']))}p\n"
        f"• 누적 라운드: {int(h['rounds'])}"
    )

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, points, win_streak FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

    lines = ["🏆 랭킹 TOP10"]
    for i, r in enumerate(rows, start=1):
        uname = r["username"] or str(r["user_id"])
        lines.append(f"{i}. {uname} — {fmt_points(int(r['points']))}p (🔥{int(r['win_streak'])})")

    await update.effective_message.reply_text("\n".join(lines))

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서만 가능!")
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    rid = await ensure_round(update, context)
    if not is_round_open(chat.id):
        await update.effective_message.reply_text("지금 라운드는 마감됨. /start 로 새 라운드 열어줘!")
        return

    args = context.args
    if len(args) != 2:
        await update.effective_message.reply_text("사용법: /bet 금액 P|B|T  (예: /bet 1000 P)")
        return

    try:
        amount = int(args[0])
    except:
        await update.effective_message.reply_text("금액은 숫자!")
        return

    choice = args[1].upper()
    if choice not in BET_CHOICES:
        await update.effective_message.reply_text("선택은 P/B/T 중 하나!")
        return
    if amount <= 0:
        await update.effective_message.reply_text("금액은 1 이상!")
        return

    cur = get_user_points(u.id)
    if amount > cur:
        await update.effective_message.reply_text(f"잔액 부족! 현재 {fmt_points(cur)}p")
        return

    # 기존 베팅 있으면 환급 후 재차감
    with db() as conn:
        prev = conn.execute(
            "SELECT amount FROM bets WHERE chat_id=? AND round_id=? AND user_id=?",
            (chat.id, rid, u.id)
        ).fetchone()

    if prev:
        prev_amt = int(prev["amount"])
        set_user_points(u.id, cur + prev_amt)
        cur = cur + prev_amt

    set_user_points(u.id, cur - amount)
    upsert_bet(chat.id, rid, u.id, choice, amount)

    await update.effective_message.reply_text(
        f"🎯 베팅 완료: {fmt_points(amount)}p → {BET_CHOICES[choice]}({choice})\n"
        f"남은 포인트: {fmt_points(get_user_points(u.id))}p"
    )

async def cmd_allin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서만 가능!")
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    rid = await ensure_round(update, context)

    if len(context.args) != 1:
        await update.effective_message.reply_text("사용법: /allin P|B|T")
        return

    choice = context.args[0].upper()
    if choice not in BET_CHOICES:
        await update.effective_message.reply_text("선택은 P/B/T 중 하나!")
        return

    cur = get_user_points(u.id)
    if cur <= 0:
        await update.effective_message.reply_text("올인할 포인트가 없음…")
        return

    # 기존 베팅 있으면 환급 후 올인
    with db() as conn:
        prev = conn.execute(
            "SELECT amount FROM bets WHERE chat_id=? AND round_id=? AND user_id=?",
            (chat.id, rid, u.id)
        ).fetchone()

    if prev:
        prev_amt = int(prev["amount"])
        set_user_points(u.id, cur + prev_amt)
        cur = cur + prev_amt

    amount = cur
    set_user_points(u.id, 0)
    upsert_bet(chat.id, rid, u.id, choice, amount)

    await update.effective_message.reply_text(
        f"💎 올인! {fmt_points(amount)}p → {BET_CHOICES[choice]}({choice})"
    )

async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id not in ADMIN_IDS:
        await update.effective_message.reply_text("👑 관리자 전용이야.")
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text("사용법: /give @username 10000")
        return

    target = context.args[0].lstrip("@")
    try:
        amount = int(context.args[1])
    except:
        await update.effective_message.reply_text("금액은 숫자!")
        return
    if amount <= 0:
        await update.effective_message.reply_text("0보다 큰 값!")
        return

    with db() as conn:
        row = conn.execute("SELECT user_id, points FROM users WHERE username=?", (target,)).fetchone()

    if not row:
        await update.effective_message.reply_text("그 유저는 아직 DB에 없어. 한 번이라도 /start 해야 돼.")
        return

    uid = int(row["user_id"])
    cur = int(row["points"])
    set_user_points(uid, cur + amount)

    await update.effective_message.reply_text(
        f"✅ 지급 완료: @{target} +{fmt_points(amount)}p (총 {fmt_points(get_user_points(uid))}p)"
    )

# =========================
# MAIN
# =========================
def build_app() -> Application:
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("round", cmd_round))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("allin", cmd_allin))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("house", cmd_house))
    app.add_handler(CommandHandler("give", cmd_give))

    # ✅ 채팅 적립 (명령어 아닌 일반 메시지)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_activity))

    # ✅ 포인트 벌기
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("spin", cmd_spin))

    return app

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 비어있음")

    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
