"""
Sinyal motoru — 0-100 puan sistemi, 4 timeframe (5m/15m/1h/4h), BTC filtresi.

Puan dağılımı (toplam 100):
  MTF Trend (4h+1h+15m+5m) : 30
  BTC Yön Uyumu             : 20
  Breakout / Hacim          : 15
  MACD                      : 10
  RSI                       : 10
  EMA Pattern               : 10
  Momentum + Funding        :  5
"""

import pandas as pd
import pandas_ta as ta
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from config import (
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BB_PERIOD, BB_STD,
    ATR_PERIOD, SL_ATR_MULT, TP_ATR_MULT,
    MAX_SL_PCT, MAX_TP_PCT,
    MIN_ATR_PCT, MAX_ATR_PCT,
    MIN_CONFIDENCE, MIN_SIGNAL_SCORE,
    TF_ENTRY, TF_TREND, TF_MACRO, TF_CONTEXT,
    LEV_STRONG,
    ADX_PERIOD, ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
)
from risk_manager import safe_stop_loss


@dataclass
class Signal:
    symbol:       str
    action:       str        # "BUY" | "SELL" | "HOLD"
    confidence:   float      # 0.0 – 1.0
    reason:       str
    price:        float
    rsi:          float
    trend:        str        # "UP" | "DOWN" | "SIDEWAYS"
    score:        int        # 0–100
    atr:          float
    sl:           float = 0.0
    tp:           float = 0.0
    atr_pct:      float = 0.0
    btc_trend:    str   = "SIDEWAYS"
    market_regime:str   = "UNKNOWN"   # TREND / RANGE / CHAOTIC


def _atr(df: pd.DataFrame) -> float:
    atr_s = ta.atr(df["high"], df["low"], df["close"], length=ATR_PERIOD)
    if atr_s is None or atr_s.empty:
        return 0.0
    return float(atr_s.iloc[-1])


def _trend_from_df(df: pd.DataFrame) -> str:
    """EMA hizalamasına göre trend."""
    if len(df) < EMA_TREND + 5:
        return "SIDEWAYS"
    c  = df["close"]
    ef = ta.ema(c, length=EMA_FAST)
    es = ta.ema(c, length=EMA_SLOW)
    et = ta.ema(c, length=EMA_TREND)
    if ef is None or es is None or et is None:
        return "SIDEWAYS"
    if float(ef.iloc[-1]) > float(es.iloc[-1]) > float(et.iloc[-1]):
        return "UP"
    if float(ef.iloc[-1]) < float(es.iloc[-1]) < float(et.iloc[-1]):
        return "DOWN"
    return "SIDEWAYS"


def _market_regime(df: pd.DataFrame) -> str:
    """ADX ile piyasa rejimini belirle: TREND / RANGE / CHAOTIC."""
    try:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_PERIOD)
        if adx_df is None or adx_df.empty:
            return "UNKNOWN"
        col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not col:
            return "UNKNOWN"
        val = float(adx_df[col[0]].iloc[-1])
        if val >= ADX_TREND_THRESHOLD:
            return "TREND"
        if val >= ADX_RANGE_THRESHOLD:
            return "RANGE"
        return "CHAOTIC"
    except Exception:
        return "UNKNOWN"


