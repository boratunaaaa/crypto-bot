"""
MOMENTUM_FAST testleri — sinyal üretimi, overextension koruması, bozuk veri.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import config
import momentum as mom


def make_df(closes, volumes=None, taker_ratio=0.65, trades_per_bar=100,
            wick=0.35):
    """
    1m mum çerçevesi üret.

    Mum gövdesi kadar fitil de üretilir (`wick` oranında). Bu önemli: gerçek
    piyasada hareketli mumların aralığı da genişler, dolayısıyla ATR büyür.
    Sabit dar fitil kullanan sentetik veri ATR'yi yapay olarak küçültür ve
    ATR'ye bağlı filtreleri (overextension) yanlış tetikler.
    """
    n = len(closes)
    volumes = volumes if volumes is not None else [1000.0] * n
    idx = pd.date_range(datetime(2026, 8, 12, 10, 0), periods=n, freq="1min")
    closes = np.array(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])

    body = np.abs(closes - opens)
    pad = np.maximum(body * wick, closes * 0.0008)
    highs = np.maximum(opens, closes) + pad
    lows  = np.minimum(opens, closes) - pad

    qv = np.array(volumes, dtype=float)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": qv, "quote_volume": qv,
        "trades": [trades_per_bar] * n,
        "taker_buy_quote": qv * taker_ratio,
        "taker_buy_base": qv * taker_ratio,
    }, index=idx)


def flat_then_pump(flat_n=40, pump_n=5, base=100.0, pump_pct=0.03):
    """Yatay seyir sonrası hacimli yükseliş."""
    closes = [base] * flat_n
    for i in range(pump_n):
        closes.append(closes[-1] * (1 + pump_pct))
    vols = [1000.0] * flat_n + [8000.0] * pump_n
    return make_df(closes, vols)


# ── Özellik çıkarımı ──────────────────────────────────────────────────────────

def test_extract_features_basic():
    df = flat_then_pump()
    f = mom.extract_features(df)
    assert f["ret_1m"] > 0
    assert f["ret_5m"] > f["ret_1m"]
    assert f["rel_volume"] > 3.0
    assert f["taker_buy_ratio"] == pytest.approx(0.65, abs=0.02)
    assert f["atr_pct"] > 0


def test_features_handle_short_dataframe():
    df = make_df([100.0] * 5)
    f = mom.extract_features(df)
    assert f["rel_volume"] == 0.0      # taban yok → nötr
    assert "price" in f


def test_features_handle_zero_volume():
    df = make_df([100, 101, 102] * 10, volumes=[0.0] * 30)
    f = mom.extract_features(df)
    assert f["rel_volume"] == 0.0
    assert f["taker_buy_ratio"] == 0.5   # bölme hatası yok, nötr döner


def test_features_handle_nan_prices():
    df = flat_then_pump()
    df.loc[df.index[10], "close"] = np.nan
    f = mom.extract_features(df)       # çökmemeli
    assert isinstance(f, dict)


# ── Sinyal üretimi ────────────────────────────────────────────────────────────

def test_strong_pump_generates_buy_signal():
    df = flat_then_pump(pump_pct=0.012)
    sig = mom.analyze("PUMPUSDT", df, oi_change=0.03, spread=0.001)
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.score >= config.MOMENTUM_MIN_SCORE
    assert sig.strategy == "MOMENTUM_FAST"
    assert sig.sl < sig.price


def test_flat_market_produces_no_signal():
    df = make_df([100.0] * 45)
    assert mom.analyze("FLATUSDT", df) is None


def test_insufficient_data_returns_none():
    assert mom.analyze("XUSDT", make_df([100, 101])) is None
    assert mom.analyze("XUSDT", None) is None


def test_short_signal_on_dump():
    closes = [100.0] * 40
    for _ in range(5):
        closes.append(closes[-1] * 0.985)
    df = make_df(closes, [1000.0] * 40 + [9000.0] * 5, taker_ratio=0.30)
    sig = mom.analyze("DUMPUSDT", df, oi_change=0.03, spread=0.001)
    assert sig is not None and sig.action == "SELL"
    assert sig.sl > sig.price


# ── Overextension / chase koruması ────────────────────────────────────────────

def test_vertical_move_without_pullback_is_rejected():
    """Dev yeşil mumun tepesinden kör alım yapılmamalı."""
    closes = [100.0] * 40
    for _ in range(5):
        closes.append(closes[-1] * 1.06)     # 5 dakikada ~%34
    df = make_df(closes, [1000.0] * 40 + [20000.0] * 5)
    sig = mom.analyze("VERTUSDT", df, oi_change=0.05, spread=0.001)
    assert sig is not None
    assert sig.action == "HOLD"
    assert sig.reject_reason


def test_wide_spread_rejected():
    df = flat_then_pump(pump_pct=0.012)
    sig = mom.analyze("WIDEUSDT", df, oi_change=0.03,
                      spread=config.MAX_SPREAD_PCT * 3)
    assert sig.action == "HOLD" and "Spread" in sig.reject_reason


def test_unknown_spread_does_not_block():
    """Spread bilinmiyorsa sinyal reddedilmemeli (veri eksikliği ≠ kötü koşul)."""
    df = flat_then_pump(pump_pct=0.012)
    sig = mom.analyze("XUSDT", df, oi_change=0.03, spread=None)
    assert sig.action == "BUY"


def test_btc_crash_vetoes_momentum():
    df = flat_then_pump(pump_pct=0.012)
    sig = mom.analyze("XUSDT", df, oi_change=0.03, spread=0.001,
                      btc_change_15m=config.MOM_BTC_CRASH_PCT - 0.01)
    assert sig.action == "HOLD" and "BTC" in sig.reject_reason


def test_btc_sideways_does_not_veto_strong_altcoin():
    """
    Bağımsız güçlü altcoin momentumu BTC yatay diye reddedilmemeli —
    bu, TREND stratejisinden kasıtlı farkı.
    """
    df = flat_then_pump(pump_pct=0.012)
    sig = mom.analyze("XUSDT", df, oi_change=0.03, spread=0.001,
                      btc_change_15m=-0.003)
    assert sig.action == "BUY"


# ── Stop hesabı ───────────────────────────────────────────────────────────────

def test_stop_distance_within_configured_bounds():
    df = flat_then_pump(pump_pct=0.012)
    f = mom.extract_features(df)
    for direction in ("BUY", "SELL"):
        sl = mom.compute_stop(f, direction)
        dist_pct = abs(f["price"] - sl) / f["price"]
        assert mom._MIN_STOP_PCT - 1e-9 <= dist_pct <= config.MAX_SL_PCT + 1e-9


def test_stop_is_never_at_entry():
    df = flat_then_pump(pump_pct=0.012)
    f = mom.extract_features(df)
    assert mom.compute_stop(f, "BUY") != f["price"]


# ── Skorlama ──────────────────────────────────────────────────────────────────

def test_score_is_bounded():
    df = flat_then_pump(pump_pct=0.05)
    f = mom.extract_features(df, oi_change=0.5)
    score, _ = mom.score_momentum(f, "BUY")
    assert 0 <= score <= 100


def test_more_volume_scores_higher():
    weak = mom.extract_features(flat_then_pump(pump_pct=0.012))
    strong = dict(weak)
    strong["rel_volume"] = weak["rel_volume"] * 3
    s_weak, _ = mom.score_momentum(weak, "BUY")
    s_strong, _ = mom.score_momentum(strong, "BUY")
    assert s_strong >= s_weak


# ── Tarama ────────────────────────────────────────────────────────────────────

class FakeMDM:
    def __init__(self, data):  self.data = data
    def get_fast_df(self, sym): return self.data.get(sym)
    def get_oi_change_pct(self, sym): return 0.03
    def get_spread_pct(self, sym): return 0.001


def test_scan_skips_stale_symbols():
    """Bayat veri (None) sessizce atlanmalı, çökmemeli."""
    mdm = FakeMDM({"AAAUSDT": flat_then_pump(pump_pct=0.012), "BBBUSDT": None})
    out = mom.scan(mdm, ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    assert [s.symbol for s in out if s.action == "BUY"] == ["AAAUSDT"]


def test_scan_skips_btc():
    mdm = FakeMDM({"BTCUSDT": flat_then_pump(pump_pct=0.012)})
    assert mom.scan(mdm, ["BTCUSDT"]) == []


def test_scan_survives_malformed_dataframe():
    bad = pd.DataFrame({"close": [1, 2, 3]})        # eksik kolonlar
    mdm = FakeMDM({"BADUSDT": bad})
    out = mom.scan(mdm, ["BADUSDT"])                 # çökmemeli
    assert out == []


def test_scan_sorts_by_score_descending():
    mdm = FakeMDM({
        "AAAUSDT": flat_then_pump(pump_pct=0.012),
        "BBBUSDT": flat_then_pump(pump_pct=0.020),
    })
    out = [s for s in mom.scan(mdm, ["AAAUSDT", "BBBUSDT"]) if s.action != "HOLD"]
    scores = [s.score for s in out]
    assert scores == sorted(scores, reverse=True)


# ── Gölge mod ─────────────────────────────────────────────────────────────────

def test_golgeden_cikan_strateji_KENDI_sermayesiyle_calisir():
    """Eski hâli "gölge mod açık olmalı" diyordu; gerekçesi şuydu: bu strateji
    hiç ölçülmedi, ölçülmeden ORTAK kasayı riske atmasın.

    2026-08-18'de gölge moddan çıkarıldı — ama gerekçe ortadan kalkmadı,
    KARŞILANDI: artık kendi sanal sermayesiyle çalışıyor, TREND'in kasasına
    dokunmuyor. Test o yüzden silinmedi, değişmezi güncellendi.

    Korunan kural: MOMENTUM işlem açıyorsa AYRI ve pozitif bir sermayesi
    olmalı. Aksi hâlde ölçülmemiş bir strateji ölçülmüş olanın parasını harcar
    ve iki stratejinin sonucu birbirine karışır.
    """
    if config.MOMENTUM_SHADOW_MODE:
        return  # gölgedeyse işlem açmıyor, sermaye sorusu doğmuyor

    assert hasattr(config, "MOMENTUM_BALANCE"), (
        "MOMENTUM gölgeden çıkarıldı ama kendi sermayesi tanımlanmamış — "
        "ortak kasadan harcar"
    )
    assert config.MOMENTUM_BALANCE > 0
    assert config.MOMENTUM_BALANCE == config.PAPER_BALANCE, (
        "karşılaştırma ancak eşit başlangıç sermayesiyle anlamlı"
    )


def test_shadow_signal_persists():
    from database import save_shadow_signal, get_pending_shadow_signals
    rid = save_shadow_signal("XUSDT", "BUY", 85, 1.234, {"rel_volume": 5.2})
    assert rid is not None
    pending = get_pending_shadow_signals()
    assert any(p["symbol"] == "XUSDT" and p["outcome"] == "PENDING" for p in pending)
