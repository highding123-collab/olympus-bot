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
)

# =========================
# ENV
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # Railway Variables에 이 이름으로 넣어줘
DB_PATH = os.getenv("DB_PATH", "points.db")

# =========================
# GAME CONFIG (바카라 스타일)
# =========================
STARTING_POINTS = 100000

# 배팅 선택지: P(플레이어), B(뱅커), T(타이)
BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}

# 대충 실제 바카라 확률 비슷하게 (대략값)
RESULT_WEIGHTS = {"P": 44.62, "B": 45.86, "T": 9.52}

# 배당(원금 포함)
# P: 2.0x, B: 1.95x(커미션 5%), T: 8.0x
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

# 연승 보너스 배당(요청 기능)
# 예: 2연승부터 0.02씩 추가 (최대 0.20)
STREAK_BONUS_START = 2
STREAK_BONUS_STEP = 0.02
STREAK_BONUS_MAX = 0.20

# 라운드 자동 결과 시간(초) — 1분
ROUND_SECONDS = 60

# =========================
# ADMIN
# =========================
# 관리자 텔레그램 ID 넣으면 /give 가능
# 예: ADMIN_IDS = {123456789, 987654321}
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
            profit INTEGER NOT NULL DEFAULT 0,   -- 하우스 누적 수익
            rounds INTEGER NOT NULL DEFAULT 0,   -- 진행 라운드 수
            updated_at TEXT NOT NULL
        )
        """)
        conn.commit()

def ensure_user(user_id: int, username: str | None):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, points, win_streak, updated_at) VALUES(?,?,?,?,?)",
                (user_id, username or "", STARTING_POINTS, 0, now_iso()),
            )
        else:
            # username 업데이트만
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
    """라운드를 OPEN 상태로 만들고 round_id 증가"""
    with db() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            rid = 1
        else:
            rid = int(row["round_id"]) + 1

        created = now_iso()
        closes = datetime.now(timezone.utc).timestamp() + ROUND_SECONDS
        closes_iso = datetime.fromtimestamp(closes, tz=timezone.utc).isoformat()

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

def delete_bet(chat_id: int, round_id: int, user_id: int):
    with db() as conn:
        conn.execute("DELETE FROM bets WHERE chat_id=? AND round_id=? AND user_id=?",
                     (chat_id, round_id, user_id))
        conn.commit()

# =========================
# GAME LOGIC
# =========================
def weighted_result() -> str:
    keys = list(RESULT_WEIGHTS.keys())
    weights = list(RESULT_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]

def streak_bonus_multiplier(streak: int) -> float:
    """연승 보너스 배당 추가 (2연승부터)"""
    if streak < STREAK_BONUS_START:
        return 0.0
    bonus = (streak - STREAK_BONUS_START + 1) * STREAK_BONUS_STEP
    return min(bonus, STREAK_BONUS_MAX)

def fmt_points(n: int) -> str:
    return f"{n:,}"

# =========================
# ASYNC ROUND TIMER (job_queue 안씀)
# =========================
_round_tasks: dict[tuple[int, int], asyncio.Task] = {}

async def settle_round(application: Application, chat_id: int, round_id: int):
    """라운드 결과 확정 + 정산 + 메시지"""
    # 이미 CLOSED면 중복 실행 방지
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
    lines.append(f"🎲 **라운드 #{round_id} 결과:** {BET_CHOICES[result]}({result})")
    lines.append(f"⏱️ 배팅 마감. 정산 중...\n")

    # 정산
    for b in bets:
        user_id = int(b["user_id"])
        choice = b["choice"]
        amount = int(b["amount"])

        # 기본: 배팅은 이미 차감되어 있어야 함
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
            # 패배
            set_user_streak(user_id, 0)
            lines.append(f"❌ {user_id}: -{fmt_points(amount)}p")

    # 하우스 수익 = 총배팅 - 총지급
    house_delta = total_bet - total_payout
    add_house_profit(chat_id, house_delta)

    h = get_house(chat_id)
    lines.append("\n🏦 **하우스 통계**")
    lines.append(f"- 이번 라운드 수익: {fmt_points(house_delta)}p")
    lines.append(f"- 누적 수익: {fmt_points(int(h['profit']))}p")
    lines.append(f"- 누적 라운드: {int(h['rounds'])}")

    # 다음 라운드 안내
    lines.append("\n➡️ 다음 라운드 베팅: `/bet 금액 P|B|T`  또는  `/allin P|B|T`")

    await application.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )

async def close_round_after_delay(application: Application, chat_id: int, round_id: int):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(application, chat_id, round_id)

async def ensure_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """라운드가 없거나 CLOSED면 새 라운드 열고 60초 뒤 자동정산 예약"""
    chat_id = update.effective_chat.id
    ensure_house(chat_id)

    r = get_round(chat_id)
    if r is None or r["status"] != "OPEN":
        rid = open_new_round(chat_id)

        # 타이머 task 등록
        key = (chat_id, rid)
        t = context.application.create_task(close_round_after_delay(context.application, chat_id, rid))
        _round_tasks[key] = t

        await update.effective_message.reply_text(
            f"🆕 **라운드 #{rid} 시작!** (⏱️ {ROUND_SECONDS}초 후 자동 결과)\n"
            f"베팅: `/bet 금액 P|B|T`  |  올인: `/allin P|B|T`",
            parse_mode="Markdown"
        )
        return rid

    return int(r["round_id"])

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 그룹에서만 사용하도록
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("그룹에서 사용해줘! 👥")
        return

    u = update.effective_user
    ensure_user(u.id, u.username)

    await ensure_round(update, context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 **명령어 모음**\n"
        "• `/start` 라운드 시작(없으면 생성)\n"
        "• `/bet 금액 P|B|T` 배팅 (예: /bet 1000 P)\n"
        "• `/allin P|B|T` 💎 올인\n"
        "• `/me` 내 포인트/연승\n"
        "• `/round` 현재 라운드 상태\n"
        "• `/rank` TOP10 랭킹\n"
        "• `/house` 🏦 하우스 수익/라운드 통계\n"
        "\n"
        "👑 **관리자 전용**\n"
        "• `/give @username 10000` 포인트 지급\n"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username)
    p = get_user_points(u.id)
    s = get_user_streak(u.id)
    await update.effective_message.reply_text(
        f"🙋 @{u.username or u.id}\n"
        f"• 포인트: {fmt_points(p)}p\n"
        f"• 🔥 연승: {s}",
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
        f"(1분 자동 결과 시스템)",
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
        await update.effective_message.reply_text("지금 라운드는 마감됨. 곧 새 라운드 열어줘!")
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

    # 기존 베팅 있으면 되돌리고 다시 차감
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

    args = context.args
    if len(args) != 1:
        await update.effective_message.reply_text("사용법: /allin P|B|T")
        return

    choice = args[0].upper()
    if choice not in BET_CHOICES:
        await update.effective_message.reply_text("선택은 P/B/T 중 하나!")
        return

    cur = get_user_points(u.id)
    if cur <= 0:
        await update.effective_message.reply_text("올인할 포인트가 없음…")
        return

    # 기존 베팅 있으면 제거(환급) 후 올인
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
        f"💎 **올인!** {fmt_points(amount)}p → {BET_CHOICES[choice]}({choice})",
        parse_mode="Markdown"
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
        await update.effective_message.reply_text("그 유저는 아직 DB에 없어. 한 번이라도 봇을 써야 돼(/start).")
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

    return app

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 비어있음")

    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
