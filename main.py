# -*- coding: utf-8 -*-
"""MEXC Futures 5m Telegram signal bot. Signal notifications only; no orders."""
import asyncio, logging, os, sqlite3, time
from dataclasses import dataclass
from datetime import datetime, timezone
import aiohttp
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
POLL = max(2, int(os.getenv("POLL_SECONDS", "3")))
DB = os.getenv("DB_PATH", "signals.sqlite3")
BASE = "https://api.mexc.com"
INTERVAL = "Min5"                 # Fixed 5-minute futures candles only
BAR_MS = 300_000

# Each sequence includes its signal candle as the LAST character.
# R = red, G = green. The preceding candles must match exactly and consecutively.
PATTERNS = [
    ("p02", "RRGGGGRGR", "SHORT", "🔴🔴🟢🟢🟢🟢🔴🟢🔴", "117/117 · 36/36"),
    ("p03", "RRGRGGRRR", "LONG",  "🔴🔴🟢🔴🟢🟢🔴🔴🔴", "117/117 · 41/41"),
    ("p04", "GRGRGGRRR", "LONG",  "🟢🔴🟢🔴🟢🟢🔴🔴🔴", "116/116"),
    ("p05", "GGRGGGG",   "SHORT", "🟢🟢🔴🟢🟢🟢🟢", "98,90% · 91"),
    ("p06", "GRGGRRR",   "LONG",  "🟢🔴🟢🟢🔴🔴🔴", "98,69% · 153"),
    ("p08", "RRRGGRG",   "LONG",  "🔴🔴🔴🟢🟢🔴🟢", "Статистика не вказана"),
]
PATTERN_MAP = {p[0]: p for p in PATTERNS}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mexc-signal-bot")

@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    @property
    def color(self):
        return "G" if self.c > self.o else "R" if self.c < self.o else "D"

def connect():
    return sqlite3.connect(DB)

def init_db():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS warnings(
            symbol TEXT, pattern_id TEXT, candle_ms INTEGER, created INTEGER,
            PRIMARY KEY(symbol,pattern_id,candle_ms))""")
        db.execute("""CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, pattern_id TEXT,
            candle_ms INTEGER, side TEXT, entry REAL, checked INTEGER DEFAULT 0,
            favorable INTEGER DEFAULT 0, favorable_candle INTEGER,
            status TEXT DEFAULT 'PENDING',
            outcome TEXT, UNIQUE(symbol,pattern_id,candle_ms))""")
        # Migrate existing Railway SQLite databases without losing saved signals.
        cols = {row[1] for row in db.execute("PRAGMA table_info(signals)")}
        if "favorable_candle" not in cols:
            db.execute("ALTER TABLE signals ADD COLUMN favorable_candle INTEGER")
        removed_ids = ("p01", "p07", "p09", "p10", "p11", "p12", "p13", "p14")
        marks = ",".join("?" for _ in removed_ids)
        db.execute(f"DELETE FROM warnings WHERE pattern_id IN ({marks})", removed_ids)
        db.execute(f"DELETE FROM signals WHERE pattern_id IN ({marks})", removed_ids)

def stamp(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")

async def tg(session, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    async with session.post(url, json={"chat_id": CHAT_ID, "text": text},
                            timeout=15) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"Telegram API {r.status}: {body}")
    log.info("Telegram message sent.")

async def get_candles(session, symbol):
    contract = symbol.replace("USDT", "_USDT")
    url = f"{BASE}/api/v1/contract/kline/{contract}"
    async with session.get(url, params={"interval": INTERVAL}, timeout=15) as r:
        r.raise_for_status()
        payload = await r.json(content_type=None)
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"MEXC Futures API response error: {payload}")
    d = payload["data"]
    cols = [d.get(k, []) for k in ("time", "open", "high", "low", "close")]
    if not cols[0] or len({len(x) for x in cols}) != 1:
        raise RuntimeError(f"Invalid MEXC candle arrays for {contract}")
    return sorted([Candle(int(t)*1000, float(o), float(h), float(l), float(c))
                   for t,o,h,l,c in zip(*cols)], key=lambda x: x.t)

def preceding_matches(pattern, closed_before):
    need = pattern[1][:-1]
    if not need:
        return True
    if len(closed_before) < len(need):
        return False
    return "".join(c.color for c in closed_before[-len(need):]) == need

async def warn(session, symbol, pattern, candle):
    pid, seq, side, label, stats = pattern
    with connect() as db:
        cur = db.execute("INSERT OR IGNORE INTO warnings VALUES(?,?,?,?)",
                         (symbol, pid, candle.t, int(time.time())))
        if cur.rowcount == 0:
            return
    color = "зеленою 🟢" if candle.color == "G" else "червоною 🔴"
    await tg(session, f"⚠️ ПОПЕРЕДЖЕННЯ — {symbol}\n"
        f"MEXC Futures · 5 хвилин\nПатерн: {label}\n"
        f"Незакрита сигнальна свічка стала {color}.\n"
        f"Напрямок патерну: {side}\nЧас: {stamp(candle.t)}\n\n"
        "Це ще не підтверджений сигнал.")

async def confirm_warnings(session, symbol, closed):
    lookup = {c.t:c for c in closed}
    with connect() as db:
        rows = db.execute("SELECT pattern_id,candle_ms FROM warnings WHERE symbol=?",
                          (symbol,)).fetchall()
    for pid, t in rows:
        candle = lookup.get(t)
        pattern = PATTERN_MAP.get(pid)
        if candle is None or pattern is None or candle.color != pattern[1][-1]:
            continue
        before = [c for c in closed if c.t < t]
        if not preceding_matches(pattern, before):
            continue
        with connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO signals
                (symbol,pattern_id,candle_ms,side,entry) VALUES(?,?,?,?,?)""",
                (symbol,pid,t,pattern[2],candle.c))
            if cur.rowcount == 0:
                continue
        await tg(session, f"✅ ПІДТВЕРДЖЕНИЙ СИГНАЛ — {symbol}\n"
            f"MEXC Futures · 5 хвилин\nПатерн: {pattern[3]}\n"
            f"Напрямок: {'LONG 🟢' if pattern[2]=='LONG' else 'SHORT 🔴'}\n"
            f"Закриття сигнальної свічки: {candle.c}\n"
            f"Час закриття: {stamp(t+BAR_MS)}\n\n"
            "⏳ Перевіряємо наступні 5 закритих свічок.")

