"""
Kalıcılık testleri — restart sonrası hiçbir şey sıfırlanmamalı.

Bu, refactor'ın en kritik güvenlik düzeltmesi. Eski davranış:
`KillSwitchTracker()` her başlatmada argümansız kuruluyordu ve bakiye
PAPER_BALANCE'a dönüyordu. systemd'nin `Restart=always` ayarıyla birleşince
bu, çökme döngüsündeki bir botun günlük zarar limitini SINIRSIZ kez
sıfırlayabilmesi anlamına geliyordu.
"""

from datetime import datetime, timedelta

import pytest

import config
import database
from database import (
    state_set, state_get, state_set_many, state_all,
    save_open_positions, load_open_positions, save_trade, get_recent_pnls,
    get_performance_summary, get_summary_excluding_top,
)
from risk_manager import KillSwitchTracker
from trader import PaperPortfolio


# ── Basit state ───────────────────────────────────────────────────────────────

def test_state_roundtrip():
    state_set("foo", {"a": 1, "b": [1, 2, 3]})
    assert state_get("foo") == {"a": 1, "b": [1, 2, 3]}


def test_state_missing_key_returns_default():
    assert state_get("yok_boyle_bir_key", "varsayilan") == "varsayilan"


def test_state_overwrite():
    state_set("k", 1)
    state_set("k", 2)
    assert state_get("k") == 2


def test_state_set_many():
    state_set_many({"x": 1, "y": "iki", "z": [3]})
    allst = state_all()
    assert allst["x"] == 1 and allst["y"] == "iki" and allst["z"] == [3]


# ── Kill switch kalıcılığı ────────────────────────────────────────────────────

def test_kill_switch_survives_restart():
    ks = KillSwitchTracker(1000.0)
    ks.initialize(1000.0)
    ks.record_trade_open(1000.0, risk_r=1.0)
    ks.record_trade_open(1000.0, risk_r=1.0)
    ks.record_trade_close(-25.0, 975.0, realized_r=-1.0, symbol="AAAUSDT")

    state_set("kill_switch", ks.to_dict())

    # ── Restart simülasyonu ──
    restored = KillSwitchTracker.from_dict(state_get("kill_switch"))

    assert restored.daily_pnl == ks.daily_pnl
    assert restored.daily_trades == ks.daily_trades
    assert restored.daily_risk_used_r == ks.daily_risk_used_r
    assert restored.consecutive_losses == ks.consecutive_losses
    assert restored.high_water_mark == ks.high_water_mark
    assert restored.symbol_on_cooldown("AAAUSDT")


def test_daily_loss_limit_cannot_be_reset_by_restart():
    """Restart artık bir kaçış yolu değil."""
    ks = KillSwitchTracker(1000.0)
    ks.initialize(1000.0)
    ks.record_trade_close(-(1000.0 * config.DAILY_LOSS_LIMIT_PCT + 5), 950.0, symbol="X")
    assert not ks.can_trade(950.0)[0]

    state_set("kill_switch", ks.to_dict())
    restored = KillSwitchTracker.from_dict(state_get("kill_switch"))
    assert not restored.can_trade(950.0)[0], "restart kill switch'i sıfırladı!"


def test_halted_at_survives_restart():
    ks = KillSwitchTracker(1000.0)
    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        ks.record_trade_close(-5.0, 1000.0, symbol="X")
    state_set("kill_switch", ks.to_dict())
    restored = KillSwitchTracker.from_dict(state_get("kill_switch"))
    assert restored._halted_at is not None
    assert not restored.can_trade(1000.0)[0]


# ── Bakiye kalıcılığı ─────────────────────────────────────────────────────────

def test_paper_balance_survives_restart():
    p = PaperPortfolio(1000.0)
    p.balance = 1234.56
    p.total_pnl = 234.56
    p.wins, p.losses = 7, 3
    state_set_many({
        "paper_balance": p.balance, "total_pnl": p.total_pnl,
        "wins": p.wins, "losses": p.losses, "total_commission": 12.3,
    })

    p2 = PaperPortfolio(config.PAPER_BALANCE)
    assert p2.balance == config.PAPER_BALANCE      # taze başlangıç
    p2.balance   = float(state_get("paper_balance"))
    p2.total_pnl = float(state_get("total_pnl"))
    p2.wins      = int(state_get("wins"))
    p2.losses    = int(state_get("losses"))
    assert p2.balance == 1234.56
    assert p2.wins == 7 and p2.losses == 3


# ── Açık pozisyon kalıcılığı ──────────────────────────────────────────────────

def test_open_positions_survive_restart():
    p = PaperPortfolio(1000.0)
    ok, _ = p.open_position(
        "TESTUSDT", "BUY", 100.0, margin=50.0, leverage=3,
        sl=98.0, tp=107.0, atr_pct=0.012, strategy="MOMENTUM_FAST",
        qty=1.5, risk_amount=7.5,
    )
    assert ok
    save_open_positions(p.positions)

    p2 = PaperPortfolio(1000.0)
    n = p2.restore_positions(load_open_positions())
    assert n == 1
    r = p2.positions["TESTUSDT"]
    # Giriş ALEYHTE kayar — kâğıt da canlıyla aynı maliyet modelini uygular.
    assert r["entry_price"] == pytest.approx(100.0 * (1 + config.SLIPPAGE_PCT))
    assert r["strategy"] == "MOMENTUM_FAST"
    assert r["risk_amount"] == pytest.approx(7.5)
    assert isinstance(r["open_time"], datetime), "datetime tipi korunmalı"


