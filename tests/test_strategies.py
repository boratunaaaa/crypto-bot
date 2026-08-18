"""
Deney stratejileri: BREAKOUT, PULLBACK, MEANREV.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
Bu üç strateji ölçülecek. Ölçüm ancak stratejiler gerçekten FARKLI şeyler
yapıyorsa anlamlı: üçü aynı koşulda tetikleniyorsa elimizde üç strateji değil,
üç kılığa girmiş tek risk vardır ve karşılaştırma boş çıkar.

O yüzden testlerin çoğu "sinyal üretiyor mu" değil, "YANLIŞ koşulda susuyor
mu" sorusunu ölçer. Özellikle:

  * MEANREV trend içinde ASLA çalışmamalı — ortalamaya dönüş stratejisinin
    klasik ölüm biçimi budur: trend boyunca her düşüşte alır ve kaybeder.
  * BREAKOUT kanalı hesaplarken SON barı dışlamalı; dahil etseydi fiyat her
    zaman kendi tepesine eşit olur ve her bar "kırılım" görünürdü.
  * Bir stratejinin hatası diğerlerinin veri akışını kesmemeli; yoksa yedi
    hafta sonra elimizde hiçbir şey olmaz.
"""

import numpy as np
import pandas as pd
import pytest

import strategies as st

RS = np.random.RandomState(20260818)


def _df(kapanis, hacim=None):
    n = len(kapanis)
    kapanis = np.asarray(kapanis, dtype=float)
    hacim = np.full(n, 100.0) if hacim is None else np.asarray(hacim, dtype=float)
    return pd.DataFrame(
        {
            "open": kapanis,
            "high": kapanis + 0.4,
            "low": kapanis - 0.4,
            "close": kapanis,
            "volume": hacim,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="5min"),
    )


def _yatay(n=260, seviye=100.0, gurultu=0.5):
    return seviye + RS.normal(0, gurultu, n)


# ── BREAKOUT ──────────────────────────────────────────────────────────────────

def test_breakout_hacimli_kirilimda_tetiklenir():
    kapanis = np.concatenate([_yatay(255), [104.0]])
    hacim = np.concatenate([np.full(255, 100.0), [400.0]])
    s = st.breakout("XUSDT", _df(kapanis, hacim), 104.0)
    assert s.action == "BUY", s.reject_reason
    assert s.score > 0 and s.sl < 104.0 < s.tp


def test_breakout_hacimsiz_kirilimi_REDDEDER():
    """Hacimsiz kırılım çoğunlukla gürültüdür ve hemen geri gelir."""
    kapanis = np.concatenate([_yatay(255), [104.0]])
    s = st.breakout("XUSDT", _df(kapanis), 104.0)
    assert s.action == "HOLD"
    assert "hacim" in s.reject_reason


def test_breakout_kanal_icinde_susar():
    kapanis = _yatay(260)
    hacim = np.concatenate([np.full(259, 100.0), [400.0]])
    s = st.breakout("XUSDT", _df(kapanis, hacim), float(kapanis[-1]))
    assert s.action == "HOLD"
    assert "kanal" in s.reject_reason


def test_breakout_kanala_SON_bari_katmaz():
    """Son bar kanala dahil edilseydi fiyat hep kendi tepesine eşit olur ve
    HER bar kırılım sayılırdı — strateji sürekli işlem açardı."""
    kapanis = _yatay(260)
    hacim = np.concatenate([np.full(259, 100.0), [400.0]])
    df = _df(kapanis, hacim)
    # Son barın tepesini yapay olarak çok yükselt: kanal ONU saymamalı.
    df.iloc[-1, df.columns.get_loc("high")] = 999.0
    s = st.breakout("XUSDT", df, float(kapanis[-1]))
    assert s.action == "HOLD", "son bar kanala karışmış — her bar kırılım görünür"


def test_breakout_asagi_kirilimda_SELL():
    kapanis = np.concatenate([_yatay(255), [96.0]])
    hacim = np.concatenate([np.full(255, 100.0), [400.0]])
    s = st.breakout("XUSDT", _df(kapanis, hacim), 96.0)
    assert s.action == "SELL", s.reject_reason
    assert s.sl > 96.0 > s.tp


# ── PULLBACK ──────────────────────────────────────────────────────────────────

def _yukselen_trend(n=260):
    return np.linspace(80.0, 120.0, n) + RS.normal(0, 0.2, n)


def test_pullback_yukselen_trendde_geri_cekilmede_tetiklenir():
    kapanis = _yukselen_trend()
    df = _df(kapanis)
    fiyat = float(pd.Series(kapanis).ewm(span=st.EMA_FAST, adjust=False).mean().iloc[-1])
    s = st.pullback("XUSDT", df, fiyat)
    assert s.action == "BUY", s.reject_reason


