"""
Risk yönetimi testleri.

Odak: 2026-08-12 ölçümünde bulunan iki somut hatanın bir daha oluşamaması.
  1. Teminat tavanı risk hedefini eziyordu → gerçek dolar riski kaldıraçla
     birlikte büyüyordu (5x işlemler 2x'in 2-3 katı risk taşıyordu).
  2. Günlük durdurma sabit işlem sayısına bakıyordu → kaçan sinyallerin %50'si.
"""

from datetime import datetime, timedelta

import pytest

import config
from risk_manager import (
    calculate_position, calculate_leverage, KillSwitchTracker,
    portfolio_open_risk_r, can_add_position, dynamic_risk_pct, safe_stop_loss,
)


EQUITY = 1000.0


# ── Kaldıraçtan bağımsız risk ─────────────────────────────────────────────────

@pytest.mark.parametrize("leverage", [2, 3])
def test_dollar_risk_is_identical_across_leverage(leverage):
    """
    Aynı stop mesafesinde, kaldıraç ne olursa olsun riske edilen dolar AYNI
    olmalı. Eski kodun temel hatası tam buradaydı.
    """
    entry, sl = 100.0, 98.0     # %2 stop
    margin, qty, risk_amount, err = calculate_position(
        EQUITY, leverage, entry, sl, 0.0075
    )
    assert err == ""
    assert risk_amount == pytest.approx(EQUITY * 0.0075, rel=1e-6)
    # Stop'a çarpınca kaybedilecek gerçek dolar
    actual_loss = abs(entry - sl) * qty
    assert actual_loss == pytest.approx(EQUITY * 0.0075, rel=1e-6)


def test_notional_is_leverage_independent():
    """
    Risk-bazlı boyutlandırmada notional kaldıraçtan bağımsızdır — dolayısıyla
    büyük bir harekette kazanılacak dolar da kaldıraçtan bağımsızdır.
    Kaldıraç yalnızca bloke edilen teminatı değiştirir.
    """
    entry, sl = 100.0, 97.0
    _, qty2, _, _ = calculate_position(EQUITY, 2, entry, sl, 0.0075)
    _, qty3, _, _ = calculate_position(EQUITY, 3, entry, sl, 0.0075)
    assert qty2 == pytest.approx(qty3, rel=1e-9)

    m2, _, _, _ = calculate_position(EQUITY, 2, entry, sl, 0.0075)
    m3, _, _, _ = calculate_position(EQUITY, 3, entry, sl, 0.0075)
    assert m3 < m2   # yüksek kaldıraç = daha az teminat


def test_position_rejected_not_shrunk_when_margin_cap_exceeded():
    """
    Teminat tavanı aşılırsa pozisyon KÜÇÜLTÜLMEZ, REDDEDİLİR.
    Küçültmek riski hedeften saptırır ve ölçümü bozar.
    """
    entry, sl = 100.0, 99.9     # çok yakın stop → devasa notional
    margin, qty, risk, err = calculate_position(EQUITY, 3, entry, sl, 0.0075)
    assert err != ""
    assert margin == 0.0 and qty == 0.0


def test_too_wide_stop_rejected():
    entry, sl = 100.0, 90.0     # %10 stop, MAX_SL_PCT %4
    _, _, _, err = calculate_position(EQUITY, 3, entry, sl, 0.0075)
    assert "SL çok uzak" in err


def test_total_margin_cap_enforced():
    entry, sl = 100.0, 97.0
    already_open = EQUITY * config.MAX_TOTAL_MARGIN_PCT
    _, _, _, err = calculate_position(
        EQUITY, 3, entry, sl, 0.0075, open_margin_total=already_open
    )
    assert "Toplam teminat" in err


def test_leverage_is_capped_and_decoupled_from_confidence():
    """
    Kaldıraç artık güven skoruna bağlı değil. Eski davranış confidence>=0.80'de
    5x veriyordu; ölçüm o dilimin net zararda olduğunu gösterdi.
    """
    assert calculate_leverage(0.60) == calculate_leverage(0.99)
    assert calculate_leverage(0.99, score=100) <= config.LEVERAGE_MAX


# ── Risk bütçesi ──────────────────────────────────────────────────────────────

def test_risk_budget_replaces_fixed_trade_count():
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    # Bütçe dolana kadar işlem açılabilmeli
    n = 0
    while ks.can_trade(EQUITY)[0] and n < 100:
        ks.record_trade_open(EQUITY, risk_r=1.0)
        n += 1
    assert n == int(config.DAILY_RISK_BUDGET_R)
    ok, reason = ks.can_trade(EQUITY)
    assert not ok and "risk bütçesi" in reason.lower()


def test_winning_trade_refunds_budget_so_good_days_allow_more_trades():
    """
    Kullanıcının asıl şikayeti: "10 işlemden sonra çok kârlı işlemler olabilir
    ve bunları kaçırırız." Kazanan işlem bütçeyi geri besler.
    """
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    for _ in range(int(config.DAILY_RISK_BUDGET_R)):
        ks.record_trade_open(EQUITY, risk_r=1.0)
    assert not ks.can_trade(EQUITY)[0]

    ks.record_trade_close(50.0, EQUITY + 50, realized_r=2.5, symbol="AAAUSDT")
    assert ks.can_trade(EQUITY + 50)[0], "kârlı işlem sonrası bütçe açılmalı"


