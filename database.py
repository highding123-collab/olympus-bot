import sqlite3

DB_PATH = "vip_casino.db"

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
            points INTEGER,
            win_streak INTEGER DEFAULT 0,
            total_bet INTEGER DEFAULT 0,
            total_win INTEGER DEFAULT 0,
            max_streak INTEGER DEFAULT 0
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

        CREATE TABLE IF NOT EXISTS road(
            chat_id INTEGER,
            round_id INTEGER,
            result TEXT
        );

        CREATE TABLE IF NOT EXISTS house(
            chat_id INTEGER PRIMARY KEY,
            profit INTEGER DEFAULT 0,
            rounds INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS shoe(
            chat_id INTEGER PRIMARY KEY,
            cards TEXT,
            position INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            PRIMARY KEY(chat_id,user_id,day)
        );

        CREATE TABLE IF NOT EXISTS spin(
            chat_id INTEGER,
            user_id INTEGER,
            day TEXT,
            used INTEGER,
            PRIMARY KEY(chat_id,user_id,day)
        );
        """)
        conn.commit()
