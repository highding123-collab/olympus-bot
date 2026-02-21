import os
import sqlite3
import random
from datetime import datetime

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

# --- ENV ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN".lower())
DB = "points.db"

# --- GAME CONFIG ---
STARTING_POINTS = 100000

# Baccarat-style choices
BET_CHOICES = ["P", "B", "T"]  # Player, Banker, Tie

# Rough real baccarat probabilities (approx)
# Player 44.62%, Banker 45.86%, Tie 9.52%
RESULT_WEIGHTS = {"P": 44.62, "B": 45.86, "T": 9.52}

# Payouts (including returning stake)
# Player: 2.0x, Banker: 1.95x (5% commission), Tie: 8x
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

# If Tie occurs: refund Player/Banker bets (stake back), Tie bets pay out
TIE_REFUND_PB = True

# Admin list (optional). If empty, chat admins can still /close
ADMIN_IDS: list[int] = []


# --- DB HELPERS ---
def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 1000,
            streak INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bets_chat ON bets(chat_id)")
        conn.commit()


def ensure_user(user):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, points, streak) VALUES (?, ?, ?, 0)",
                (user.id, user.username or "", STARTING_POINTS)
            )
        else:
            # keep username fresh-ish
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (user.username or "", user.id))
        conn.commit()


def get_points(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0


def add_points(user_id: int, delta: int):
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (delta, user_id))
        conn.commit()


def set_streak(user_id: int, streak: int):
    with db() as conn:
        conn.execute("UPDATE users SET streak=? WHERE user_id=?", (streak, user_id))
        conn.commit()


