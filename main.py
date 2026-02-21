import os
import sqlite3
import random
import time
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --------------------
# ENV / CONFIG
# --------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in env vars")

DB = "points.db"

# 운영 설정
ROUND_SECONDS = 60
DAILY_CHECKIN_REWARD = 200
MISSION_REWARD_RANGE = (100, 300)  # 미션 완료 보상 범위
DICE_REWARD_RANGE = (50, 250)      # 주사위 보상 범위 (베팅 없음)
ROULETTE_REWARD_RANGE = (0, 400)   # 룰렛 보상 범위 (베팅 없음)
QUIZ_REWARD = 250

# 부스트(= 올인 대체 기능): 60초 동안 보상 2배
BOOST_SECONDS = 60
BOOST_MULTIPLIER = 2

# 관리자
def parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return set()
    out = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out

ADMIN_IDS = parse_admin_ids()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --------------------
# DB helpers
# --------------------
def db():
    return sqlite3.connect(DB)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS points (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT,
            actor_id INTEGER,
            created_at TEXT NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            PRIMARY KEY (chat_id, round_id)
        )
        """)

        # 라운드별 누적 획득(리더보드용)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS round_earnings (
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            earned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, round_id, user_id)
        )
        """)

        # 출석 기록 (UTC 기준 날짜)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id, day_utc)
        )
        """)

        # 유저별 연속 참여(연승 대체 = 연속 이벤트 참여 보너스)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS streaks (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0,
            last_day_utc TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """)

        # 유저별 부스트 상태
        conn.execute("""
        CREATE TABLE IF NOT EXISTS boosts (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """)

        # 퀴즈 상태(라운드별 1문제)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS quiz_state (
            chat_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            qid INTEGER NOT NULL,
            question TEXT NOT NULL,
            a TEXT NOT NULL,
            b TEXT NOT NULL,
            c TEXT NOT NULL,
            answer TEXT NOT NULL,
            PRIMARY KEY (chat_id, round_id)
        )
        """)

        conn.commit()

def get_points(chat_id: int, user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT points FROM points WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        return row[0] if row else 0

def add_points(chat_id: int, user_id: int, delta: int, reason: str, actor_id: int | None):
    with db() as conn:
        conn.execute("""
        INSERT INTO points(chat_id, user_id, points)
        VALUES(?,?,?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET points = points.points + excluded.points
        """, (chat_id, user_id, delta))

        conn.execute("""
        INSERT INTO ledger(chat_id, user_id, delta, reason, actor_id, created_at)
        VALUES(?,?,?,?,?,?)
        """, (chat_id, user_id, delta, reason, actor_id, now_iso()))
        conn.commit()

def add_round_earning(chat_id: int, round_id: int, user_id: int, earned: int):
    with db() as conn:
        conn.execute("""
        INSERT INTO round_earnings(chat_id, round_id, user_id, earned)
        VALUES(?,?,?,?)
        ON CONFLICT(chat_id, round_id, user_id) DO UPDATE SET earned = earned + excluded.earned
        """, (chat_id, round_id, user_id, earned))
        conn.commit()


# --------------------
# Round system (60s auto close)
# --------------------
ROUND_BY_CHAT: dict[int, dict] = {}
ROUND_SEQ = 0

def next_round_id() -> int:
    global ROUND_SEQ
    ROUND_SEQ += 1
    return ROUND_SEQ

def ensure_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict:
    st = ROUND_BY_CHAT.get(chat_id)
    if st:
        return st

    rid = next_round_id()

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rounds(chat_id, round_id, started_at, status) VALUES(?,?,?,?)",
            (chat_id, rid, now_iso(), "OPEN")
        )
        conn.commit()

    job = context.job_queue.run_once(
        close_round_job,
        when=ROUND_SECONDS,
        data={"chat_id": chat_id, "round_id": rid},
        name=f"close_round:{chat_id}:{rid}",
    )

    st = {"round_id": rid, "job": job, "started_at": time.time()}
    ROUND_BY_CHAT[chat_id] = st
    return st

async def close_round_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    rid = context.job.data["round_id"]

    st = ROUND_BY_CHAT.get(chat_id)
    if not st or st["round_id"] != rid:
        return

    with db() as conn:
        conn.execute(
            "UPDATE rounds SET ended_at=?, status=? WHERE chat_id=? AND round_id=?",
            (now_iso(), "CLOSED", chat_id, rid)
        )
        top = conn.execute("""
            SELECT user_id, earned
            FROM round_earnings
            WHERE chat_id=? AND round_id=?
            ORDER BY earned DESC
            LIMIT 5
        """, (chat_id, rid)).fetchall()
        conn.commit()

    msg = [f"⏱ 라운드 #{rid} 종료!"]
    if top:
        msg.append("🏁 이번 라운드 TOP 5 (획득 포인트):")
        for i, (uid, earned) in enumerate(top, start=1):
            msg.append(f"{i}) {uid} : +{earned}")
    else:
        msg.append("이번 라운드 참여 기록이 없어.")
    msg.append("다음 라운드는 누군가 버튼/명령을 쓰면 자동 시작!")

    await context.bot.send_message(chat_id, "\n".join(msg))
    ROUND_BY_CHAT.pop(chat_id, None)


# --------------------
# Boost (올인 버튼 대체: 60초 보상 2배)
# --------------------
def is_boost_active(chat_id: int, user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM boosts WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        if not row:
            return False
        exp = datetime.fromisoformat(row[0])
        return exp > datetime.now(timezone.utc)

def set_boost(chat_id: int, user_id: int, seconds: int):
    exp = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    with db() as conn:
        conn.execute("""
        INSERT INTO boosts(chat_id, user_id, expires_at)
        VALUES(?,?,?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET expires_at=excluded.expires_at
        """, (chat_id, user_id, exp.isoformat()))
        conn.commit()

def apply_boost(chat_id: int, user_id: int, base_reward: int) -> int:
    return base_reward * BOOST_MULTIPLIER if is_boost_active(chat_id, user_id) else base_reward


# --------------------
# Streak (연승 대체: 연속 참여 보너스)
# 규칙:
# - 같은 UTC day에 첫 이벤트 참여 시 streak 갱신
# - 어제에 이어서 참여하면 streak+1, 아니면 1로 리셋
# - streak가 3/5/7이면 보너스 지급
# --------------------
def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def update_streak_and_get_bonus(chat_id: int, user_id: int) -> int:
    today = utc_day()
    with db() as conn:
        row = conn.execute(
            "SELECT streak, last_day_utc FROM streaks WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()

        if not row:
            streak = 1
            last = today
            conn.execute(
                "INSERT INTO streaks(chat_id, user_id, streak, last_day_utc) VALUES(?,?,?,?)",
                (chat_id, user_id, streak, last)
            )
        else:
            streak, last = row
            if last == today:
                # 이미 오늘 갱신됨
                conn.commit()
                return 0

            # yesterday?
            yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            if last == yday:
                streak = streak + 1
            else:
                streak = 1
            conn.execute(
                "UPDATE streaks SET streak=?, last_day_utc=? WHERE chat_id=? AND user_id=?",
                (streak, today, chat_id, user_id)
            )

        conn.commit()

    # 보너스 룰(원하면 여기 숫자 바꾸면 됨)
    if streak in (3, 5, 7):
        return 300 * (streak // 2)  # 3->300, 5->600, 7->900 느낌
    return 0


# --------------------
# UI
# --------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 출석", callback_data="checkin"),
         InlineKeyboardButton("🎯 미션", callback_data="mission")],
        [InlineKeyboardButton("🎲 주사위", callback_data="dice"),
         InlineKeyboardButton("🎡 룰렛", callback_data="roulette")],
        [InlineKeyboardButton("🧠 퀴즈", callback_data="quiz"),
         InlineKeyboardButton("💎 부스트(60초 x2)", callback_data="boost")],
        [InlineKeyboardButton("💰 내 포인트", callback_data="my_points"),
         InlineKeyboardButton("📊 통계", callback_data="stats")],
    ])

def quiz_kb(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("A", callback_data=f"quiz_answer:{round_id}:A"),
         InlineKeyboardButton("B", callback_data=f"quiz_answer:{round_id}:B"),
         InlineKeyboardButton("C", callback_data=f"quiz_answer:{round_id}:C")],
    ])


# --------------------
# Quiz bank (간단 3지선다)
# --------------------
QUIZ_BANK = [
    ("파이썬에서 리스트 길이를 구하는 함수는?", "len()", "size()", "count()", "A"),
    ("HTTP 상태코드 404는?", "권한 없음", "서버 오류", "찾을 수 없음", "C"),
    ("Git에서 브랜치 합치는 작업은?", "merge", "clone", "pull", "A"),
    ("SQLite는 무엇인가?", "파일 기반 DB", "그래픽 툴", "클라우드 호스팅", "A"),
]

def upsert_round_quiz(chat_id: int, round_id: int) -> tuple[str, str, str, str, str]:
    # round_id 당 1문제 고정
    with db() as conn:
        row = conn.execute(
            "SELECT question,a,b,c,answer FROM quiz_state WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchone()
        if row:
            return row

        qid = random.randint(1, 10**9)
        q = random.choice(QUIZ_BANK)
        conn.execute("""
            INSERT OR REPLACE INTO quiz_state(chat_id, round_id, qid, question, a, b, c, answer)
            VALUES(?,?,?,?,?,?,?,?)
        """, (chat_id, round_id, qid, q[0], q[1], q[2], q[3], q[4]))
        conn.commit()
        return (q[0], q[1], q[2], q[3], q[4])

def get_round_quiz(chat_id: int, round_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT question,a,b,c,answer FROM quiz_state WHERE chat_id=? AND round_id=?",
            (chat_id, round_id)
        ).fetchone()
        return row


# --------------------
# Commands
# --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("그룹에서 사용해줘!")
        return
    ensure_round(update.effective_chat.id, context)
    await update.message.reply_text("🎮 올림푸스 포인트 이벤트 봇!\n아래 메뉴에서 골라서 해봐.", reply_markup=main_menu_kb())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("그룹에서 사용해줘!")
        return
    ensure_round(update.effective_chat.id, context)
    await update.message.reply_text("메뉴!", reply_markup=main_menu_kb())

async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    p = get_points(chat_id, user_id)
    await update.message.reply_text(f"💰 현재 포인트: {p}")

# 관리자 지급/회수/설정 (답장 기반)
async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    actor = update.effective_user.id
    if not is_admin(actor):
        return await update.message.reply_text("❌ 관리자만 가능")

    if not update.message.reply_to_message:
        return await update.message.reply_text("사용법: 지급할 사람 메시지에 답장으로\n/give 100 이유")

    target = update.message.reply_to_message.from_user
    try:
        amount = int(context.args[0])
    except:
        return await update.message.reply_text("금액 예: /give 100 이유")

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "admin give"
    ensure_round(chat_id, context)

    amount2 = apply_boost(chat_id, target.id, amount)  # 관리자가 주는건 부스트 영향 주기 싫으면 이 줄 제거
    add_points(chat_id, target.id, amount2, reason, actor)
    add_round_earning(chat_id, ROUND_BY_CHAT[chat_id]["round_id"], target.id, max(amount2, 0))

    await update.message.reply_text(f"✅ {target.first_name} +{amount2} 지급 (사유: {reason})")

async def cmd_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    actor = update.effective_user.id
    if not is_admin(actor):
        return await update.message.reply_text("❌ 관리자만 가능")

    if not update.message.reply_to_message:
        return await update.message.reply_text("사용법: 회수할 사람 메시지에 답장으로\n/take 100 이유")

    target = update.message.reply_to_message.from_user
    try:
        amount = int(context.args[0])
    except:
        return await update.message.reply_text("금액 예: /take 100 이유")

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "admin take"
    ensure_round(chat_id, context)

    add_points(chat_id, target.id, -abs(amount), reason, actor)
    await update.message.reply_text(f"✅ {target.first_name} -{abs(amount)} 회수 (사유: {reason})")

async def cmd_setpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    actor = update.effective_user.id
    if not is_admin(actor):
        return await update.message.reply_text("❌ 관리자만 가능")

    if not update.message.reply_to_message:
        return await update.message.reply_text("사용법: 대상 메시지에 답장으로\n/setpoints 1000 이유")

    target = update.message.reply_to_message.from_user
    try:
        value = int(context.args[0])
    except:
        return await update.message.reply_text("값 예: /setpoints 1000 이유")

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "admin set"
    ensure_round(chat_id, context)

    with db() as conn:
        cur = conn.execute("SELECT points FROM points WHERE chat_id=? AND user_id=?", (chat_id, target.id)).fetchone()
        old = cur[0] if cur else 0
    delta = value - old
    add_points(chat_id, target.id, delta, reason, actor)
    if delta > 0:
        add_round_earning(chat_id, ROUND_BY_CHAT[chat_id]["round_id"], target.id, delta)

    await update.message.reply_text(f"✅ {target.first_name} 포인트를 {value}로 설정 (사유: {reason})")


# --------------------
# Callbacks (buttons)
# --------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    chat_id = q.message.chat_id
    user = q.from_user
    user_id = user.id

    ensure_round(chat_id, context)
    rid = ROUND_BY_CHAT[chat_id]["round_id"]

    data = q.data

    # ---- My Points
    if data == "my_points":
        p = get_points(chat_id, user_id)
        return await q.edit_message_text(f"💰 {user.first_name} 포인트: {p}", reply_markup=main_menu_kb())

    # ---- Boost
    if data == "boost":
        set_boost(chat_id, user_id, BOOST_SECONDS)
        bonus = update_streak_and_get_bonus(chat_id, user_id)
        if bonus > 0:
            add_points(chat_id, user_id, bonus, "streak bonus", user_id)
            add_round_earning(chat_id, rid, user_id, bonus)
        return await q.edit_message_text(
            f"💎 부스트 ON! {BOOST_SECONDS}초 동안 보상 x{BOOST_MULTIPLIER}\n"
            f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
            reply_markup=main_menu_kb()
        )

    # ---- Check-in
    if data == "checkin":
        day = utc_day()
        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM checkins WHERE chat_id=? AND user_id=? AND day_utc=?",
                (chat_id, user_id, day)
            ).fetchone()
            if exists:
                return await q.edit_message_text("✅ 오늘은 이미 출석했어!", reply_markup=main_menu_kb())

            conn.execute(
                "INSERT INTO checkins(chat_id, user_id, day_utc) VALUES(?,?,?)",
                (chat_id, user_id, day)
            )
            conn.commit()

        reward = apply_boost(chat_id, user_id, DAILY_CHECKIN_REWARD)
        add_points(chat_id, user_id, reward, "daily checkin", user_id)
        add_round_earning(chat_id, rid, user_id, reward)

        bonus = update_streak_and_get_bonus(chat_id, user_id)
        if bonus > 0:
            add_points(chat_id, user_id, bonus, "streak bonus", user_id)
            add_round_earning(chat_id, rid, user_id, bonus)

        return await q.edit_message_text(
            f"✅ 출석 완료! +{reward}\n"
            f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
            reply_markup=main_menu_kb()
        )

    # ---- Mission (simple: random reward + message)
    if data == "mission":
        base = random.randint(*MISSION_REWARD_RANGE)
        reward = apply_boost(chat_id, user_id, base)

        add_points(chat_id, user_id, reward, "mission complete", user_id)
        add_round_earning(chat_id, rid, user_id, reward)

        bonus = update_streak_and_get_bonus(chat_id, user_id)
        if bonus > 0:
            add_points(chat_id, user_id, bonus, "streak bonus", user_id)
            add_round_earning(chat_id, rid, user_id, bonus)

        missions = [
            "오늘 한 번 웃기기 😆",
            "좋은 말 한마디 하기 💬",
            "물 한 컵 마시기 💧",
            "스트레칭 30초 🧘",
            "채팅에 이모지 3개 남기기 😀😀😀",
        ]
        m = random.choice(missions)

        return await q.edit_message_text(
            f"🎯 미션: {m}\n보상: +{reward}\n"
            f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
            reply_markup=main_menu_kb()
        )

    # ---- Dice (no bet, just reward)
    if data == "dice":
        roll = random.randint(1, 6)
        base = random.randint(*DICE_REWARD_RANGE) + roll * 10
        reward = apply_boost(chat_id, user_id, base)

        add_points(chat_id, user_id, reward, f"dice roll {roll}", user_id)
        add_round_earning(chat_id, rid, user_id, reward)

        bonus = update_streak_and_get_bonus(chat_id, user_id)
        if bonus > 0:
            add_points(chat_id, user_id, bonus, "streak bonus", user_id)
            add_round_earning(chat_id, rid, user_id, bonus)

        return await q.edit_message_text(
            f"🎲 주사위: {roll}\n보상: +{reward}\n"
            f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
            reply_markup=main_menu_kb()
        )

    # ---- Roulette (no bet, just random)
    if data == "roulette":
        # 0~400 (가끔 0도 나오게)
        base = random.randint(*ROULETTE_REWARD_RANGE)
        # 약간의 잭팟
        if random.random() < 0.05:
            base += 800

        reward = apply_boost(chat_id, user_id, base)
        add_points(chat_id, user_id, reward, "roulette", user_id)
        add_round_earning(chat_id, rid, user_id, reward)

        bonus = update_streak_and_get_bonus(chat_id, user_id)
        if bonus > 0:
            add_points(chat_id, user_id, bonus, "streak bonus", user_id)
            add_round_earning(chat_id, rid, user_id, bonus)

        return await q.edit_message_text(
            f"🎡 룰렛 결과!\n보상: +{reward}\n"
            f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
            reply_markup=main_menu_kb()
        )

    # ---- Quiz (round fixed question)
    if data == "quiz":
        quiz = upsert_round_quiz(chat_id, rid)
        question, a, b, c, answer = quiz
        return await q.edit_message_text(
            f"🧠 퀴즈 (라운드 #{rid})\n{question}\n\nA) {a}\nB) {b}\nC) {c}",
            reply_markup=quiz_kb(rid)
        )

    # ---- Quiz answer
    if data.startswith("quiz_answer:"):
        _, rid_s, pick = data.split(":")
        rid2 = int(rid_s)

        # 현재 라운드가 바뀌었으면 무효 처리
        if rid2 != rid:
            return await q.edit_message_text("⏱ 라운드가 이미 바뀌었어! 새 라운드에서 다시 퀴즈 눌러줘.", reply_markup=main_menu_kb())

        quiz = get_round_quiz(chat_id, rid2)
        if not quiz:
            return await q.edit_message_text("퀴즈가 아직 없어. 다시 퀴즈 눌러줘!", reply_markup=main_menu_kb())

        question, a, b, c, ans = quiz

        # 같은 라운드 퀴즈 중복 보상 방지: ledger reason으로 체크
        with db() as conn:
            already = conn.execute("""
                SELECT 1 FROM ledger
                WHERE chat_id=? AND user_id=? AND reason=?
                LIMIT 1
            """, (chat_id, user_id, f"quiz:{rid2}")).fetchone()

        if already:
            return await q.edit_message_text("✅ 이번 라운드 퀴즈 보상은 이미 받았어!", reply_markup=main_menu_kb())

        if pick == ans:
            base = QUIZ_REWARD
            reward = apply_boost(chat_id, user_id, base)
            add_points(chat_id, user_id, reward, f"quiz:{rid2}", user_id)
            add_round_earning(chat_id, rid2, user_id, reward)

            bonus = update_streak_and_get_bonus(chat_id, user_id)
            if bonus > 0:
                add_points(chat_id, user_id, bonus, "streak bonus", user_id)
                add_round_earning(chat_id, rid2, user_id, bonus)

            return await q.edit_message_text(
                f"✅ 정답! (+{reward})\n"
                f"{'🔥 연속참여 보너스 +' + str(bonus) if bonus>0 else ''}",
                reply_markup=main_menu_kb()
            )
        else:
            # 오답은 보상 없음(원하면 위로상 50 같은거 넣어도 됨)
            return await q.edit_message_text(
                f"❌ 오답! 정답은 {ans}\n다음 라운드에서 다시 도전!",
                reply_markup=main_menu_kb()
            )

    # ---- Stats
    if data == "stats":
        with db() as conn:
            total = conn.execute("SELECT COALESCE(SUM(points),0) FROM points WHERE chat_id=?", (chat_id,)).fetchone()[0]
            issued = conn.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE chat_id=? AND delta>0", (chat_id,)).fetchone()[0]
            removed = conn.execute("SELECT COALESCE(SUM(-delta),0) FROM ledger WHERE chat_id=? AND delta<0", (chat_id,)).fetchone()[0]
            top = conn.execute("""
                SELECT user_id, points FROM points
                WHERE chat_id=?
                ORDER BY points DESC
                LIMIT 5
            """, (chat_id,)).fetchall()

        lines = [
            "📊 운영 통계",
            f"• 전체 포인트 합: {total}",
            f"• 누적 발급(+): {issued}",
            f"• 누적 차감(-): {removed}",
            "",
            "🏆 TOP 5 (보유 포인트):",
        ]
        if top:
            for i, (uid, p) in enumerate(top, start=1):
                lines.append(f"{i}) {uid} : {p}")
        else:
            lines.append("데이터 없음")

        return await q.edit_message_text("\n".join(lines), reply_markup=main_menu_kb())

    # fallback
    await q.edit_message_text("메뉴!", reply_markup=main_menu_kb())


# --------------------
# Run
# --------------------
async def post_init(app: Application):
    init_db()

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("points", cmd_points))

    app.add_handler(CommandHandler("give", cmd_give))
    app.add_handler(CommandHandler("take", cmd_take))
    app.add_handler(CommandHandler("setpoints", cmd_setpoints))

    app.add_handler(CallbackQueryHandler(on_button))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
