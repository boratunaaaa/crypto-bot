"""
Korelasyon / stres limiti testleri.

Kapatılan boşluk: `MAX_CORRELATED_POSITIONS` ve `MAX_PORTFOLIO_RISK_R` sayı
sayıyordu, birlikte hareketi görmüyordu. 4 ayrı altcoin long'u kayıtta "dört
bağımsız 1R bahsi" görünüyor; BTC %5 düşerse dördü birden stop oluyor. Yani
gerçekte tek bir 4R'lik BTC bahsi ve hiçbir kapı bunu ölçmüyordu.
"""

import pytest

from correlation import (
    beta_to, can_add_correlated, correlation, net_directional_r,
    returns, stress_loss_r,
)


def _pos(side="BUY", entry=100.0, stop=98.0, qty=1.0, risk=2.0):
    return {"symbol": "X", "side": side, "entry_price": entry,
            "stop_loss": stop, "qty": qty, "risk_amount": risk}


# ── Temel istatistik ──────────────────────────────────────────────────────────

def test_correlation_of_identical_series_is_one():
    a = returns([100, 101, 99, 103, 102, 105, 104, 108, 107, 110,
                 112, 111, 115, 114, 118, 117, 121, 120, 124, 123, 127])
    assert correlation(a, a) == pytest.approx(1.0)


def test_correlation_of_opposite_series_is_negative_one():
    a = returns([100 + i * (1 if i % 2 else -2) for i in range(30)])
    b = [-x for x in a]
    assert correlation(a, b) == pytest.approx(-1.0)


def test_insufficient_sample_assumes_worst_case():
    """
    Bilinmiyorsa 0 (korelasyon yok) DEĞİL, 1.0 dönmeli. "Bilmiyorum"u
    "risk yok" saymak, tam da korunmak istediğimiz riski görünmez kılar.
    """
    assert correlation([0.01, 0.02], [0.01, 0.02]) == 1.0
    assert beta_to([0.01, 0.02], [0.01, 0.02]) == 1.0


def test_beta_detects_amplified_asset():
    """Altcoin BTC'nin 2 katı hareket ediyorsa beta ≈ 2."""
    btc = returns([100 * (1.01 ** i) if i % 3 else 100 * (0.99 ** i) for i in range(40)])
    alt = [r * 2 for r in btc]
    assert beta_to(alt, btc) == pytest.approx(2.0, rel=0.01)


# ── Net yönlü maruziyet ───────────────────────────────────────────────────────

def test_four_longs_count_as_four_r_not_four_independent_bets():
    positions = {f"ALT{i}": _pos("BUY") for i in range(4)}
    assert net_directional_r(positions) == pytest.approx(4.0)


def test_opposing_positions_offset_each_other():
    """Gerçekten hedge eden pozisyonlar birbirini götürmeli — sayı saymak bunu yapamaz."""
    positions = {"A": _pos("BUY"), "B": _pos("SELL", entry=100.0, stop=102.0)}
    assert net_directional_r(positions) == pytest.approx(0.0)


def test_high_beta_positions_count_more():
    """Beta 2 olan bir altcoin, 1R nominal riskle 2R maruziyet taşır."""
    positions = {"ALT": _pos("BUY")}
    assert net_directional_r(positions, {"ALT": 2.0}) == pytest.approx(2.0)


def test_breakeven_position_carries_no_directional_risk():
    """Stop girişin üstüne taşınmışsa maruziyet sıfırdır."""
    positions = {"A": _pos("BUY", entry=100.0, stop=101.0)}
    assert net_directional_r(positions) == 0.0


# ── Stres testi ───────────────────────────────────────────────────────────────

def test_btc_shock_wipes_out_correlated_longs():
    """
    ASIL SENARYO: 4 alt long, her biri %2 stop mesafesinde. BTC %5 düşerse
    dördü de stop olur — nominal 4R risk gerçekten 4R zarara dönüşür.
    """
    positions = {f"ALT{i}": _pos("BUY", stop=98.0) for i in range(4)}
    loss = stress_loss_r(positions, shock_pct=-0.05)
    assert loss == pytest.approx(4.0), f"beklenen 4R, ölçülen {loss}R"


def test_wide_stops_survive_small_shock():
    """Stop mesafesi şoktan genişse pozisyon oransal zarar eder, tamamen değil."""
    # 1R = (giriş - stop) × qty olacak şekilde tutarlı kurulum
    positions = {"A": _pos("BUY", entry=100.0, stop=90.0, qty=1.0, risk=10.0)}
    loss = stress_loss_r(positions, shock_pct=-0.05)          # %5 şok, %10 stop
    assert loss == pytest.approx(0.5, rel=0.01)


def test_hedged_book_is_credited_only_half():
    """
    Kriz anında korelasyonlar 1'e koşar ve hedge'ler beklendiği gibi çalışmaz.
    Hedge kazancı yarım ağırlıkla sayılır — muhafazakâr olan bu.
    """
    positions = {"A": _pos("BUY", stop=98.0),
                 "B": _pos("SELL", entry=100.0, stop=102.0)}
    loss = stress_loss_r(positions, shock_pct=-0.05)
    assert loss == pytest.approx(0.5), "hedge'e tam güvenilmiş"


# ── Kapı ──────────────────────────────────────────────────────────────────────

def test_guard_blocks_fifth_correlated_long():
    positions = {f"ALT{i}": _pos("BUY") for i in range(3)}
    ok, reason = can_add_correlated(
        positions, "ALT9", "BUY", 1.0, {},
        max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05,
    )
    assert not ok
    assert "maruziyet" in reason or "Stres" in reason


def test_guard_allows_opposing_position():
    """Ters yönde pozisyon maruziyeti AZALTIR — engellenmemeli."""
    positions = {f"ALT{i}": _pos("BUY") for i in range(3)}
    ok, _ = can_add_correlated(
        positions, "ALT9", "SELL", 1.0, {},
        max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05,
    )
    assert ok


def test_guard_allows_first_position():
    ok, _ = can_add_correlated(
        {}, "ALT1", "BUY", 1.0, {},
        max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05,
    )
    assert ok


def test_guard_is_stricter_for_high_beta_symbols():
    """Aynı sayıda pozisyon, yüksek beta → daha erken durur."""
    positions = {"A": _pos("BUY"), "B": _pos("BUY")}
    ok_low, _ = can_add_correlated(
        positions, "C", "BUY", 1.0, {"A": 0.5, "B": 0.5, "C": 0.5},
        max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05,
    )
    ok_high, _ = can_add_correlated(
        positions, "C", "BUY", 1.0, {"A": 2.0, "B": 2.0, "C": 2.0},
        max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05,
    )
    assert ok_low and not ok_high


def test_correlation_gate_respects_actual_risk_size():
    """
    DENETİM: kapı her adayı matematiksel olarak 1R sayıyordu; %0.4 riskli
    işlemle %1.2 riskli işlem aynı maruziyeti üretiyordu. Risk birimi tek
    olmalı — aksi hâlde portföy tavanı da bütçe iadesi de yanlış hesaplanır.
    """
    positions = {f"ALT{i}": _pos("BUY") for i in range(2)}
    small, _ = can_add_correlated(positions, "C", "BUY", 0.4, {},
                                  max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05)
    big, _ = can_add_correlated(positions, "C", "BUY", 1.6, {},
                                max_net_r=3.0, max_stress_r=3.0, shock_pct=0.05)
    assert small and not big, "küçük ve büyük riskli aday aynı muamele gördü"
