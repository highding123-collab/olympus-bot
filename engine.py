import random
import json
from database import db

STARTING_POINTS = 200000

PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

STREAK_START = 2
STREAK_STEP = 0.02
STREAK_MAX = 0.20

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]

# =========================
# 카드 유틸
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
            conn.execute("INSERT INTO shoe VALUES(?,?,0)", (chat_id, json.dumps(deck)))
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
        conn.execute("UPDATE shoe SET cards=?, position=? WHERE chat_id=?",
                     (json.dumps(deck), pos, chat_id))
        conn.commit()
    return card

# =========================
# 바카라 룰
# =========================

def play_baccarat(chat_id):
    player = [draw_card(chat_id), draw_card(chat_id)]
    banker = [draw_card(chat_id), draw_card(chat_id)]

    def total(hand):
        return sum(card_value(r) for r, s in hand) % 10

    p = total(player)
    b = total(banker)

    if p in [8,9] or b in [8,9]:
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
        if b <= 2 or \
           (b == 3 and v != 8) or \
           (b == 4 and 2 <= v <= 7) or \
           (b == 5 and 4 <= v <= 7) or \
           (b == 6 and 6 <= v <= 7):
            banker.append(draw_card(chat_id))
            b = total(banker)

    return player, banker, p, b

# =========================
# 연승 보너스
# =========================

def streak_bonus(streak):
    if streak < STREAK_START:
        return 0
    bonus = (streak - STREAK_START + 1) * STREAK_STEP
    return min(bonus, STREAK_MAX)

# =========================
# 정산 로직
# =========================

def settle_round(chat_id, round_id):
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

    lines = []
    lines.append(f"🎲 결과: PLAYER {p} / BANKER {b}")

    for bet in bets:
        uid = bet["user_id"]
        choice = bet["choice"]
        amount = bet["amount"]
        total_bet += amount

        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

        streak = user["win_streak"]
        points = user["points"]

        if result == "T":
            if choice == "T":
                payout = int(amount * PAYOUTS["T"])
                total_payout += payout
                new_points = points + payout
                new_streak = streak + 1
            else:
                payout = amount
                new_points = points + payout
                new_streak = 0
        elif choice == result:
            new_streak = streak + 1
            bonus = streak_bonus(new_streak)
            mult = PAYOUTS[result] + bonus
            payout = int(amount * mult)
            total_payout += payout
            new_points = points + payout
        else:
            payout = 0
            new_points = points
            new_streak = 0

        max_streak = max(user["max_streak"], new_streak)

        with db() as conn:
            conn.execute("""
                UPDATE users 
                SET points=?, win_streak=?, max_streak=?, 
                    total_bet=total_bet+?, 
                    total_win=total_win+?
                WHERE user_id=?
            """, (new_points, new_streak, max_streak,
                  amount, payout, uid))
            conn.commit()

        if payout > 0:
            lines.append(f"🔥 {uid} +{payout}")
        else:
            lines.append(f"❌ {uid}")

    with db() as conn:
        row = conn.execute("SELECT * FROM house WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO house VALUES(?,?,?)", (chat_id, 0, 0))
            profit = 0
            rounds = 0
        else:
            profit = row["profit"]
            rounds = row["rounds"]

        profit += total_bet - total_payout
        rounds += 1

        conn.execute("UPDATE house SET profit=?, rounds=? WHERE chat_id=?",
                     (profit, rounds, chat_id))

        conn.execute("INSERT INTO road VALUES(?,?,?)",
                     (chat_id, round_id, result))

        conn.execute("DELETE FROM bets WHERE chat_id=?", (chat_id,))
        conn.execute("UPDATE rounds SET status='CLOSED' WHERE chat_id=?", (chat_id,))
        conn.commit()

    return "\n".join(lines), result