def _analyze_single(
    symbol:     str,
    df_entry:   pd.DataFrame,          # 5m
    df_trend:   Optional[pd.DataFrame],# 15m
    df_macro:   Optional[pd.DataFrame],# 1h
    df_context: Optional[pd.DataFrame],# 4h
    price:      Optional[float],
    funding:    float,
    btc_trend:  str = "SIDEWAYS",
    oi_trend:   str = "FLAT",
) -> Signal:

    current_price = price if price else float(df_entry["close"].iloc[-1])

    if len(df_entry) < 50:
        return Signal(symbol, "HOLD", 0.0, "Yetersiz veri",
                      current_price, 50.0, "SIDEWAYS", 0, 0.0,
                      btc_trend=btc_trend)

    # ── Piyasa rejimi ─────────────────────────────────────────────────────────
    regime = _market_regime(df_entry)
    if regime == "CHAOTIC":
        return Signal(symbol, "HOLD", 0.0, f"Kaotik piyasa (ADX düşük)",
                      current_price, 50.0, "SIDEWAYS", 0, 0.0,
                      btc_trend=btc_trend, market_regime=regime)

    close = df_entry["close"]

    # ── ATR ve volatilite filtresi ────────────────────────────────────────────
    atr_val = _atr(df_entry)
    atr_pct = atr_val / current_price if current_price > 0 else 0.0

    if atr_pct < MIN_ATR_PCT:
        return Signal(symbol, "HOLD", 0.0,
                      f"Düşük volatilite ({atr_pct*100:.2f}%)",
                      current_price, 50.0, "SIDEWAYS", 0, atr_val, atr_pct=atr_pct)
    if atr_pct > MAX_ATR_PCT:
        return Signal(symbol, "HOLD", 0.0,
                      f"Aşırı volatilite ({atr_pct*100:.2f}%)",
                      current_price, 50.0, "SIDEWAYS", 0, atr_val, atr_pct=atr_pct)

    # ── Multi-timeframe trendler ──────────────────────────────────────────────
    trend_5m  = _trend_from_df(df_entry)
    trend_15m = _trend_from_df(df_trend)   if df_trend   is not None else "SIDEWAYS"
    trend_1h  = _trend_from_df(df_macro)   if df_macro   is not None else "SIDEWAYS"
    trend_4h  = _trend_from_df(df_context) if df_context is not None else "SIDEWAYS"

    # ── RSI ───────────────────────────────────────────────────────────────────
    rsi_s = ta.rsi(close, length=RSI_PERIOD)
    rsi   = float(rsi_s.iloc[-1]) if rsi_s is not None and len(rsi_s) > 0 else 50.0

    # ── EMA (entry TF) ────────────────────────────────────────────────────────
    ef = ta.ema(close, length=EMA_FAST)
    es = ta.ema(close, length=EMA_SLOW)
    et = ta.ema(close, length=EMA_TREND)

    if ef is None or es is None or et is None:
        return Signal(symbol, "HOLD", 0.0, "EMA hatası",
                      current_price, rsi, trend_5m, 0, atr_val)

    ef_now, ef_prev = float(ef.iloc[-1]), float(ef.iloc[-2])
    es_now, es_prev = float(es.iloc[-1]), float(es.iloc[-2])
    et_now          = float(et.iloc[-1])

    ema_bull     = ef_now > es_now > et_now
    ema_bear     = ef_now < es_now < et_now
    golden_cross = ef_prev <= es_prev and ef_now > es_now
    death_cross  = ef_prev >= es_prev and ef_now < es_now

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_df   = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    macd_bull = macd_bear = False
    if macd_df is not None and len(macd_df) >= 2:
        _line_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
        _hist_col = next((c for c in macd_df.columns if "MACDh_" in c), None)
        _sig_col  = next((c for c in macd_df.columns if "MACDs_" in c), None)
        if _line_col and _hist_col and _sig_col:
            hist_now  = float(macd_df[_hist_col].iloc[-1])
            hist_prev = float(macd_df[_hist_col].iloc[-2])
            line_now  = float(macd_df[_line_col].iloc[-1])
            sig_now   = float(macd_df[_sig_col].iloc[-1])
            macd_bull = line_now > sig_now and hist_now > hist_prev
            macd_bear = line_now < sig_now and hist_now < hist_prev

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    near_lower = near_upper = squeeze = False
    bb = ta.bbands(close, length=BB_PERIOD, std=BB_STD)
    if bb is not None and not bb.empty:
        _bbl = next((c for c in bb.columns if c.startswith("BBL_")), None)
        _bbm = next((c for c in bb.columns if c.startswith("BBM_")), None)
        _bbu = next((c for c in bb.columns if c.startswith("BBU_")), None)
        bb_lower = float(bb[_bbl].iloc[-1]) if _bbl else float(bb.iloc[-1, 0])
        bb_upper = float(bb[_bbu].iloc[-1]) if _bbu else float(bb.iloc[-1, 2])
        bb_mid   = float(bb[_bbm].iloc[-1]) if _bbm else float(bb.iloc[-1, 1])
        bb_width = (bb_upper - bb_lower) / bb_mid
        near_lower = current_price <= bb_lower * 1.003
        near_upper = current_price >= bb_upper * 0.997
        squeeze    = bb_width < 0.02

    # ── Breakout ─────────────────────────────────────────────────────────────
    lookback      = min(20, len(df_entry) - 1)
    recent_high   = df_entry["high"].iloc[-lookback-1:-1].max()
    recent_low    = df_entry["low"].iloc[-lookback-1:-1].min()
    breakout_up   = current_price > recent_high * 1.001
    breakout_down = current_price < recent_low  * 0.999

    # ── Momentum ──────────────────────────────────────────────────────────────
    roc = ta.roc(close, length=5)
    roc_val         = float(roc.iloc[-1]) if roc is not None and len(roc) > 0 else 0.0
    strong_bull_mom = roc_val > 0.5
    strong_bear_mom = roc_val < -0.5

    # ── Hacim ─────────────────────────────────────────────────────────────────
    vol_avg     = df_entry["volume"].rolling(20).mean().iloc[-1]
    vol_now     = df_entry["volume"].iloc[-1]
    high_volume = vol_now > vol_avg * 1.8

    # ── Funding rate ──────────────────────────────────────────────────────────
    funding_favors_long  = funding < -0.0005
    funding_favors_short = funding > 0.0005

    # ═══════════════════════════════════════════════════════════════════════════
    # PUAN SİSTEMİ — 100 puan üzerinden
    # ═══════════════════════════════════════════════════════════════════════════
    buy_score  = 0
    sell_score = 0
    buy_reasons  = []
    sell_reasons = []

    # ── 1. MTF Trend Uyumu (max 30 puan) ─────────────────────────────────────
    # 4h = 10, 1h = 8, 15m = 7, 5m = 5
    if trend_4h == "UP":
        buy_score  += 10; buy_reasons.append("4h↑")
    elif trend_4h == "DOWN":
        sell_score += 10; sell_reasons.append("4h↓")

    if trend_1h == "UP":
        buy_score  += 8; buy_reasons.append("1h↑")
    elif trend_1h == "DOWN":
        sell_score += 8; sell_reasons.append("1h↓")

    if trend_15m == "UP":
        buy_score  += 7; buy_reasons.append("15m↑")
    elif trend_15m == "DOWN":
        sell_score += 7; sell_reasons.append("15m↓")

    if trend_5m == "UP":
        buy_score  += 5; buy_reasons.append("5m↑")
    elif trend_5m == "DOWN":
        sell_score += 5; sell_reasons.append("5m↓")

    # ── 2. BTC Yön Uyumu (max 20 puan) ───────────────────────────────────────
    if btc_trend == "UP":
        buy_score  += 20; buy_reasons.append("BTC↑")
    elif btc_trend == "DOWN":
        sell_score += 20; sell_reasons.append("BTC↓")
    else:  # SIDEWAYS — ikisi de biraz puan alır
        buy_score  += 10
        sell_score += 10

    # ── 3. Breakout / Hacim (max 15 puan) ────────────────────────────────────
    if breakout_up and high_volume:
        buy_score  += 15; buy_reasons.append("⚡Breakout↑+Hacim")
    elif breakout_down and high_volume:
        sell_score += 15; sell_reasons.append("⚡Breakout↓+Hacim")
    elif squeeze and strong_bull_mom:
        buy_score  += 10; buy_reasons.append("BB sıkışma+↑")
    elif squeeze and strong_bear_mom:
        sell_score += 10; sell_reasons.append("BB sıkışma+↓")
    elif high_volume:
        if trend_5m == "UP":
            buy_score  += 8; buy_reasons.append("Hacim↑")
        elif trend_5m == "DOWN":
            sell_score += 8; sell_reasons.append("Hacim↑")
    elif near_lower:
        buy_score  += 4; buy_reasons.append("BB Alt")
    elif near_upper:
        sell_score += 4; sell_reasons.append("BB Üst")

    # ── 4. MACD (max 10 puan) ─────────────────────────────────────────────────
    if macd_bull:
        buy_score  += 10; buy_reasons.append("MACD↑")
    elif macd_bear:
        sell_score += 10; sell_reasons.append("MACD↓")

    # ── 5. RSI (max 10 puan) ──────────────────────────────────────────────────
    if rsi < RSI_OVERSOLD:
        buy_score  += 10; buy_reasons.append(f"RSI({rsi:.0f})↓")
    elif rsi < 40:
        buy_score  += 5;  buy_reasons.append(f"RSI({rsi:.0f})")
    elif rsi > RSI_OVERBOUGHT:
        sell_score += 10; sell_reasons.append(f"RSI({rsi:.0f})↑")
    elif rsi > 60:
        sell_score += 5;  sell_reasons.append(f"RSI({rsi:.0f})")

    # ── 6. EMA Pattern (max 10 puan) ─────────────────────────────────────────
    if golden_cross:
        buy_score  += 10; buy_reasons.append("Altın Kesişim")
    elif death_cross:
        sell_score += 10; sell_reasons.append("Ölüm Kesişimi")
    elif ema_bull:
        buy_score  += 5; buy_reasons.append("EMA↑")
    elif ema_bear:
        sell_score += 5; sell_reasons.append("EMA↓")

    # ── 7. Momentum + Funding + OI (max 7 puan, overflow 100'e taşınır) ──────
    if strong_bull_mom:
        buy_score  += 3; buy_reasons.append("Mom↑")
    elif strong_bear_mom:
        sell_score += 3; sell_reasons.append("Mom↓")

    if funding_favors_long:
        buy_score  += 2; buy_reasons.append("Funding←Long")
    elif funding_favors_short:
        sell_score += 2; sell_reasons.append("Funding←Short")

    # Open Interest onayı: OI yükseliyorsa mevcut trend güçlenir
    if oi_trend == "UP":
        if trend_5m == "UP":
            buy_score  += 2; buy_reasons.append("OI↑")
        elif trend_5m == "DOWN":
            sell_score += 2; sell_reasons.append("OI↑")
    elif oi_trend == "DOWN":
        # OI düşüyorsa trend zayıflıyor — her iki yönü de kısıt
        buy_score  = max(0, buy_score  - 2)
        sell_score = max(0, sell_score - 2)

    # Range piyasada breakout sinyallerine penaltı
    if regime == "RANGE":
        if breakout_up   and buy_score  > 0:
            buy_score  = max(0, buy_score  - 8)
        if breakout_down and sell_score > 0:
            sell_score = max(0, sell_score - 8)

    # ── Karar ────────────────────────────────────────────────────────────────
    if buy_score >= MIN_SIGNAL_SCORE and buy_score > sell_score:
        action     = "BUY"
        score      = buy_score
        confidence = min(buy_score / 100.0, 1.0)
        reasons    = buy_reasons
    elif sell_score >= MIN_SIGNAL_SCORE and sell_score > buy_score:
        action     = "SELL"
        score      = sell_score
        confidence = min(sell_score / 100.0, 1.0)
        reasons    = sell_reasons
    else:
        action     = "HOLD"
        score      = max(buy_score, sell_score)
        confidence = 0.0
        reasons    = []

    # ── SL/TP (ATR tabanlı) ───────────────────────────────────────────────────
    sl_dist = min(atr_val * SL_ATR_MULT, current_price * MAX_SL_PCT)
    tp_dist = min(atr_val * TP_ATR_MULT, current_price * MAX_TP_PCT)

    if action == "BUY":
        raw_sl = current_price - sl_dist
        sl = safe_stop_loss(current_price, "BUY",  LEV_STRONG, raw_sl)
        tp = round(current_price + tp_dist, 8)
    elif action == "SELL":
        raw_sl = current_price + sl_dist
        sl = safe_stop_loss(current_price, "SELL", LEV_STRONG, raw_sl)
        tp = round(current_price - tp_dist, 8)
    else:
        sl = tp = 0.0

    return Signal(
        symbol        = symbol,
        action        = action,
        confidence    = confidence,
        reason        = " | ".join(reasons) if reasons else "Sinyal yok",
        price         = current_price,
        rsi           = rsi,
        trend         = trend_5m,
        score         = score,
        atr           = atr_val,
        sl            = sl,
        tp            = tp,
        atr_pct       = atr_pct,
        btc_trend     = btc_trend,
        market_regime = regime,
    )


def analyze_all(
    symbols:        list[str],
    get_ohlcv:      Callable,
    get_price:      Callable,
    get_funding:    Callable,
    get_oi_trend:   Callable | None = None,
    btc_trend:      str = "SIDEWAYS",
    max_workers:    int = 20,
) -> list[Signal]:
    """Coinleri paralel analiz et, puana göre sıralanmış liste döndür."""

    def job(sym):
        df_e = get_ohlcv(sym, TF_ENTRY)
        if df_e is None or df_e.empty:
            return None
        df_t  = get_ohlcv(sym, TF_TREND)
        df_m  = get_ohlcv(sym, TF_MACRO)
        df_c  = get_ohlcv(sym, TF_CONTEXT)
        oi_t  = get_oi_trend(sym) if get_oi_trend else "FLAT"
        return _analyze_single(
            sym, df_e, df_t, df_m, df_c,
            get_price(sym),
            get_funding(sym),
            btc_trend,
            oi_t,
        )

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(job, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                sig = fut.result()
                if sig is not None:
                    results.append(sig)
            except Exception:
                pass

    results.sort(key=lambda s: s.confidence, reverse=True)
    return results
