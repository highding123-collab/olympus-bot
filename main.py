import os
import sqlite3
import random
import asyncio
import json
from datetime import datetime
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from PIL import Image, ImageDraw

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "casino.db"

STARTING_POINTS = 200000
ROUND_SECONDS = 60
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

BET_CHOICES = {"P": "플레이어", "B": "뱅커", "T": "타이"}
PAYOUTS = {"P": 2.0, "B": 1.95, "T": 8.0}

SUIT = ["♠", "♥", "♦", "♣"]
RANK = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]

# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER
        );
        CREATE TABLE IF NOT EXISTS rounds(
            chat_id INTEGER PRIMARY KEY,
            round_id INTEGER,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS bets(
            chat_id INTEGER,
            round_id INTEGER,
            user_id INTEGER,
            choice TEXT,
            amount INTEGER
        );
        CREATE TABLE IF NOT EXISTS house(
            chat_id INTEGER PRIMARY KEY,
            profit INTEGER DEFAULT 0,
            rounds INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS road_history(
            chat_id INTEGER,
            round_id INTEGER,
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS shoe(
            chat_id INTEGER PRIMARY KEY,
            cards TEXT,
            position INTEGER
        );
        CREATE TABLE IF NOT EXISTS daily_claims(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            PRIMARY KEY(chat_id,user_id,day)
        );
        CREATE TABLE IF NOT EXISTS spin_claims(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            used INTEGER,
            PRIMARY KEY(chat_id,user_id,day)
        );
        """)
        conn.commit()

# ---------------- 유저 ----------------

def ensure_user(uid, username):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users VALUES(?,?,?)",
                (uid, username or "", STARTING_POINTS)
            )
            conn.commit()

def get_points(uid):
    with db() as conn:
        r = conn.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
        return r["points"] if r else 0

def set_points(uid, p):
    with db() as conn:
        conn.execute("UPDATE users SET points=? WHERE user_id=?", (p, uid))
        conn.commit()

# ---------------- 슈 ----------------

def card_value(rank):
    if rank == "A": return 1
    if rank in ["10","J","Q","K"]: return 0
    return int(rank)

def create_shoe():
    deck = []
    for _ in range(8):
        for s in SUIT:
            for r in RANK:
                deck.append((r,s))
    random.shuffle(deck)
    return deck

def get_shoe(chat_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM shoe WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            deck = create_shoe()
            conn.execute("INSERT INTO shoe VALUES(?,?,0)", (chat_id,json.dumps(deck)))
            conn.commit()
            return deck,0
        return json.loads(row["cards"]), row["position"]

def draw_card(chat_id):
    deck,pos = get_shoe(chat_id)
    if pos >= len(deck)-6:
        deck = create_shoe()
        pos=0
    card = deck[pos]
    pos+=1
    with db() as conn:
        conn.execute("UPDATE shoe SET cards=?, position=? WHERE chat_id=?",
                     (json.dumps(deck),pos,chat_id))
        conn.commit()
    return card

# ---------------- 바카라 엔진 ----------------

def play_baccarat(chat_id):
    player=[draw_card(chat_id),draw_card(chat_id)]
    banker=[draw_card(chat_id),draw_card(chat_id)]

    def total(hand): return sum(card_value(r) for r,s in hand)%10

    p=total(player); b=total(banker)

    if p in [8,9] or b in [8,9]: return player,banker,p,b

    third=None
    if p<=5:
        third=draw_card(chat_id)
        player.append(third)
        p=total(player)

    if third is None:
        if b<=5:
            banker.append(draw_card(chat_id))
            b=total(banker)
    else:
        v=card_value(third[0])
        if b<=2 or \
           (b==3 and v!=8) or \
           (b==4 and 2<=v<=7) or \
           (b==5 and 4<=v<=7) or \
           (b==6 and 6<=v<=7):
            banker.append(draw_card(chat_id))
            b=total(banker)

    return player,banker,p,b

# ---------------- 빅로드 ----------------

def build_road(chat_id):
    with db() as conn:
        rows=conn.execute("SELECT result FROM road_history WHERE chat_id=? ORDER BY round_id",
                          (chat_id,)).fetchall()
    return [r["result"] for r in rows]

def draw_road_image(chat_id):
    results=build_road(chat_id)
    cell=30
    cols=max(len(results),20)
    img=Image.new("RGB",(cols*cell,6*cell+20),"#111")
    draw=ImageDraw.Draw(img)

    col=-1; row=0; last=None
    for r in results:
        if r=="T": continue
        if r!=last:
            col+=1; row=0
        x0=col*cell+5; y0=row*cell+5
        x1=x0+20; y1=y0+20
        color="#1f4fff" if r=="P" else "#ff2a2a"
        draw.ellipse([x0,y0,x1,y1],fill=color)
        row+=1; last=r

    path=f"road_{chat_id}.png"
    img.save(path)
    return path

# ---------------- 정산 ----------------

async def settle_round(app,chat_id,round_id):
    player,banker,p,b=play_baccarat(chat_id)

    if p>b: result="P"
    elif b>p: result="B"
    else: result="T"

    with db() as conn:
        bets=conn.execute("SELECT * FROM bets WHERE chat_id=? AND round_id=?",
                          (chat_id,round_id)).fetchall()

    total_bet=0; total_payout=0
    lines=[]

    for bet in bets:
        uid=bet["user_id"]; choice=bet["choice"]; amt=bet["amount"]
        total_bet+=amt

        if result=="T":
            if choice=="T":
                payout=int(amt*PAYOUTS["T"])
                set_points(uid,get_points(uid)+payout)
                total_payout+=payout
                lines.append(f"🎯 {uid} +{payout}")
            else:
                set_points(uid,get_points(uid)+amt)
                lines.append(f"↩️ {uid} 환급")
            continue

        if choice==result:
            payout=int(amt*PAYOUTS[result])
            set_points(uid,get_points(uid)+payout)
            total_payout+=payout
            lines.append(f"✅ {uid} +{payout}")
        else:
            lines.append(f"❌ {uid}")

    with db() as conn:
        row=conn.execute("SELECT * FROM house WHERE chat_id=?",(chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO house VALUES(?,?,?)",(chat_id,0,0))
            profit=0; rounds=0
        else:
            profit=row["profit"]; rounds=row["rounds"]

        profit+=total_bet-total_payout; rounds+=1
        conn.execute("UPDATE house SET profit=?, rounds=? WHERE chat_id=?",
                     (profit,rounds,chat_id))
        conn.execute("INSERT INTO road_history VALUES(?,?,?)",
                     (chat_id,round_id,result))
        conn.execute("DELETE FROM bets WHERE chat_id=?",(chat_id,))
        conn.execute("UPDATE rounds SET status='CLOSED' WHERE chat_id=?",
                     (chat_id,))
        conn.commit()

    path=draw_road_image(chat_id)
    await app.bot.send_photo(chat_id,photo=open(path,"rb"))
    await app.bot.send_message(chat_id,"\n".join(lines))

# ---------------- 명령어 ----------------

async def cmd_start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat
    with db() as conn:
        r=conn.execute("SELECT * FROM rounds WHERE chat_id=?",(chat.id,)).fetchone()
        rid=1 if not r else r["round_id"]+1
        conn.execute("INSERT OR REPLACE INTO rounds VALUES(?,?,?)",
                     (chat.id,rid,"OPEN"))
        conn.commit()
    asyncio.create_task(delayed_settle(context.application,chat.id,rid))
    await update.message.reply_text(f"라운드 {rid} 시작")

async def delayed_settle(app,chat_id,rid):
    await asyncio.sleep(ROUND_SECONDS)
    await settle_round(app,chat_id,rid)

async def cmd_bet(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; chat=update.effective_chat
    ensure_user(u.id,u.username)
    if len(context.args)<2: return
    amt=int(context.args[0]); choice=context.args[1].upper()
    if amt>get_points(u.id):
        await update.message.reply_text("잔액 부족"); return
    with db() as conn:
        r=conn.execute("SELECT round_id FROM rounds WHERE chat_id=?",(chat.id,)).fetchone()
        if not r: return
        conn.execute("INSERT INTO bets VALUES(?,?,?,?,?)",
                     (chat.id,r["round_id"],u.id,choice,amt))
        conn.commit()
    set_points(u.id,get_points(u.id)-amt)
    await update.message.reply_text("베팅 완료")

async def cmd_daily(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; chat=update.effective_chat
    ensure_user(u.id,u.username)
    today=datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        row=conn.execute("SELECT 1 FROM daily_claims WHERE chat_id=? AND user_id=? AND day=?",
                         (chat.id,u.id,today)).fetchone()
        if row:
            await update.message.reply_text("이미 받음"); return
        conn.execute("INSERT INTO daily_claims VALUES(?,?,?)",
                     (chat.id,u.id,today))
        conn.commit()
    set_points(u.id,get_points(u.id)+DAILY_REWARD)
    await update.message.reply_text(f"+{DAILY_REWARD} 지급")

async def cmd_spin(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; chat=update.effective_chat
    ensure_user(u.id,u.username)
    today=datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        row=conn.execute("SELECT used FROM spin_claims WHERE chat_id=? AND user_id=? AND day=?",
                         (chat.id,u.id,today)).fetchone()
        used=row["used"] if row else 0
        if used>=SPIN_DAILY_LIMIT:
            await update.message.reply_text("오늘 다 씀"); return
        rewards=[r for r,w in SPIN_TABLE]
        weights=[w for r,w in SPIN_TABLE]
        prize=random.choices(rewards,weights=weights,k=1)[0]
        if row:
            conn.execute("UPDATE spin_claims SET used=? WHERE chat_id=? AND user_id=? AND day=?",
                         (used+1,chat.id,u.id,today))
        else:
            conn.execute("INSERT INTO spin_claims VALUES(?,?,?,1)",
                         (chat.id,u.id,today))
        conn.commit()
    set_points(u.id,get_points(u.id)+prize)
    await update.message.reply_text(f"룰렛 +{prize}")

async def cmd_road(update:Update,context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat
    path=draw_road_image(chat.id)
    await update.message.reply_photo(photo=open(path,"rb"))

# ---------------- MAIN ----------------

def main():
    init_db()
    app=Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("bet",cmd_bet))
    app.add_handler(CommandHandler("daily",cmd_daily))
    app.add_handler(CommandHandler("spin",cmd_spin))
    app.add_handler(CommandHandler("road",cmd_road))

    app.run_polling()

if __name__=="__main__":
    main()