def inc_streak(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET streak = streak + 1 WHERE user_id=?", (user_id,))
        conn.commit()


def reset_streak(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET streak = 0 WHERE user_id=?", (user_id,))
        conn.commit()


def get_streak(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT streak FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0


def upsert_bet(chat_id: int, user_id: int, amount: int, choice: str):
    now = datetime.utcnow().isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO bets (chat_id, user_id, amount, choice, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET amount=excluded.amount, choice=excluded.choice, created_at=excluded.created_at
            """,
            (chat_id, user_id, amount, choice, now)
        )
        conn.commit()


def get_bets(chat_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT user_id, amount, choice FROM bets WHERE chat_id=?",
            (chat_id,)
        ).fetchall()


def clear_bets(chat_id: int):
    with db() as conn:
        conn.execute("DELETE FROM bets WHERE chat_id=?", (chat_id,))
        conn.commit()


def get_user_bet(chat_id: int, user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT amount, choice FROM bets WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()


# --- AUTH ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def choice_label(c: str) -> str:
    return {"P": "플(P)", "B": "뱅(B)", "T": "타이(T)"}.get(c, c)


def weighted_result() -> str:
    items = list(RESULT_WEIGHTS.items())
    choices = [k for k, _ in items]
    weights = [w for _, w in items]
    return random.choices(choices, weights=weights, k=1)[0]


# --- COMMANDS ---
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎰 포인트 바카라 봇\n\n"
        "사용법(그룹에서):\n"
        "• /balance  → 내 포인트\n"
        "• /bet 100 P  → 100포인트 플(PLAYER)\n"
        "• /bet 200 B  → 200포인트 뱅(BANKER)\n"
        "• /bet 50 T   → 50포인트 타이(TIE)\n"
        "• /bets       → 현재 라운드 베팅 현황\n"
        "• /rank       → 상위 랭킹(포인트/연승)\n\n"
        "정산(관리자/방장/관리자 권한 필요):\n"
        "• /close        → 바카라 확률로 랜덤 결과 & 정산\n"
        "• /close P|B|T  → 결과를 수동 지정해서 정산\n\n"
        "규칙:\n"
        f"• 플 배당: {PAYOUTS['P']}x\n"
        f"• 뱅 배당: {PAYOUTS['B']}x (수수료)\n"
        f"• 타이 배당: {PAYOUTS['T']}x\n"
        f"• 타이 나오면 플/뱅은 {'환불' if TIE_REFUND_PB else '패배'}\n"
        "• 이기면 연승 +1, 지면 0으로 초기화\n"
    )
    await update.message.reply_text(text)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    pts = get_points(update.effective_user.id)
    streak = get_streak(update.effective_user.id)
    await update.message.reply_text(f"💰 포인트: {pts}\n🔥 연승: {streak}")


async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.message.reply_text("그룹에서만 사용 가능해.")

    ensure_user(update.effective_user)

    if len(context.args) != 2:
        return await update.message.reply_text("형식: /bet 100 P  (P=플, B=뱅, T=타이)")

    # amount
    try:
        amount = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("금액은 숫자로 입력해줘. 예: /bet 100 P")

    if amount <= 0:
        return await update.message.reply_text("0보다 큰 금액만 가능해.")

    choice = context.args[1].upper()
    if choice not in BET_CHOICES:
        return await update.message.reply_text("선택은 P / B / T 중 하나야. 예: /bet 100 B")

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # if user already had a bet, refund it first, then place new
    prev = get_user_bet(chat_id, user_id)
    if prev:
        prev_amount, prev_choice = int(prev[0]), prev[1]
        add_points(user_id, prev_amount)  # refund previous stake

    pts = get_points(user_id)
    if pts < amount:
        # if we refunded prev bet above, pts already includes it.
        return await update.message.reply_text(f"포인트 부족 😵 (보유: {pts})")

    # take stake
    add_points(user_id, -amount)
    upsert_bet(chat_id, user_id, amount, choice)

    await update.message.reply_text(f"🎲 베팅 완료: {amount} 포인트 → {choice_label(choice)}")


async def bets_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.message.reply_text("그룹에서만 사용 가능해.")

    chat_id = update.effective_chat.id
    rows = get_bets(chat_id)

    if not rows:
        return await update.message.reply_text("현재 라운드에 베팅이 없어.")

    totals = {"P": 0, "B": 0, "T": 0}
    counts = {"P": 0, "B": 0, "T": 0}
    for _, amount, choice in rows:
        totals[choice] += int(amount)
        counts[choice] += 1

    my = get_user_bet(chat_id, update.effective_user.id)
    my_line = ""
    if my:
        my_line = f"\n\n🙋 내 베팅: {int(my[0])} → {choice_label(my[1])}"

    text = (
        "📊 현재 라운드 베팅 현황\n"
        f"• 플(P): {counts['P']}명 / {totals['P']}p\n"
        f"• 뱅(B): {counts['B']}명 / {totals['B']}p\n"
        f"• 타이(T): {counts['T']}명 / {totals['T']}p"
        f"{my_line}"
    )
    await update.message.reply_text(text)


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.message.reply_text("그룹에서만 사용 가능해.")

    if not (await is_admin(update, context)):
        return await update.message.reply_text("관리자(또는 방장/관리자 권한)만 정산할 수 있어.")

    chat_id = update.effective_chat.id
    rows = get_bets(chat_id)
    if not rows:
        return await update.message.reply_text("정산할 베팅이 없어.")

    # result: /close OR /close P|B|T
    if len(context.args) == 0:
        result = weighted_result()
        result_source = "확률(랜덤)"
    else:
        r = context.args[0].upper()
        if r not in BET_CHOICES:
            return await update.message.reply_text("형식: /close 또는 /close P|B|T")
        result = r
        result_source = "수동"

    # settlement
    winners = []
    refunds = []
    losers = []

    for user_id, amount, choice in rows:
        user_id = int(user_id)
        amount = int(amount)
        choice = choice.upper()

        # Tie handling
        if result == "T" and TIE_REFUND_PB and choice in ("P", "B"):
            # refund stake
            add_points(user_id, amount)
            refunds.append((user_id, amount, choice))
            # streak unchanged on refund
            continue

        if choice == result:
            payout = PAYOUTS[result]
            reward = int(amount * payout)  # includes returning stake by definition of payout
            add_points(user_id, reward)
            winners.append((user_id, amount, choice, reward))
            inc_streak(user_id)
        else:
            losers.append((user_id, amount, choice))
            reset_streak(user_id)

    clear_bets(chat_id)

    # Build message
    lines = []
    lines.append(f"🎰 결과: {choice_label(result)}  ({result_source})")
    lines.append("")
    if winners:
        lines.append("✅ 당첨")
        for uid, amt, ch, rw in winners[:20]:
            lines.append(f"• {uid} : {amt} → {choice_label(ch)}  | +{rw}p")
        if len(winners) > 20:
            lines.append(f"…외 {len(winners)-20}명")
        lines.append("")
    if refunds:
        lines.append("↩️ 환불(타이)")
        for uid, amt, ch in refunds[:20]:
            lines.append(f"• {uid} : {amt} → {choice_label(ch)}  | 환불")
        if len(refunds) > 20:
            lines.append(f"…외 {len(refunds)-20}명")
        lines.append("")
    if losers:
        lines.append("❌ 미당첨")
        for uid, amt, ch in losers[:10]:
            lines.append(f"• {uid} : {amt} → {choice_label(ch)}")
        if len(losers) > 10:
            lines.append(f"…외 {len(losers)-10}명")

    lines.append("\n다음 라운드 베팅: /bet 100 P|B|T")
    await update.message.reply_text("\n".join(lines))


async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(
            "SELECT username, user_id, points, streak FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()

    text = "🏆 랭킹 TOP10\n"
    for i, (name, uid, pts, streak) in enumerate(rows, start=1):
        label = name if name else str(uid)
        text += f"{i}. {label} — {int(pts)}p (🔥{int(streak)})\n"
    await update.message.reply_text(text)


def main():
    if not TOKEN:
        raise Exception("TELEGRAM_BOT_TOKEN 환경변수가 비어있음")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("bet", bet))
    app.add_handler(CommandHandler("bets", bets_status))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("rank", rank))

    app.run_polling()


if __name__ == "__main__":
    main()