async def evaluate(session, symbol, closed):
    with connect() as db:
        rows = db.execute("""SELECT id,pattern_id,candle_ms,side,entry,checked,
            favorable,favorable_candle
            FROM signals WHERE symbol=? AND status='PENDING'""", (symbol,)).fetchall()
    for sid,pid,t,side,entry,checked,favorable,favorable_candle in rows:
        following = [c for c in closed if c.t >= t+BAR_MS][:5]
        if favorable and favorable_candle is None:
            for index, c in enumerate(following[:checked], start=1):
                if ((side == "LONG" and c.c > entry) or
                    (side == "SHORT" and c.c < entry)):
                    favorable_candle = index
                    break
        if len(following) <= checked and favorable_candle is not None:
            # Persist a recovered candle number even when no new candle arrived.
            with connect() as db:
                db.execute("UPDATE signals SET favorable_candle=? WHERE id=?",
                           (favorable_candle,sid))
            continue
        if len(following) <= checked:
            continue
        for index, c in enumerate(following[checked:], start=checked + 1):
            is_favorable = ((side == "LONG" and c.c > entry) or
                            (side == "SHORT" and c.c < entry))
            if is_favorable and not favorable:
                favorable = 1
                favorable_candle = index
        n = len(following)
        outcome = ("WIN" if favorable else "LOSS") if n == 5 else None
        with connect() as db:
            if outcome:
                db.execute("""UPDATE signals SET checked=?,favorable=?,
                    favorable_candle=?,status=?,outcome=? WHERE id=?""",
                    (n,favorable,favorable_candle,outcome,outcome,sid))
            else:
                db.execute("""UPDATE signals SET checked=?,favorable=?,
                    favorable_candle=? WHERE id=?""",
                    (n,favorable,favorable_candle,sid))
        if outcome:
            pattern = PATTERN_MAP[pid]
            winning_text = (f"Виграшна свічка: {favorable_candle}/5"
                            if favorable_candle else "Виграшної свічки: немає")
            await tg(session, f"{'🏆' if outcome=='WIN' else '❌'} РЕЗУЛЬТАТ: {outcome} — {symbol}\\n"
                f"Напрямок: {side}\\nПатерн: {pattern[3]}\\n"
                f"Перевірено: 5/5 наступних закритих свічок\\n"
                f"{winning_text}\\n"
                f"Ціна закриття сигнальної свічки: {entry}\\n"
                f"Сигнал: {stamp(t)}")

async def process(session, symbol):
    candles = await get_candles(session, symbol)
    now = int(time.time()*1000)
    closed = [c for c in candles if c.t + BAR_MS <= now]
    forming = [c for c in candles if c.t <= now < c.t + BAR_MS]
    await evaluate(session, symbol, closed)
    await confirm_warnings(session, symbol, closed)
    if not forming:
        log.warning("No current MEXC 5m candle for %s", symbol)
        return
    current = forming[-1]
    if current.color not in ("R","G"):
        return
    for p in PATTERNS:
        if current.color == p[1][-1] and preceding_matches(p, closed):
            await warn(session, symbol, p, current)

async def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.")
    init_db()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        await tg(session, "🤖 MEXC Futures Signal Bot запущено\n"
            "BTCUSDT · ETHUSDT · тільки 5m\n"
            "Результат за наступними 5 закритими свічками.\n"
            "Автоматичні угоди не відкриваються.")
        while True:
            for symbol in SYMBOLS:
                try:
                    await process(session, symbol)
                except Exception:
                    log.exception("Помилка обробки %s", symbol)
            await asyncio.sleep(POLL)

if __name__ == "__main__":
    asyncio.run(main())
