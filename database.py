"""
SQLite trade veritabanı — her işlemi kaydeder, analiz raporu üretir.
"""

import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger("database")
DB_PATH = Path(__file__).parent / "trades.db"


def _conn():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.executescript("""
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
        """)
    logger.info("Veritabanı hazır")


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
):
    duration = (close_time - open_time).total_seconds() / 60
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO trades
                (symbol, side, entry_price, close_price, margin, leverage,
                 gross_pnl, commission, net_pnl, reason_open, reason_close,
                 open_time, close_time, duration_min, score, btc_trend,
                 market_regime, atr_pct, fear_greed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol, side, entry_price, close_price, margin, leverage,
                gross_pnl, commission, net_pnl, reason_open, reason_close,
                open_time.isoformat(), close_time.isoformat(), duration,
                score, btc_trend, market_regime, atr_pct, fear_greed,
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
                    1.0 if net_pnl >= 0 else 0.0,  # win_rate
                    0.0,  # profit_factor (tek işlemde anlamsız)
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
                np  = row["net_pnl"]    + net_pnl
                wr  = w / nt
                pf  = (row["gross_pnl"] + gp) / (gl or 0.001)
                mw  = max(row["max_win"],  net_pnl if net_pnl >= 0 else 0)
                ml  = min(row["max_loss"], net_pnl if net_pnl <  0 else 0)
                con.execute("""
                    UPDATE daily_stats SET
                    num_trades=?, wins=?, losses=?, gross_pnl=?, commission=?,
                    net_pnl=?, win_rate=?, profit_factor=?, max_win=?, max_loss=?
                    WHERE stat_date=?
                """, (nt, w, l, gp, com, np, wr, pf, mw, ml, str(stat_date)))
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


def get_performance_summary() -> dict:
    """Tüm zamanların performans özeti."""
    try:
        with _conn() as con:
            r = con.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN net_pnl >= 0 THEN 1 ELSE 0 END) as wins,
                    SUM(net_pnl) as net_pnl,
                    SUM(commission) as commission,
                    AVG(CASE WHEN net_pnl >= 0 THEN net_pnl END) as avg_win,
                    AVG(CASE WHEN net_pnl <  0 THEN net_pnl END) as avg_loss,
                    MAX(net_pnl) as max_win,
                    MIN(net_pnl) as max_loss,
                    AVG(duration_min) as avg_duration
                FROM trades
            """).fetchone()
            if r["total"] == 0:
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
            }
    except Exception as e:
        logger.error(f"Performans özeti alınamadı: {e}")
        return {}
