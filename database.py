"""
SQLite veritabanı — işlemler, kalıcı bot state'i, gözlemlenebilirlik kayıtları.

2026-08-12 refactor:
- Güvenli migration (mevcut trades.db bozulmadan yeni kolonlar eklenir)
- bot_state / open_positions: restart sonrası hiçbir şey sıfırlanmaz
- strategy kolonu: TREND ve MOMENTUM_FAST ayrı ölçülebilir
- entry_snapshot: her işlemin karar anındaki tüm özellikleri (JSON)
- momentum_shadow: gölge modda "açsaydım ne olurdu" kayıtları
"""

import json
import os
import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger("database")

# Testler canlı trades.db'ye yazmasın diye yol ayarlanabilir.
# Üretimde bu değişken tanımlı değildir ve varsayılan dosya kullanılır.
DB_PATH = Path(os.environ.get("CRYPTO_BOT_DB", Path(__file__).parent / "trades.db"))


def _conn():
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    return con


# ── Şema ──────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missed_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT,
    symbol        TEXT,
    action        TEXT,
    score         INTEGER,
    confidence    REAL,
    reason_missed TEXT,
    btc_trend     TEXT,
    market_regime TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    entry_price   REAL,
    close_price   REAL,
    margin        REAL,
    leverage      INTEGER,
    gross_pnl     REAL,
    commission    REAL,
    net_pnl       REAL,
    reason_open   TEXT,
    reason_close  TEXT,
    open_time     TEXT,
    close_time    TEXT,
    duration_min  REAL,
    score         INTEGER,
    btc_trend     TEXT,
    market_regime TEXT,
    atr_pct       REAL,
    fear_greed    INTEGER
);

CREATE TABLE IF NOT EXISTS daily_stats (
    stat_date     TEXT PRIMARY KEY,
    num_trades    INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    gross_pnl     REAL,
    commission    REAL,
    net_pnl       REAL,
    win_rate      REAL,
    profit_factor REAL,
    max_win       REAL,
    max_loss      REAL
);

-- Kalıcı bot state'i. Restart sonrası equity/kill-switch/risk bütçesi buradan gelir.
CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- Açık pozisyonlar. Restart sonrası bot pozisyonlarını unutmaz.
CREATE TABLE IF NOT EXISTS open_positions (
    symbol     TEXT PRIMARY KEY,
    payload    TEXT,
    updated_at TEXT
);

-- Gölge mod: MOMENTUM_FAST'in "açsaydım" kayıtları (gerçek işlem yok).
CREATE TABLE IF NOT EXISTS momentum_shadow (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    symbol      TEXT,
    action      TEXT,
    score       INTEGER,
    price       REAL,
    features    TEXT,
    outcome     TEXT,
    peak_pct    REAL,
    trough_pct  REAL,
    evaluated_at TEXT
);

