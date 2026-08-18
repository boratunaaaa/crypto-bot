"""
Deney stratejileri: BREAKOUT, PULLBACK, MEANREV.

Her biri KENDİ sanal portföyüyle çalışır (bkz. `portfolios.py`), ortak çıkış
motorunu ve emir simülasyonunu kullanır (bkz. `position.py`, `trader.py`).

── PARAMETRELER GEÇMİŞ VERİYE UYDURULMADI ───────────────────────────────────
Buradaki her eşik KONVANSİYONEL bir varsayılandır; bu botun geçmişine bakılıp
seçilmedi:
    Donchian kanalı 20 bar        — Donchian'ın klasik tanımı
    hacim teyidi 1.5×             — yaygın kırılım teyidi eşiği
    EMA 20 / 50 / 200             — standart trend şablonu
    z-skoru |2|                   — Bollinger'ın 2 standart sapması
    RSI 30 / 70                   — Wilder'ın orijinal eşikleri
    ATR 14, SL 2.0×ATR            — mevcut botla AYNI (config'ten gelir)

Bunu bilerek yapıyoruz: eşikleri geçmiş veriye uydurmak backtest'i
güzelleştirir, geleceği değil. Deneyin amacı "hangi eşik iyi görünüyor"
değil, "hangi FİKRİN tekrarlanabilir avantajı var".

── HER STRATEJİ FARKLI BİR PİYASA DAVRANIŞINI HEDEFLER ──────────────────────
Aynı fikrin parametre varyasyonları ayrı strateji sayılmaz; öyle olsaydı bu
gizli bir grid search olur ve denenen konfigürasyon sayısı arttıkça seçim
yanlılığı büyürdü.

    BREAKOUT — sıkışmadan çıkış anı        (hareketin BAŞI)
    PULLBACK — kurulu trendde geri çekilme (hareketin DEVAMI)
    MEANREV  — aşırıya giden hareketin dönüşü (hareketin SONU)

Üçü aynı anda aynı sembolde tetiklenmez; MEANREV yalnız trend YOKKEN,
PULLBACK yalnız trend VARKEN çalışır. Bu, üçünün aynı riski üç kılıkta
taşımasını yapısal olarak zorlaştırır — ama garanti etmez, korelasyon yine
ölçülmelidir.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pandas_ta as ta

from config import (
    ATR_PERIOD,
    MAX_SL_PCT,
    MAX_TP_PCT,
    MIN_ATR_PCT,
    MAX_ATR_PCT,
    SL_ATR_MULT,
    TP_ATR_MULT,
)

# ── Konvansiyonel sabitler (geçmiş veriye uydurulmadı) ────────────────────────
DONCHIAN_BARS      = 20     # Donchian kanalının klasik uzunluğu
VOLUME_CONFIRM_X   = 1.5    # kırılımda yaygın hacim teyidi eşiği
EMA_FAST           = 20
EMA_MID            = 50
EMA_SLOW           = 200
ZSCORE_EXTREME     = 2.0    # Bollinger'ın 2 standart sapması
RSI_OVERSOLD       = 30     # Wilder
RSI_OVERBOUGHT     = 70
MIN_BARS           = 210    # EMA200 için yeterli geçmiş


@dataclass
class StratSignal:
    """`analyzer.Signal` ile aynı alanları taşır + `strategy`.

    Ayrı bir sınıf çünkü `Signal` TREND'e özgü alanlar (rsi, trend) zorunlu
    kılıyor ve `strategy` alanı yok — `main.py` yönlendirmeyi o alana göre
    yapıyor, taşımayan sinyal SESSİZCE TREND kasasından harcar.
    """

    symbol:        str
    action:        str          # "BUY" | "SELL" | "HOLD"
    confidence:    float
    reason:        str
    price:         float
    score:         int
    strategy:      str
    sl:            float = 0.0
    tp:            float = 0.0
    atr_pct:       float = 0.0
    btc_trend:     str = "SIDEWAYS"
    market_regime: str = "UNKNOWN"
    features:      dict = field(default_factory=dict)
    reject_reason: str = ""


def _hazir(df: pd.DataFrame | None) -> bool:
    return df is not None and not df.empty and len(df) >= MIN_BARS


def _atr_pct(df: pd.DataFrame, price: float) -> tuple[float, float]:
    seri = ta.atr(df["high"], df["low"], df["close"], length=ATR_PERIOD)
    if seri is None or seri.empty or price <= 0:
        return 0.0, 0.0
    atr = float(seri.iloc[-1])
    return atr, (atr / price)


def _sl_tp(price: float, action: str, atr: float) -> tuple[float, float]:
    """Mevcut botla AYNI stop/hedef modeli — config'ten gelir.

    Bilerek ortak: stratejiler ÇIKIŞ mantığıyla değil GİRİŞ fikriyle
    ayrışmalı. Çıkışı da değiştirseydik, ölçtüğümüz fark hangi bileşenden
    geliyor bilemezdik.
    """
    sl_dist = min(atr * SL_ATR_MULT, price * MAX_SL_PCT)
    tp_dist = min(atr * TP_ATR_MULT, price * MAX_TP_PCT)
    if action == "BUY":
        return price - sl_dist, price + tp_dist
    return price + sl_dist, price - tp_dist


def _oynaklik_uygun(atr_pct: float) -> bool:
    """Çok durgun ya da çok çılgın semboller elenir — mevcut botla aynı bant."""
    return MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT


def _red(symbol: str, price: float, strateji: str, sebep: str) -> StratSignal:
    return StratSignal(
        symbol=symbol, action="HOLD", confidence=0.0, reason="", price=price,
        score=0, strategy=strateji, reject_reason=sebep,
    )


# ── BREAKOUT: sıkışmadan çıkış anı ────────────────────────────────────────────

STRATEGY_BREAKOUT = "BREAKOUT"


def breakout(symbol: str, df: pd.DataFrame, price: float,
             btc_trend: str = "SIDEWAYS") -> StratSignal:
    """Donchian kanal kırılımı + hacim teyidi.

    Fikir: fiyat son `DONCHIAN_BARS` barın en yükseğini AŞARSA, sıkışma
    çözülüyor olabilir. Hacim teyidi şart: hacimsiz kırılım çoğunlukla
    gürültüdür ve hemen geri gelir.

    Kullanıcının asıl ilgilendiği "bir coin %50 gidecekse erken yakala"
    problemine en doğrudan saldıran strateji budur.
    """
    if not _hazir(df):
        return _red(symbol, price, STRATEGY_BREAKOUT, "yetersiz veri")

    atr, atrp = _atr_pct(df, price)
    if not _oynaklik_uygun(atrp):
        return _red(symbol, price, STRATEGY_BREAKOUT, f"oynaklık dışı (%{atrp*100:.2f})")

    # Kırılımın kendisi kanala DAHİL EDİLMEZ: son barı da sayarsak fiyat
    # her zaman kendi tepesine eşit olur ve her bar "kırılım" görünür.
    onceki = df.iloc[:-1]
    ust = float(onceki["high"].tail(DONCHIAN_BARS).max())
    alt = float(onceki["low"].tail(DONCHIAN_BARS).min())

    hacim = df["volume"]
    ort_hacim = float(hacim.iloc[-(DONCHIAN_BARS + 1):-1].mean())
    son_hacim = float(hacim.iloc[-1])
    hacim_x = (son_hacim / ort_hacim) if ort_hacim > 0 else 0.0

    if hacim_x < VOLUME_CONFIRM_X:
        return _red(symbol, price, STRATEGY_BREAKOUT, f"hacim teyidi yok ({hacim_x:.1f}×)")

    if price > ust:
        action, yon = "BUY", "yukarı"
    elif price < alt:
        action, yon = "SELL", "aşağı"
    else:
        return _red(symbol, price, STRATEGY_BREAKOUT, "kanal içinde")

    # Skor: hacim teyidi ne kadar güçlü + kırılım ne kadar net.
    kirilim_gucu = abs(price - (ust if action == "BUY" else alt)) / max(atr, 1e-12)
    skor = int(min(100, 60 + min(hacim_x - VOLUME_CONFIRM_X, 3.0) * 8
                   + min(kirilim_gucu, 2.0) * 6))
    sl, tp = _sl_tp(price, action, atr)
    return StratSignal(
        symbol=symbol, action=action, confidence=skor / 100, price=price,
        score=skor, strategy=STRATEGY_BREAKOUT, sl=sl, tp=tp, atr_pct=atrp,
        btc_trend=btc_trend, market_regime="BREAKOUT",
        reason=f"⚡{DONCHIAN_BARS}bar {yon} kırılım | Hacim {hacim_x:.1f}×",
        features={"hacim_x": round(hacim_x, 2), "kirilim_atr": round(kirilim_gucu, 2)},
    )


# ── PULLBACK: kurulu trendde geri çekilme ─────────────────────────────────────

STRATEGY_PULLBACK = "PULLBACK"


def pullback(symbol: str, df: pd.DataFrame, price: float,
             btc_trend: str = "SIDEWAYS") -> StratSignal:
    """Trend YÖNÜNDE, geri çekilme sonrası giriş.

    Fikir: trend kurulmuşsa (EMA50 > EMA200), fiyatın EMA20'ye geri çekilmesi
    trendin bitmesi değil nefes almasıdır. BREAKOUT hareketin başını,
    PULLBACK devamını hedefler — bu yüzden ikisi aynı anda tetiklenmez.

    Aşırı düşüş filtresi: RSI 30'un ALTINDAYSA bu "geri çekilme" değil
    "trend kırılması" olabilir; girilmez.
    """
    if not _hazir(df):
        return _red(symbol, price, STRATEGY_PULLBACK, "yetersiz veri")

    atr, atrp = _atr_pct(df, price)
    if not _oynaklik_uygun(atrp):
        return _red(symbol, price, STRATEGY_PULLBACK, f"oynaklık dışı (%{atrp*100:.2f})")

    kapanis = df["close"]
    ema_f = float(ta.ema(kapanis, length=EMA_FAST).iloc[-1])
    ema_m = float(ta.ema(kapanis, length=EMA_MID).iloc[-1])
    ema_s = float(ta.ema(kapanis, length=EMA_SLOW).iloc[-1])
    rsi_s = ta.rsi(kapanis, length=14)
    rsi = float(rsi_s.iloc[-1]) if rsi_s is not None and not rsi_s.empty else 50.0

    yukselen = ema_m > ema_s
    dusen = ema_m < ema_s

    # Geri çekilme = fiyat EMA20'ye 1 ATR'den yakın.
    ema_mesafe = abs(price - ema_f) / max(atr, 1e-12)
    if ema_mesafe > 1.0:
        return _red(symbol, price, STRATEGY_PULLBACK,
                    f"EMA{EMA_FAST}'den uzak ({ema_mesafe:.1f}×ATR)")

    if yukselen and price > ema_s:
        if rsi < RSI_OVERSOLD:
            return _red(symbol, price, STRATEGY_PULLBACK, f"trend kırılıyor olabilir (RSI {rsi:.0f})")
        action, yon = "BUY", "yükselen"
    elif dusen and price < ema_s:
        if rsi > RSI_OVERBOUGHT:
            return _red(symbol, price, STRATEGY_PULLBACK, f"trend kırılıyor olabilir (RSI {rsi:.0f})")
        action, yon = "SELL", "düşen"
    else:
        return _red(symbol, price, STRATEGY_PULLBACK, "kurulu trend yok")

    # Trend ne kadar net + geri çekilme ne kadar temiz.
    trend_gucu = abs(ema_m - ema_s) / max(atr, 1e-12)
    skor = int(min(100, 58 + min(trend_gucu, 4.0) * 7 + (1.0 - ema_mesafe) * 10))
    sl, tp = _sl_tp(price, action, atr)
    return StratSignal(
        symbol=symbol, action=action, confidence=skor / 100, price=price,
        score=skor, strategy=STRATEGY_PULLBACK, sl=sl, tp=tp, atr_pct=atrp,
        btc_trend=btc_trend, market_regime="TREND",
        reason=f"↩︎ {yon} trendde geri çekilme | EMA{EMA_FAST}'e {ema_mesafe:.1f}×ATR | RSI {rsi:.0f}",
        features={"trend_gucu": round(trend_gucu, 2), "ema_mesafe": round(ema_mesafe, 2),
                  "rsi": round(rsi, 1)},
    )


# ── MEANREV: aşırıya giden hareketin dönüşü ───────────────────────────────────

STRATEGY_MEANREV = "MEANREV"


def meanrev(symbol: str, df: pd.DataFrame, price: float,
            btc_trend: str = "SIDEWAYS") -> StratSignal:
    """Ortalamadan aşırı sapma sonrası dönüş — YALNIZ trend yokken.

    Fikir: yatay bir piyasada fiyat ortalamasından 2 standart sapma uzağa
    giderse geri gelme eğilimindedir.

    TREND FİLTRESİ HAYATİ: ortalamaya dönüş stratejisi trend içinde
    uygulanırsa her düşüşte alır ve trend boyunca kaybeder — bu stratejinin
    klasik ölüm biçimi budur. EMA50/EMA200 birbirine yakın değilse (yani
    trend varsa) sinyal üretilmez.
    """
    if not _hazir(df):
        return _red(symbol, price, STRATEGY_MEANREV, "yetersiz veri")

    atr, atrp = _atr_pct(df, price)
    if not _oynaklik_uygun(atrp):
        return _red(symbol, price, STRATEGY_MEANREV, f"oynaklık dışı (%{atrp*100:.2f})")

    kapanis = df["close"]
    ema_m = float(ta.ema(kapanis, length=EMA_MID).iloc[-1])
    ema_s = float(ta.ema(kapanis, length=EMA_SLOW).iloc[-1])

    # Trend varsa ortalamaya dönüş DENENMEZ.
    trend_gucu = abs(ema_m - ema_s) / max(atr, 1e-12)
    if trend_gucu > 1.0:
        return _red(symbol, price, STRATEGY_MEANREV,
                    f"trend var ({trend_gucu:.1f}×ATR) — ortalamaya dönüş uygulanmaz")

    pencere = kapanis.tail(DONCHIAN_BARS)
    ort = float(pencere.mean())
    sapma = float(pencere.std())
    if sapma <= 0:
        return _red(symbol, price, STRATEGY_MEANREV, "sapma yok")
    z = (price - ort) / sapma

    if z <= -ZSCORE_EXTREME:
        action, yon = "BUY", "aşırı düşüş"
    elif z >= ZSCORE_EXTREME:
        action, yon = "SELL", "aşırı yükseliş"
    else:
        return _red(symbol, price, STRATEGY_MEANREV, f"sapma yetersiz (z={z:+.1f})")

    skor = int(min(100, 58 + min(abs(z) - ZSCORE_EXTREME, 2.0) * 12
                   + (1.0 - min(trend_gucu, 1.0)) * 8))
    sl, tp = _sl_tp(price, action, atr)
    return StratSignal(
        symbol=symbol, action=action, confidence=skor / 100, price=price,
        score=skor, strategy=STRATEGY_MEANREV, sl=sl, tp=tp, atr_pct=atrp,
        btc_trend=btc_trend, market_regime="RANGE",
        reason=f"⟲ {yon} (z={z:+.1f}) | yatay piyasa ({trend_gucu:.1f}×ATR)",
        features={"z": round(z, 2), "trend_gucu": round(trend_gucu, 2)},
    )


# ── Kayıt defteri ─────────────────────────────────────────────────────────────

REGISTRY = {
    STRATEGY_BREAKOUT: breakout,
    STRATEGY_PULLBACK: pullback,
    STRATEGY_MEANREV:  meanrev,
}


def scan(symbols: list[str], get_ohlcv, get_price, btc_trend: str = "SIDEWAYS",
         only: list[str] | None = None) -> list[StratSignal]:
    """Tüm deney stratejilerini tara.

    Bir stratejinin hatası DİĞERLERİNİ durdurmaz: tek bozuk gösterge yüzünden
    bütün deneyin veri akışı kesilirse yedi hafta sonra elimizde hiçbir şey
    olmaz.
    """
    secili = {ad: fn for ad, fn in REGISTRY.items() if not only or ad in only}
    out: list[StratSignal] = []
    for sym in symbols:
        df = None
        try:
            df = get_ohlcv(sym)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        try:
            price = float(get_price(sym) or 0.0)
        except Exception:
            continue
        if price <= 0:
            continue
        for ad, fn in secili.items():
            try:
                out.append(fn(sym, df, price, btc_trend))
            except Exception as exc:  # noqa: BLE001 — bir strateji diğerini durduramaz
                out.append(_red(sym, price, ad, f"hata: {type(exc).__name__}"))
    return out