def test_restored_position_still_evaluates_correctly():
    """Geri yüklenen pozisyon çıkış motoruyla çalışmaya devam etmeli."""
    from position import evaluate, EXIT_TP1
    p = PaperPortfolio(1000.0)
    p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    atr_pct=0.01, qty=1.5, risk_amount=3.0)
    save_open_positions(p.positions)

    p2 = PaperPortfolio(1000.0)
    p2.restore_positions(load_open_positions())
    pos = p2.positions["TESTUSDT"]
    # 1R, GERÇEKLEŞEN girişe göre ölçülür (kayma dahil) — 100.0'a göre değil.
    acts = evaluate(pos, pos["entry_price"] + pos["risk_per_unit"])
    assert any(a["reason"] == EXIT_TP1 for a in acts)


def test_position_persistence_is_a_full_sync_not_append():
    p = PaperPortfolio(1000.0)
    p.open_position("AAAUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0, qty=1.0)
    p.open_position("BBBUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0, qty=1.0)
    save_open_positions(p.positions)
    assert len(load_open_positions()) == 2

    p.positions.pop("AAAUSDT")
    save_open_positions(p.positions)
    loaded = load_open_positions()
    assert len(loaded) == 1 and "BBBUSDT" in loaded


def test_corrupt_position_payload_is_skipped_not_fatal():
    import sqlite3
    with sqlite3.connect(str(database.DB_PATH)) as con:
        con.execute(
            "INSERT INTO open_positions (symbol, payload, updated_at) VALUES (?,?,?)",
            ("BADUSDT", "{bozuk json", datetime.now().isoformat()),
        )
    loaded = load_open_positions()
    assert "BADUSDT" not in loaded    # atlandı, çökmedi


# ── Migration ─────────────────────────────────────────────────────────────────

def test_migration_adds_columns_without_losing_data():
    """
    Mevcut canlı trades.db 82 gerçek işlem içeriyor. Migration onları
    bozmamalı — eski şemayla yazılmış satırlar korunarak yeni kolonlar
    eklenmeli.
    """
    import sqlite3
    # Eski şemayı taklit et: yeni kolonları düşür
    with sqlite3.connect(str(database.DB_PATH)) as con:
        con.execute("DROP TABLE trades")
        con.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                entry_price REAL, close_price REAL, margin REAL, leverage INTEGER,
                gross_pnl REAL, commission REAL, net_pnl REAL,
                reason_open TEXT, reason_close TEXT, open_time TEXT, close_time TEXT,
                duration_min REAL, score INTEGER, btc_trend TEXT,
                market_regime TEXT, atr_pct REAL, fear_greed INTEGER
            )
        """)
        con.execute(
            "INSERT INTO trades (symbol, side, net_pnl, gross_pnl, commission) "
            "VALUES ('OLDUSDT','BUY', 5.0, 5.5, 0.5)"
        )

    database.init_db()   # migration çalışır

    with sqlite3.connect(str(database.DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        cols = {r["name"] for r in con.execute("PRAGMA table_info(trades)")}
        assert "strategy" in cols and "r_multiple" in cols and "mfe_r" in cols
        row = con.execute("SELECT * FROM trades WHERE symbol='OLDUSDT'").fetchone()
        assert row is not None, "migration eski veriyi sildi!"
        assert row["net_pnl"] == 5.0
        assert row["strategy"] == "TREND"   # varsayılan


def test_migration_is_idempotent():
    database.init_db()
    database.init_db()
    database.init_db()   # tekrar tekrar çalıştırmak hata vermemeli


# ── Raporlama ─────────────────────────────────────────────────────────────────

def _save(symbol, net, strategy="TREND", r=0.0):
    save_trade(
        symbol=symbol, side="BUY", entry_price=100, close_price=101,
        margin=50, leverage=3, gross_pnl=net + 0.3, commission=0.3, net_pnl=net,
        reason_open="test", reason_close="TRAIL_STOP",
        open_time=datetime.now() - timedelta(minutes=30), close_time=datetime.now(),
        strategy=strategy, r_multiple=r,
    )


def test_strategy_attribution_in_reports():
    _save("A", 10.0, "TREND")
    _save("B", -5.0, "TREND")
    _save("C", 50.0, "MOMENTUM_FAST")

    trend = get_performance_summary("TREND")
    mom   = get_performance_summary("MOMENTUM_FAST")
    assert trend["total"] == 2 and mom["total"] == 1
    assert mom["net_pnl"] == pytest.approx(50.0)


def test_summary_excluding_top_reveals_outlier_dependence():
    """
    Gerçek paraya geçiş kapısı: kâr birkaç şanslı işleme mi bağlı?
    (2026-08-12 ölçümü: 82 işlemin kârının %117'si 2 işlemden geliyordu.)
    """
    for i in range(8):
        _save(f"L{i}", -5.0)
    _save("BIG1", 100.0)
    _save("BIG2", 80.0)

    full = get_performance_summary()
    ex   = get_summary_excluding_top(2)
    assert full["net_pnl"] > 0        # görünüşte kârlı
    assert ex["net_pnl"] < 0          # ama outlier'sız zararda
    assert ex["excluded"] == 2


def test_recent_pnls_survive_restart():
    """Dinamik risk artık bellekteki trade_log'a değil DB'ye bakar."""
    for i in range(5):
        _save(f"T{i}", float(i))
    pnls = get_recent_pnls(10)
    assert len(pnls) == 5
    assert pnls[-1] == 4.0    # en yeni sonda