-- Büyük hareket takibi (Big Move Capture KPI'ı için).
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    equity   REAL NOT NULL,
    balance  REAL,
    open_positions INTEGER,
    open_risk_r    REAL,
    mode     TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_at ON equity_snapshots(at);

CREATE TABLE IF NOT EXISTS big_moves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT,
    detected_at   TEXT,
    move_start    TEXT,
    move_pct      REAL,
    first_signal  TEXT,
    signal_score  INTEGER,
    entered       INTEGER,
    entry_at_pct  REAL,
    captured_pct  REAL,
    miss_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_close  ON trades(close_time);
CREATE INDEX IF NOT EXISTS idx_missed_ts     ON missed_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_shadow_ts     ON momentum_shadow(timestamp);
"""

# Mevcut trades tablosuna sonradan eklenen kolonlar.
# (kolon_adı, SQL tipi, varsayılan)
_TRADE_COLUMNS = [
    ("strategy",       "TEXT",    "'TREND'"),
    ("r_multiple",     "REAL",    "0"),
    ("mae_r",          "REAL",    "0"),
    ("mfe_r",          "REAL",    "0"),
    ("risk_amount",    "REAL",    "0"),
    ("initial_stop",   "REAL",    "0"),
    ("partial_pnl",    "REAL",    "0"),
    ("exit_detail",    "TEXT",    "''"),
    ("entry_snapshot", "TEXT",    "''"),
    ("funding_cost",   "REAL",    "0"),
]


def _migrate(con):
    """Mevcut veriyi bozmadan eksik kolonları ekle."""
    existing = {r["name"] for r in con.execute("PRAGMA table_info(trades)")}
    added = []
    for name, sqltype, default in _TRADE_COLUMNS:
        if name not in existing:
            con.execute(
                f"ALTER TABLE trades ADD COLUMN {name} {sqltype} DEFAULT {default}"
            )
            added.append(name)
    if added:
        logger.info(f"Migration: trades tablosuna {len(added)} kolon eklendi: {', '.join(added)}")
    return added


def init_db():
    with _conn() as con:
        con.executescript(_SCHEMA)
        _migrate(con)
    logger.info("Veritabanı hazır")


# ── Kalıcı state ──────────────────────────────────────────────────────────────

def state_set(key: str, value) -> None:
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO bot_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), datetime.now().isoformat()),
            )
    except Exception as e:
        logger.error(f"state_set({key}) hatası: {e}")


def state_get(key: str, default=None):
    try:
        with _conn() as con:
            row = con.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])
    except Exception as e:
        logger.error(f"state_get({key}) hatası: {e}")
        return default


def state_set_many(mapping: dict) -> None:
    now = datetime.now().isoformat()
    try:
        with _conn() as con:
            con.executemany(
                "INSERT INTO bot_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(k, json.dumps(v), now) for k, v in mapping.items()],
            )
    except Exception as e:
        logger.error(f"state_set_many hatası: {e}")


def state_all() -> dict:
    try:
        with _conn() as con:
            return {
                r["key"]: json.loads(r["value"])
                for r in con.execute("SELECT key, value FROM bot_state")
            }
    except Exception:
        return {}


# ── Açık pozisyonların kalıcılığı ────────────────────────────────────────────

def _json_default(o):
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    raise TypeError(f"serileştirilemeyen tip: {type(o)}")


def _json_hook(d):
    if "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    return d


def save_open_positions(positions: dict) -> None:
    """Açık pozisyonların tamamını diske yaz (idempotent, tam senkron)."""
    now = datetime.now().isoformat()
    try:
        with _conn() as con:
            con.execute("DELETE FROM open_positions")
            if positions:
                con.executemany(
                    "INSERT INTO open_positions (symbol, payload, updated_at) VALUES (?,?,?)",
                    [
                        (sym, json.dumps(pos, default=_json_default), now)
                        for sym, pos in positions.items()
                    ],
                )
    except Exception as e:
        logger.error(f"Açık pozisyonlar kaydedilemedi: {e}")


def load_open_positions() -> dict:
    try:
        with _conn() as con:
            rows = con.execute("SELECT symbol, payload FROM open_positions").fetchall()
            out = {}
            for r in rows:
                try:
                    out[r["symbol"]] = json.loads(r["payload"], object_hook=_json_hook)
                except Exception as e:
                    logger.warning(f"Pozisyon yüklenemedi {r['symbol']}: {e}")
            return out
    except Exception as e:
        logger.error(f"Açık pozisyonlar yüklenemedi: {e}")
        return {}


# ── İşlem kaydı ───────────────────────────────────────────────────────────────

def save_trade(
    symbol:       str,
    side:         str,
    entry_price:  float,
    close_price:  float,
    margin:       float,
    leverage:     int,
    gross_pnl:    float,
    commission:   float,
    net_pnl:      float,
    reason_open:  str,
    reason_close: str,
    open_time:    datetime,
    close_time:   datetime,
    score:        int       = 0,
    btc_trend:    str       = "SIDEWAYS",
    market_regime:str       = "UNKNOWN",
    atr_pct:      float     = 0.0,
    fear_greed:   int       = 50,
    strategy:     str       = "TREND",
    r_multiple:   float     = 0.0,
    mae_r:        float     = 0.0,
    mfe_r:        float     = 0.0,
    risk_amount:  float     = 0.0,
    initial_stop: float     = 0.0,
    partial_pnl:  float     = 0.0,
    exit_detail:  str       = "",
    entry_snapshot: dict | None = None,
    funding_cost: float     = 0.0,
):
    duration = (close_time - open_time).total_seconds() / 60
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO trades
                (symbol, side, entry_price, close_price, margin, leverage,
                 gross_pnl, commission, net_pnl, reason_open, reason_close,
                 open_time, close_time, duration_min, score, btc_trend,
                 market_regime, atr_pct, fear_greed,
                 strategy, r_multiple, mae_r, mfe_r, risk_amount, initial_stop,
                 partial_pnl, exit_detail, entry_snapshot, funding_cost)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol, side, entry_price, close_price, margin, leverage,
                gross_pnl, commission, net_pnl, reason_open, reason_close,
                open_time.isoformat(), close_time.isoformat(), duration,
                score, btc_trend, market_regime, atr_pct, fear_greed,
                strategy, r_multiple, mae_r, mfe_r, risk_amount, initial_stop,
                partial_pnl, exit_detail,
                json.dumps(entry_snapshot or {}, default=str), funding_cost,
            ))
        _update_daily_stats(close_time.date(), net_pnl, commission, gross_pnl)
    except Exception as e:
        logger.error(f"Trade kaydedilemedi: {e}")


def _update_daily_stats(stat_date: date, net_pnl: float, commission: float, gross_pnl: float):
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT * FROM daily_stats WHERE stat_date=?", (str(stat_date),)
            ).fetchone()
            if row is None:
                con.execute("""
                    INSERT INTO daily_stats
                    (stat_date, num_trades, wins, losses, gross_pnl, commission,
                     net_pnl, win_rate, profit_factor, max_win, max_loss)
                    VALUES (?,1,?,?,?,?,?,?,?,?,?)
                """, (
                    str(stat_date),
                    1 if net_pnl >= 0 else 0,
                    0 if net_pnl >= 0 else 1,
                    gross_pnl if gross_pnl >= 0 else 0,
                    commission, net_pnl,
                    1.0 if net_pnl >= 0 else 0.0,
                    0.0,
                    net_pnl if net_pnl >= 0 else 0,
                    net_pnl if net_pnl < 0  else 0,
                ))
            else:
                nt  = row["num_trades"] + 1
                w   = row["wins"]   + (1 if net_pnl >= 0 else 0)
                l   = row["losses"] + (0 if net_pnl >= 0 else 1)
                gp  = row["gross_pnl"]  + (gross_pnl if gross_pnl >= 0 else 0)
                gl  = abs(gross_pnl) if gross_pnl < 0 else 0
                com = row["commission"] + commission
                np_ = row["net_pnl"]    + net_pnl
                wr  = w / nt
                pf  = gp / (gl or 0.001)
                mw  = max(row["max_win"],  net_pnl if net_pnl >= 0 else 0)
                ml  = min(row["max_loss"], net_pnl if net_pnl <  0 else 0)
                con.execute("""
                    UPDATE daily_stats SET
                    num_trades=?, wins=?, losses=?, gross_pnl=?, commission=?,
                    net_pnl=?, win_rate=?, profit_factor=?, max_win=?, max_loss=?
                    WHERE stat_date=?
                """, (nt, w, l, gp, com, np_, wr, pf, mw, ml, str(stat_date)))
    except Exception as e:
        logger.error(f"Daily stats güncellenemedi: {e}")


def save_missed_signal(
    symbol:       str,
    action:       str,
    score:        int,
    confidence:   float,
    reason_missed:str,
    btc_trend:    str = "SIDEWAYS",
    market_regime:str = "UNKNOWN",
):
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO missed_signals
                (timestamp, symbol, action, score, confidence,
                 reason_missed, btc_trend, market_regime)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                datetime.now().isoformat(),
                symbol, action, score, confidence,
                reason_missed, btc_trend, market_regime,
            ))
    except Exception as e:
        logger.debug(f"Missed signal kaydedilemedi: {e}")


def save_shadow_signal(symbol: str, action: str, score: int, price: float, features: dict):
    """Gölge modda MOMENTUM_FAST sinyali — gerçek işlem açılmaz, sadece kaydedilir."""
    try:
        with _conn() as con:
            cur = con.execute("""
                INSERT INTO momentum_shadow
                (timestamp, symbol, action, score, price, features, outcome,
                 peak_pct, trough_pct, evaluated_at)
                VALUES (?,?,?,?,?,?,'PENDING',0,0,NULL)
            """, (
                datetime.now().isoformat(), symbol, action, score, price,
                json.dumps(features, default=str),
            ))
            return cur.lastrowid
    except Exception as e:
        logger.debug(f"Shadow signal kaydedilemedi: {e}")
        return None


def update_shadow_outcome(row_id: int, outcome: str, peak_pct: float, trough_pct: float):
    try:
        with _conn() as con:
            con.execute(
                "UPDATE momentum_shadow SET outcome=?, peak_pct=?, trough_pct=?, evaluated_at=? "
                "WHERE id=?",
                (outcome, peak_pct, trough_pct, datetime.now().isoformat(), row_id),
            )
    except Exception as e:
        logger.debug(f"Shadow outcome güncellenemedi: {e}")


def get_pending_shadow_signals(max_age_min: int = 240) -> list[dict]:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM momentum_shadow WHERE outcome='PENDING' ORDER BY id DESC LIMIT 500"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def save_big_move(symbol: str, move_pct: float, move_start: str, first_signal: str | None,
                  signal_score: int, entered: bool, entry_at_pct: float,
                  captured_pct: float, miss_reason: str):
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO big_moves
                (symbol, detected_at, move_start, move_pct, first_signal, signal_score,
                 entered, entry_at_pct, captured_pct, miss_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol, datetime.now().isoformat(), move_start, move_pct,
                first_signal, signal_score, 1 if entered else 0,
                entry_at_pct, captured_pct, miss_reason,
            ))
    except Exception as e:
        logger.debug(f"Big move kaydedilemedi: {e}")


# ── Raporlama ─────────────────────────────────────────────────────────────────

def get_recent_trades(n: int = 10) -> list[dict]:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_daily_report(days: int = 7) -> list[dict]:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM daily_stats ORDER BY stat_date DESC LIMIT ?", (days,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_recent_pnls(n: int = 30) -> list[float]:
    """Dinamik risk hesabı için son N işlemin net PnL'i (restart'a dayanıklı)."""
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT net_pnl FROM trades ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [r["net_pnl"] for r in reversed(rows)]
    except Exception:
        return []


def get_performance_summary(strategy: str | None = None) -> dict:
    """Performans özeti. strategy verilirse yalnız o stratejinin işlemleri."""
    where = ""
    params: tuple = ()
    if strategy:
        where = "WHERE strategy = ?"
        params = (strategy,)
    try:
        with _conn() as con:
            r = con.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN net_pnl >= 0 THEN 1 ELSE 0 END) as wins,
                    SUM(net_pnl) as net_pnl,
                    SUM(commission) as commission,
                    AVG(CASE WHEN net_pnl >= 0 THEN net_pnl END) as avg_win,
                    AVG(CASE WHEN net_pnl <  0 THEN net_pnl END) as avg_loss,
                    MAX(net_pnl) as max_win,
                    MIN(net_pnl) as max_loss,
                    AVG(duration_min) as avg_duration,
                    AVG(r_multiple) as avg_r
                FROM trades {where}
            """, params).fetchone()
            if not r or r["total"] == 0:
                return {}
            total = r["total"]
            wins  = r["wins"] or 0
            losses= total - wins
            avg_w = r["avg_win"]  or 0
            avg_l = abs(r["avg_loss"] or 0)
            pf    = (avg_w * wins) / (avg_l * losses) if losses > 0 and avg_l > 0 else 0
            return {
                "total":        total,
                "wins":         wins,
                "losses":       losses,
                "win_rate":     wins / total,
                "net_pnl":      r["net_pnl"] or 0,
                "commission":   r["commission"] or 0,
                "avg_win":      avg_w,
                "avg_loss":     avg_l,
                "max_win":      r["max_win"] or 0,
                "max_loss":     r["max_loss"] or 0,
                "profit_factor":round(pf, 2),
                "avg_duration": r["avg_duration"] or 0,
                "avg_r":        round(r["avg_r"] or 0, 3),
            }
    except Exception as e:
        logger.error(f"Performans özeti alınamadı: {e}")
        return {}


