"""
STRATEGY_MOMENTUM_FAST — ani altcoin hareketlerini erken yakalama motoru.

Neden ayrı bir strateji:
Mevcut TREND stratejisi 4 zaman diliminde (5m/15m/1h/4h) MACD+RSI+EMA hizalanması
arıyor. Bu bir *teyit* sistemidir; teyit geldiğinde hareketin önemli bir kısmı
çoktan olmuştur. Gün içinde %20-80 yapan coinleri yakalamak için farklı sinyaller
gerekir: hacim patlaması, alıcı baskısı, kısa vadeli ivme, breakout.

Neden İZOLE:
Bu kurulumlar aynı zamanda en yüksek yanlış-sinyal oranına sahip olanlardır.
Bu yüzden strateji kendi risk bütçesiyle çalışır (MOMENTUM_RISK_SHARE), her
işlemi `strategy='MOMENTUM_FAST'` olarak kaydedilir ve ayrı ölçülebilir.
Kötü çıkarsa `MOMENTUM_ENABLED=False` ile kapatılır, TREND stratejisi etkilenmez.

Gölge mod (MOMENTUM_SHADOW_MODE=True):
Sinyaller üretilir ve DB'ye yazılır ama İŞLEM AÇILMAZ. Sonuçları sonradan
değerlendirilerek (bkz. evaluate_shadow_signals) canlıya alınıp alınmayacağına
veriyle karar verilir. Varsayılan açık — çünkü bu strateji henüz hiç ölçülmedi.

BTC'nin rolü:
TREND stratejisinde BTC yönü bir filtredir. Burada DEĞİL — bağımsız güçlü bir
altcoin momentumu, BTC yatay diye reddedilmemeli. BTC yalnızca sert çöküşte
(MOM_BTC_CRASH_PCT) rejim vetosu uygular.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

from config import (
    MOMENTUM_MIN_SCORE,
    MOM_RET_1M_MIN, MOM_RET_3M_MIN, MOM_RET_5M_MIN,
    MOM_REL_VOLUME_MIN, MOM_TAKER_BUY_RATIO_MIN, MOM_VOL_BASELINE_BARS,
    MOM_MAX_EXT_ATR, MOM_MAX_IMPULSE_PCT, MOM_REQUIRE_PULLBACK,
    MOM_BTC_CRASH_PCT,
    MAX_SPREAD_PCT, MAX_SL_PCT, MIN_ATR_PCT, MAX_ATR_PCT,
)

logger = logging.getLogger("momentum")

STRATEGY_NAME = "MOMENTUM_FAST"

# Stop mesafesi bu bandın dışına çıkamaz.
_MIN_STOP_PCT = 0.006   # %0.6 — altında gürültü stop'u tetikler
_MAX_STOP_PCT = MAX_SL_PCT


@dataclass
class MomentumSignal:
    symbol:        str
    action:        str          # "BUY" | "SELL" | "HOLD"
    score:         int          # 0-100
    confidence:    float
    price:         float
    sl:            float = 0.0
    tp:            float = 0.0
    atr_pct:       float = 0.0
    reason:        str = ""
    market_regime: str = "MOMENTUM"
    btc_trend:     str = "SIDEWAYS"
    strategy:      str = STRATEGY_NAME
    features:      dict = field(default_factory=dict)
    reject_reason: str = ""


# ── Özellik çıkarımı ──────────────────────────────────────────────────────────

def _safe_ret(series: pd.Series, bars: int) -> float:
    if len(series) <= bars:
        return 0.0
    prev = series.iloc[-1 - bars]
    if prev == 0:
        return 0.0
    return (series.iloc[-1] - prev) / prev


def _atr_pct_1m(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    last = close.iloc[-1]
    if last <= 0 or pd.isna(atr):
        return 0.0
    return float(atr / last)


def extract_features(df: pd.DataFrame, oi_change: float = 0.0,
                     spread: float | None = None) -> dict:
    """
    1m veriden momentum özellikleri.

    Her özellik NaN/eksik veriye karşı korumalıdır; eksikse nötr değer döner.
    Bayat veri kontrolü çağıran tarafın sorumluluğundadır (get_fast_df).
    """
    close = df["close"]
    feats = {
        "ret_1m": _safe_ret(close, 1),
        "ret_3m": _safe_ret(close, 3),
        "ret_5m": _safe_ret(close, 5),
        "ret_15m": _safe_ret(close, 15),
        "oi_change": oi_change,
        "spread": spread if spread is not None else 0.0,
        "spread_known": spread is not None,
    }

    # İvme: son 1 dakikanın hızı, 5 dakikalık ortalama hızın üstünde mi?
    feats["acceleration"] = feats["ret_1m"] - (feats["ret_5m"] / 5.0)

    # Relative volume — mevcut mumun hacmi, önceki N mumun ortalamasına oranla
    qv = df["quote_volume"]
    if len(qv) > MOM_VOL_BASELINE_BARS + 1:
        baseline = qv.iloc[-(MOM_VOL_BASELINE_BARS + 1):-1].mean()
        feats["rel_volume"] = float(qv.iloc[-1] / baseline) if baseline > 0 else 0.0
        feats["vol_baseline"] = float(baseline)
    else:
        feats["rel_volume"] = 0.0
        feats["vol_baseline"] = 0.0

    # Taker alım oranı — son 3 mumun toplamı (tek mum çok gürültülü)
    try:
        tb = df["taker_buy_quote"].iloc[-3:].sum()
        tot = df["quote_volume"].iloc[-3:].sum()
        feats["taker_buy_ratio"] = float(tb / tot) if tot > 0 else 0.5
    except Exception:
        feats["taker_buy_ratio"] = 0.5

    # İşlem frekansı ivmesi
    try:
        tr = df["trades"]
        base_tr = tr.iloc[-(MOM_VOL_BASELINE_BARS + 1):-1].mean()
        feats["trade_accel"] = float(tr.iloc[-1] / base_tr) if base_tr > 0 else 0.0
    except Exception:
        feats["trade_accel"] = 0.0

    # Breakout — rolling yüksekler
    try:
        feats["high_15"] = float(df["high"].iloc[-16:-1].max())
        feats["high_30"] = float(df["high"].iloc[-31:-1].max())
        feats["low_15"]  = float(df["low"].iloc[-16:-1].min())
        feats["low_30"]  = float(df["low"].iloc[-31:-1].min())
    except Exception:
        last = float(close.iloc[-1])
        feats.update(high_15=last, high_30=last, low_15=last, low_30=last)

    feats["atr_pct"] = _atr_pct_1m(df)
    feats["price"]   = float(close.iloc[-1])

    # EMA'dan uzaklık — overextension ölçüsü
    try:
        ema8 = close.ewm(span=8, adjust=False).mean().iloc[-1]
        atr_abs = feats["atr_pct"] * feats["price"]
        feats["ext_atr"] = float((feats["price"] - ema8) / atr_abs) if atr_abs > 0 else 0.0
    except Exception:
        feats["ext_atr"] = 0.0

    # Son mumda geri çekilme oldu mu (dev yeşil mumun tepesinden almamak için)
    try:
        last_high = float(df["high"].iloc[-1])
        feats["pullback_from_high"] = (
            (last_high - feats["price"]) / last_high if last_high > 0 else 0.0
        )
    except Exception:
        feats["pullback_from_high"] = 0.0

    return feats


# ── Skorlama ──────────────────────────────────────────────────────────────────

def score_momentum(feats: dict, direction: str) -> tuple[int, list[str]]:
    """
    0-100 momentum skoru. TREND stratejisinin skorundan tamamen ayrıdır.

    Ağırlıklar: ivme 25, hacim 25, breakout 20, alıcı baskısı 15, OI 10, frekans 5.
    """
    score = 0
    notes: list[str] = []
    sign = 1 if direction == "BUY" else -1

    r1 = feats["ret_1m"] * sign
    r3 = feats["ret_3m"] * sign
    r5 = feats["ret_5m"] * sign

    # A) Kısa vadeli fiyat ivmesi (25)
    if r1 >= MOM_RET_1M_MIN:
        score += 8; notes.append(f"1m {r1*100:+.1f}%")
    if r3 >= MOM_RET_3M_MIN:
        score += 9; notes.append(f"3m {r3*100:+.1f}%")
    if r5 >= MOM_RET_5M_MIN:
        score += 8; notes.append(f"5m {r5*100:+.1f}%")

    # B) Relative volume patlaması (25)
    rv = feats.get("rel_volume", 0.0)
    if rv >= MOM_REL_VOLUME_MIN * 2:
        score += 25; notes.append(f"Hacim {rv:.1f}x")
    elif rv >= MOM_REL_VOLUME_MIN:
        score += 17; notes.append(f"Hacim {rv:.1f}x")
    elif rv >= MOM_REL_VOLUME_MIN * 0.6:
        score += 8

    # C) Breakout (20)
    px = feats["price"]
    if direction == "BUY":
        if px > feats.get("high_30", px):
            score += 20; notes.append("30dk kırılım")
        elif px > feats.get("high_15", px):
            score += 12; notes.append("15dk kırılım")
    else:
        if px < feats.get("low_30", px):
            score += 20; notes.append("30dk kırılım↓")
        elif px < feats.get("low_15", px):
            score += 12; notes.append("15dk kırılım↓")

    # D) Taker alım/satım baskısı (15)
    tbr = feats.get("taker_buy_ratio", 0.5)
    eff = tbr if direction == "BUY" else (1 - tbr)
    if eff >= MOM_TAKER_BUY_RATIO_MIN + 0.08:
        score += 15; notes.append(f"Taker {eff*100:.0f}%")
    elif eff >= MOM_TAKER_BUY_RATIO_MIN:
        score += 10; notes.append(f"Taker {eff*100:.0f}%")

    # E) Open interest doğrulaması (10)
    oi = feats.get("oi_change", 0.0)
    if oi > 0.02:
        score += 10; notes.append(f"OI {oi*100:+.1f}%")
    elif oi > 0.005:
        score += 5

    # F) İşlem frekansı ivmesi (5)
    if feats.get("trade_accel", 0.0) >= 2.0:
        score += 5; notes.append("İşlem freq↑")

    return min(score, 100), notes


# ── Overextension / chase koruması ────────────────────────────────────────────

def overextension_check(feats: dict, direction: str) -> str:
    """
    Dev yeşil mumun tepesinden kör alım yapmayı engeller.

    Boş string dönerse geçti; aksi hâlde red sebebi.
    Filtre bilinçli olarak GEVŞEK tutulmuştur — çok katı olursa yakalamak
    istediğimiz büyük trendlerin tamamı elenir.
    """
    sign = 1 if direction == "BUY" else -1
    ext = feats.get("ext_atr", 0.0) * sign
    if ext > MOM_MAX_EXT_ATR:
        return f"Aşırı uzamış (EMA'dan {ext:.1f}×ATR)"

    impulse = feats.get("ret_5m", 0.0) * sign
    if impulse > MOM_MAX_IMPULSE_PCT:
        if MOM_REQUIRE_PULLBACK and feats.get("pullback_from_high", 0.0) < 0.005:
            return f"Dikey hareket (%{impulse*100:.0f}), geri çekilme yok"

    atr = feats.get("atr_pct", 0.0)
    if atr > MAX_ATR_PCT:
        return f"Aşırı volatil (ATR %{atr*100:.1f})"
    if atr < MIN_ATR_PCT:
        return f"Çok durgun (ATR %{atr*100:.2f})"

    if feats.get("spread_known") and feats.get("spread", 0) > MAX_SPREAD_PCT:
        return f"Spread geniş (%{feats['spread']*100:.2f})"

    return ""


# ── Stop / hedef ──────────────────────────────────────────────────────────────

def compute_stop(feats: dict, direction: str) -> float:
    """
    Momentum stop'u — swing tabanlı, ATR ile sınırlı.

    Sabit yüzde kullanılmaz: ölçülen ortalama 1m ATR bandında sabit %1'lik bir
    stop gürültünün içinde kalır ve tam yakalamak istediğimiz hareketi keser.
    """
    px = feats["price"]
    atr_abs = max(feats.get("atr_pct", 0.01), 0.002) * px

    if direction == "BUY":
        swing = feats.get("low_15", px * 0.99)
        dist = max(px - swing, atr_abs * 2.0)
    else:
        swing = feats.get("high_15", px * 1.01)
        dist = max(swing - px, atr_abs * 2.0)

    dist = max(dist, px * _MIN_STOP_PCT)
    dist = min(dist, px * _MAX_STOP_PCT)
    return px - dist if direction == "BUY" else px + dist


# ── Ana giriş noktası ─────────────────────────────────────────────────────────

def analyze(symbol: str, df: pd.DataFrame, oi_change: float = 0.0,
            spread: float | None = None, btc_change_15m: float = 0.0
            ) -> MomentumSignal | None:
    """
    Tek sembol için momentum analizi.

    None döner → veri yetersiz.
    action='HOLD' + reject_reason → sinyal var ama filtreye takıldı (loglanır).
    """
    if df is None or len(df) < 25:
        return None

    feats = extract_features(df, oi_change=oi_change, spread=spread)
    px = feats["price"]
    if px <= 0:
        return None

    # Yön: kısa vadeli getirilerin işareti
    if feats["ret_3m"] > 0 and feats["ret_5m"] > 0:
        direction = "BUY"
    elif feats["ret_3m"] < 0 and feats["ret_5m"] < 0:
        direction = "SELL"
    else:
        return None

    score, notes = score_momentum(feats, direction)
    if score < MOMENTUM_MIN_SCORE:
        return None

    # BTC rejim vetosu — yalnızca sert çöküşte, yön filtresi olarak DEĞİL
    if btc_change_15m <= MOM_BTC_CRASH_PCT:
        return MomentumSignal(
            symbol=symbol, action="HOLD", score=score, confidence=score / 100,
            price=px, features=feats,
            reject_reason=f"BTC çöküşü (%{btc_change_15m*100:.1f}) — momentum vetosu",
        )

    rejected = overextension_check(feats, direction)
    if rejected:
        return MomentumSignal(
            symbol=symbol, action="HOLD", score=score, confidence=score / 100,
            price=px, features=feats, reject_reason=rejected,
        )

    sl = compute_stop(feats, direction)
    # TP yalnızca referanstır; runner'ın ÜST LİMİTİ YOKTUR (bkz. position.py).
    risk = abs(px - sl)
    tp = px + risk * 2 if direction == "BUY" else px - risk * 2

    return MomentumSignal(
        symbol=symbol, action=direction, score=score,
        confidence=round(score / 100, 3), price=px, sl=sl, tp=tp,
        atr_pct=feats.get("atr_pct", 0.0),
        reason="⚡MOM " + " | ".join(notes[:5]),
        features=feats,
    )


def scan(mdm, symbols: list[str], btc_change_15m: float = 0.0) -> list[MomentumSignal]:
    """Kısa liste üzerinde momentum taraması. Skora göre sıralı döner."""
    out: list[MomentumSignal] = []
    for sym in symbols:
        if sym == "BTCUSDT":
            continue
        df = mdm.get_fast_df(sym)
        if df is None:
            continue   # bayat veya hiç veri yok — sessizce atla
        try:
            sig = analyze(
                sym, df,
                oi_change=mdm.get_oi_change_pct(sym),
                spread=mdm.get_spread_pct(sym),
                btc_change_15m=btc_change_15m,
            )
        except Exception as e:
            logger.debug(f"Momentum analiz hatası {sym}: {e}")
            continue
        if sig is not None:
            out.append(sig)
    out.sort(key=lambda s: s.score, reverse=True)
    return out
