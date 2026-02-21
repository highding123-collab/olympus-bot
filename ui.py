from PIL import Image, ImageDraw
from database import db

CELL = 30

# =========================
# 카드 표시 문자열
# =========================

def format_cards(hand):
    return " ".join([f"{r}{s}" for r, s in hand])

# =========================
# 빅로드 데이터
# =========================

def get_road(chat_id):
    with db() as conn:
        rows = conn.execute("""
            SELECT result FROM road
            WHERE chat_id=?
            ORDER BY round_id
        """, (chat_id,)).fetchall()
    return [r["result"] for r in rows]

# =========================
# 빅로드 이미지 생성
# =========================

def draw_road_image(chat_id):
    results = get_road(chat_id)

    cols = max(len(results), 20)
    img = Image.new("RGB", (cols * CELL, 6 * CELL + 20), "#111")
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

        x0 = col * CELL + 5
        y0 = row * CELL + 5
        x1 = x0 + 20
        y1 = y0 + 20

        color = "#1f4fff" if r == "P" else "#ff2a2a"
        draw.ellipse([x0, y0, x1, y1], fill=color)

        row += 1
        last = r

    path = f"road_{chat_id}.png"
    img.save(path)
    return path

# =========================
# 개인 통계
# =========================

def user_stats(user_id):
    with db() as conn:
        user = conn.execute("""
            SELECT * FROM users
            WHERE user_id=?
        """, (user_id,)).fetchone()

    if not user:
        return "데이터 없음"

    total_bet = user["total_bet"]
    total_win = user["total_win"]

    if total_bet == 0:
        roi = 0
    else:
        roi = ((total_win - total_bet) / total_bet) * 100

    return (
        f"📊 개인 통계\n"
        f"포인트: {user['points']}\n"
        f"연승: {user['win_streak']}\n"
        f"최고연승: {user['max_streak']}\n"
        f"총 베팅: {total_bet}\n"
        f"총 획득: {total_win}\n"
        f"ROI: {roi:.2f}%"
    )

# =========================
# 랭킹
# =========================

def rank_top10():
    with db() as conn:
        rows = conn.execute("""
            SELECT username, points, max_streak
            FROM users
            ORDER BY points DESC
            LIMIT 10
        """).fetchall()

    lines = ["🏆 TOP 10"]

    for i, r in enumerate(rows, start=1):
        name = r["username"] or "익명"
        lines.append(f"{i}. {name} - {r['points']} (🔥{r['max_streak']})")

    return "\n".join(lines)

# =========================
# 하우스 통계
# =========================

def house_stats(chat_id):
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM house
            WHERE chat_id=?
        """, (chat_id,)).fetchone()

    if not row:
        return "하우스 데이터 없음"

    return (
        f"🏦 하우스 통계\n"
        f"누적 수익: {row['profit']}\n"
        f"총 라운드: {row['rounds']}"
    )