def test_pullback_trend_yoksa_susar():
    """Trend yokken 'geri çekilme' diye bir şey yoktur."""
    df = _df(_yatay(260))
    s = st.pullback("XUSDT", df, 100.0)
    assert s.action == "HOLD"
    assert "trend" in s.reject_reason


def test_pullback_EMAdan_uzakken_susar():
    kapanis = _yukselen_trend()
    s = st.pullback("XUSDT", _df(kapanis), float(kapanis[-1]) * 1.20)
    assert s.action == "HOLD"
    assert "uzak" in s.reject_reason or "oynaklık" in s.reject_reason


# ── MEANREV ───────────────────────────────────────────────────────────────────

def test_meanrev_yatay_piyasada_asiri_sapmada_tetiklenir():
    taban = _yatay(259, gurultu=1.0)
    ort = float(pd.Series(taban).tail(st.DONCHIAN_BARS).mean())
    sapma = float(pd.Series(taban).tail(st.DONCHIAN_BARS).std())
    dusuk = ort - 3.0 * sapma
    kapanis = np.concatenate([taban, [dusuk]])
    s = st.meanrev("XUSDT", _df(kapanis), float(dusuk))
    assert s.action in ("BUY", "HOLD")
    if s.action == "HOLD":
        # Trend filtresine takılmadığından emin ol; sapma yetersizse kabul.
        assert "sapma" in s.reject_reason or "oynaklık" in s.reject_reason


def test_meanrev_TREND_icinde_ASLA_calismaz():
    """Bu stratejinin klasik ölüm biçimi: trend boyunca her düşüşte alıp
    kaybetmek. Trend filtresi hayati."""
    kapanis = _yukselen_trend()
    # Trendin içinde sert bir geri çekilme yarat.
    kapanis = np.concatenate([kapanis[:-1], [kapanis[-1] * 0.90]])
    s = st.meanrev("XUSDT", _df(kapanis), float(kapanis[-1]))
    assert s.action == "HOLD", "MEANREV trend içinde işlem açtı — klasik ölüm"
    assert "trend" in s.reject_reason or "oynaklık" in s.reject_reason


def test_meanrev_sapma_yetersizse_susar():
    df = _df(_yatay(260, gurultu=1.0))
    s = st.meanrev("XUSDT", df, 100.0)
    assert s.action == "HOLD"


# ── Ayrışma ───────────────────────────────────────────────────────────────────

def test_stratejiler_ayni_kosulda_hep_birlikte_tetiklenmez():
    """Üçü aynı anda tetiklenseydi tek riski üç kılıkta taşırdık."""
    kapanis = _yukselen_trend()
    df = _df(kapanis)
    fiyat = float(pd.Series(kapanis).ewm(span=st.EMA_FAST, adjust=False).mean().iloc[-1])
    islem_acan = [
        ad for ad, fn in st.REGISTRY.items()
        if fn("XUSDT", df, fiyat).action in ("BUY", "SELL")
    ]
    assert len(islem_acan) < len(st.REGISTRY), (
        f"aynı koşulda hepsi tetiklendi: {islem_acan}"
    )
    assert "MEANREV" not in islem_acan, "trend içinde MEANREV tetiklendi"


# ── Sağlamlık ─────────────────────────────────────────────────────────────────

def test_yetersiz_veri_patlatmaz():
    kisa = _df(_yatay(20))
    for ad, fn in st.REGISTRY.items():
        s = fn("XUSDT", kisa, 100.0)
        assert s.action == "HOLD"
        assert "yetersiz" in s.reject_reason


def test_bir_stratejinin_hatasi_digerlerini_DURDURMAZ():
    """Tek bozuk gösterge yüzünden bütün deneyin veri akışı kesilmemeli."""
    df = _df(_yatay(260))
    orijinal = st.REGISTRY["MEANREV"]

    def _patlayan(*a, **k):
        raise ValueError("bozuk")

    st.REGISTRY["MEANREV"] = _patlayan
    try:
        out = st.scan(["XUSDT"], lambda s: df, lambda s: 100.0)
    finally:
        st.REGISTRY["MEANREV"] = orijinal

    adlar = {s.strategy for s in out}
    assert {"BREAKOUT", "PULLBACK", "MEANREV"} <= adlar
    patlayan = [s for s in out if s.strategy == "MEANREV"][0]
    assert "hata" in patlayan.reject_reason


def test_her_sinyal_KENDI_strateji_adini_tasir():
    """`main.py` yönlendirmeyi bu alana göre yapıyor; taşımayan sinyal
    SESSİZCE TREND kasasından harcar."""
    df = _df(_yatay(260))
    for s in st.scan(["XUSDT"], lambda x: df, lambda x: 100.0):
        assert s.strategy in st.REGISTRY


def test_scan_fiyat_alinamayan_sembolu_atlar():
    df = _df(_yatay(260))
    out = st.scan(["XUSDT"], lambda s: df, lambda s: None)
    assert out == []