def test_refund_is_capped_at_one_r():
    """Tek bir büyük kazanç bütçeyi sınırsız açmamalı."""
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    ks.record_trade_open(EQUITY, risk_r=1.0)
    ks.record_trade_open(EQUITY, risk_r=1.0)
    before = ks.daily_risk_used_r
    ks.record_trade_close(500.0, EQUITY, realized_r=50.0, symbol="X")
    assert ks.daily_risk_used_r == pytest.approx(before - 1.0)


def test_losing_trade_does_not_refund():
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    ks.record_trade_open(EQUITY, risk_r=1.0)
    used = ks.daily_risk_used_r
    ks.record_trade_close(-10.0, EQUITY - 10, realized_r=-1.0, symbol="X")
    assert ks.daily_risk_used_r == used


def test_hard_cap_still_exists_as_runaway_protection():
    """
    Limit tamamen kaldırılmadı: bütçe sürekli geri beslense bile mutlak bir
    güvenlik tavanı var (kod hatası / veri anomalisi koruması).
    """
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    for _ in range(config.MAX_DAILY_TRADES_HARD):
        ks.record_trade_open(EQUITY, risk_r=1.0)
        ks.daily_risk_used_r = 0.0     # bütçeyi yapay olarak boşalt
    ok, reason = ks.can_trade(EQUITY)
    assert not ok and "Güvenlik tavanı" in reason


# ── Kill switch korumaları ────────────────────────────────────────────────────

def test_daily_loss_limit_halts_trading():
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    ks.record_trade_close(-(EQUITY * config.DAILY_LOSS_LIMIT_PCT + 1), EQUITY, symbol="X")
    ok, reason = ks.can_trade(EQUITY)
    assert not ok
    assert "zarar limiti" in reason or "Üst üste" in reason


def test_drawdown_limit_halts_trading():
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    ks.high_water_mark = 2000.0
    ok, reason = ks.can_trade(1000.0)   # %50 drawdown
    assert not ok and "drawdown" in reason.lower()


def test_consecutive_losses_halt_then_cooldown_releases():
    ks = KillSwitchTracker(EQUITY)
    ks.initialize(EQUITY)
    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        ks.record_trade_close(-5.0, EQUITY, symbol="X")
    assert not ks.can_trade(EQUITY)[0]
    # Soğuma dolunca serbest bırakmalı (aksi hâlde kill switch KALICI olur)
    ks._halted_at = datetime.now() - timedelta(hours=config.KILL_SWITCH_COOLDOWN_HOURS + 1)
    assert ks.can_trade(EQUITY)[0]


def test_symbol_cooldown():
    ks = KillSwitchTracker(EQUITY)
    ks.record_trade_close(-1.0, EQUITY, symbol="FOOUSDT")
    assert ks.symbol_on_cooldown("FOOUSDT")
    assert not ks.symbol_on_cooldown("BARUSDT")


# ── Portföy seviyesi risk ─────────────────────────────────────────────────────

def _pos(entry, stop, qty, risk_amount, side="BUY"):
    return {"entry_price": entry, "stop_loss": stop, "qty": qty,
            "risk_amount": risk_amount, "initial_qty": qty, "side": side}


def test_portfolio_open_risk_counts_only_real_risk():
    positions = {
        "A": _pos(100, 98, 10, 20.0),    # tam riskte → 1R
        "B": _pos(100, 100, 10, 20.0),   # breakeven → 0R
    }
    assert portfolio_open_risk_r(positions) == pytest.approx(1.0)


def test_portfolio_risk_limit_blocks_new_positions():
    positions = {
        str(i): _pos(100, 98, 10, 20.0)
        for i in range(int(config.MAX_PORTFOLIO_RISK_R))
    }
    ok, reason = can_add_position(positions, EQUITY)
    assert not ok and "risk tavanı" in reason


def test_max_open_positions_enforced():
    positions = {str(i): _pos(100, 100, 1, 20.0) for i in range(config.MAX_OPEN_POSITIONS)}
    ok, reason = can_add_position(positions, EQUITY)
    assert not ok and "Max pozisyon" in reason


# ── Dinamik risk ──────────────────────────────────────────────────────────────

def test_dynamic_risk_stays_within_configured_bounds():
    for pnls in ([10] * 20, [-10] * 20, [5, -3, 8, -2, 6, -1, 9, -4]):
        r = dynamic_risk_pct(list(pnls), score=50)
        assert config.RISK_MIN_PCT <= r <= config.RISK_MAX_PCT


def test_strong_signal_gets_higher_base_risk_but_capped_at_one_pct():
    normal = dynamic_risk_pct([], score=50)
    strong = dynamic_risk_pct([], score=config.STRONG_SIGNAL_SCORE + 5)
    assert strong > normal
    assert strong <= 0.01 + 1e-9


# ── Likidasyon güvenliği ──────────────────────────────────────────────────────

def test_safe_stop_is_always_before_liquidation():
    for lev in (2, 3):
        entry = 100.0
        sl = safe_stop_loss(entry, "BUY", lev, atr_sl=50.0)   # aşırı geniş ATR stop
        liq = entry * (1 - 1.0 / lev)
        assert sl > liq, "stop likidasyon fiyatının altında kaldı"