def get_summary_excluding_top(n: int = 2) -> dict:
    """
    En büyük N kazananı çıkararak özet.

    Gerçek paraya geçiş kapılarından biri: kâr birkaç şanslı işleme mi bağlı,
    yoksa sistemin gerçek bir edge'i mi var? 2026-08-12 ölçümünde 82 işlemin
    kârının %117'si 2 işlemden geliyordu — bu metrik onu görünür kılar.
    """
    try:
        with _conn() as con:
            r = con.execute("""
                SELECT COUNT(*) as total,
                       SUM(net_pnl) as net_pnl,
                       SUM(CASE WHEN net_pnl >= 0 THEN 1 ELSE 0 END) as wins
                FROM trades
                WHERE id NOT IN (SELECT id FROM trades ORDER BY net_pnl DESC LIMIT ?)
            """, (n,)).fetchone()
            if not r or r["total"] == 0:
                return {}
            return {
                "total":   r["total"],
                "net_pnl": r["net_pnl"] or 0,
                "wins":    r["wins"] or 0,
                "win_rate": (r["wins"] or 0) / r["total"],
                "excluded": n,
            }
    except Exception:
        return {}


def save_equity_snapshot(equity: float, balance: float = 0.0,
                         open_positions: int = 0, open_risk_r: float = 0.0,
                         mode: str = "paper") -> None:
    """
    Periyodik equity örneği.

    Gerçek maksimum drawdown yalnız kapanmış işlemlerden ÖLÇÜLEMEZ: -3R'ye
    inip +1R'de kapanan bir işlem kayıtta sıfır drawdown'lı bir kazançtır.
    Gerçek paraya geçiş kapısının anlamlı olması için düzenli örnekleme şart.
    """
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO equity_snapshots "
                "(at, equity, balance, open_positions, open_risk_r, mode) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), float(equity),
                 float(balance), int(open_positions), float(open_risk_r), mode),
            )
    except Exception as e:
        logger.debug(f"Equity snapshot yazılamadı: {e}")
