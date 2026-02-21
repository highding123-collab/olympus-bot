import random
from datetime import datetime
from database import db

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
ACTIVITY_MAX_STEPS = 20  # 하루 최대 200메시지

# =========================
# 유저 기본 처리
# =========================

def ensure_user(user_id, username):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("""
                INSERT INTO users(user_id, username, points)
                VALUES(?,?,?)
            """, (user_id, username or "", STARTING_POINTS))
            conn.commit()

# =========================
# 베팅
# =========================

def place_bet(chat_id, round_id, user_id, choice, amount):
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            return "유저 없음"

        if amount <= 0:
            return "금액은 1 이상"

        if amount > user["points"]:
            return "잔액 부족"

        # 기존 베팅 환급
        prev = conn.execute("""
            SELECT amount FROM bets 
            WHERE chat_id=? AND round_id=? AND user_id=?
        """, (chat_id, round_id, user_id)).fetchone()

        if prev:
            conn.execute("""
                UPDATE users SET points = points + ?
                WHERE user_id=?
            """, (prev["amount"], user_id))

        # 차감
        conn.execute("""
            UPDATE users SET points = points - ?
            WHERE user_id=?
        """, (amount, user_id))

        conn.execute("""
            INSERT OR REPLACE INTO bets
            VALUES(?,?,?,?,?)
        """, (chat_id, round_id, user_id, choice, amount))

        conn.commit()

    return "베팅 완료"

def all_in(chat_id, round_id, user_id, choice):
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            return "유저 없음"

        points = user["points"]
        if points <= 0:
            return "올인 불가"

        # 기존 베팅 환급
        prev = conn.execute("""
            SELECT amount FROM bets 
            WHERE chat_id=? AND round_id=? AND user_id=?
        """, (chat_id, round_id, user_id)).fetchone()

        if prev:
            conn.execute("""
                UPDATE users SET points = points + ?
                WHERE user_id=?
            """, (prev["amount"], user_id))
            points += prev["amount"]

        conn.execute("""
            UPDATE users SET points=0 WHERE user_id=?
        """, (user_id,))

        conn.execute("""
            INSERT OR REPLACE INTO bets
            VALUES(?,?,?,?,?)
        """, (chat_id, round_id, user_id, choice, points))

        conn.commit()

    return f"💎 올인 {points}"

# =========================
# DAILY
# =========================

def claim_daily(chat_id, user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")

    with db() as conn:
        row = conn.execute("""
            SELECT 1 FROM daily 
            WHERE chat_id=? AND user_id=? AND day=?
        """, (chat_id, user_id, today)).fetchone()

        if row:
            return "오늘 이미 받음"

        conn.execute("INSERT INTO daily VALUES(?,?,?)",
                     (chat_id, user_id, today))

        conn.execute("""
            UPDATE users SET points = points + ?
            WHERE user_id=?
        """, (DAILY_REWARD, user_id))

        conn.commit()

    return f"+{DAILY_REWARD} 지급"

# =========================
# SPIN
# =========================

def spin_reward(chat_id, user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")

    with db() as conn:
        row = conn.execute("""
            SELECT * FROM spin
            WHERE chat_id=? AND user_id=? AND day=?
        """, (chat_id, user_id, today)).fetchone()

        used = row["used"] if row else 0

        if used >= SPIN_DAILY_LIMIT:
            return "오늘 룰렛 끝"

        rewards = [r for r, w in SPIN_TABLE]
        weights = [w for r, w in SPIN_TABLE]
        prize = random.choices(rewards, weights=weights, k=1)[0]

        if row:
            conn.execute("""
                UPDATE spin SET used=? 
                WHERE chat_id=? AND user_id=? AND day=?
            """, (used+1, chat_id, user_id, today))
        else:
            conn.execute("""
                INSERT INTO spin VALUES(?,?,?,1)
            """, (chat_id, user_id, today))

        conn.execute("""
            UPDATE users SET points = points + ?
            WHERE user_id=?
        """, (prize, user_id))

        conn.commit()

    return f"🎰 +{prize}"

# =========================
# 채팅 적립
# =========================

def activity_reward(chat_id, user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")

    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS activity(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            msg_count INTEGER,
            rewarded_steps INTEGER,
            PRIMARY KEY(chat_id,user_id,day)
        )
        """)
        row = conn.execute("""
            SELECT * FROM activity
            WHERE chat_id=? AND user_id=? AND day=?
        """, (chat_id, user_id, today)).fetchone()

        if not row:
            conn.execute("""
                INSERT INTO activity VALUES(?,?,?,?,?)
            """, (chat_id, user_id, today, 1, 0))
            conn.commit()
            return None

        msg_count = row["msg_count"] + 1
        rewarded = row["rewarded_steps"]

        conn.execute("""
            UPDATE activity SET msg_count=?
            WHERE chat_id=? AND user_id=? AND day=?
        """, (msg_count, chat_id, user_id, today))

        steps = min(msg_count // ACTIVITY_STEP, ACTIVITY_MAX_STEPS)

        if steps > rewarded:
            reward = (steps - rewarded) * ACTIVITY_REWARD
            conn.execute("""
                UPDATE users SET points = points + ?
                WHERE user_id=?
            """, (reward, user_id))

            conn.execute("""
                UPDATE activity SET rewarded_steps=?
                WHERE chat_id=? AND user_id=? AND day=?
            """, (steps, chat_id, user_id, today))

            conn.commit()
            return f"+{reward}"

        conn.commit()
        return None
