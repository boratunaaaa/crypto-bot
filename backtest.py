"""
Backtest motoru — geçmiş verilerle strateji testi.

Kullanım:
  python3 backtest.py [symbol] [days]

Örnekler:
  python3 backtest.py BTCUSDT 90
  python3 backtest.py ETHUSDT 30
  python3 backtest.py          ← varsayılan: BTCUSDT, 60 gün
"""

import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import pandas_ta as ta
from binance.client import Client

# Config yükle
from config import (
    API_KEY, API_SECRET,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BB_PERIOD, BB_STD, BB_STD,
    ATR_PERIOD, SL_ATR_MULT, TP_ATR_MULT,
    MAX_SL_PCT, MAX_TP_PCT,
    MIN_ATR_PCT, MAX_ATR_PCT,
    MIN_SIGNAL_SCORE,
    COMMISSION_RATE,
    ADX_PERIOD, ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
    RISK_PER_TRADE_PCT, MAX_POSITION_PCT,
)
from risk_manager import calculate_leverage
from analyzer import _analyze_single, _trend_from_df, _market_regime


def _calc_btc_trend(df_1h: pd.DataFrame) -> str:
    """1h diliminden BTC trend yönünü hesapla (EMA 8/21/50 hizalaması)."""
    try:
        if df_1h is None or len(df_1h) < 55:
            return "SIDEWAYS"
        close = df_1h["close"]
        e8  = close.ewm(span=8,  adjust=False).mean().iloc[-1]
        e21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        if e8 > e21 > e50:
            return "UP"
        if e8 < e21 < e50:
            return "DOWN"
        return "SIDEWAYS"
    except Exception:
        return "SIDEWAYS"


def _fetch_historical(client: Client, symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Tarihsel OHLCV verisini çek (pagination ile)."""
    end_ms   = int(datetime.utcnow().timestamp() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=days + 5)).timestamp() * 1000)

    all_klines = []
    while start_ms < end_ms:
        klines = client.futures_klines(
            symbol=symbol, interval=interval,
            startTime=start_ms, endTime=end_ms,
            limit=1500,
        )
        if not klines:
            break
        all_klines.extend(klines)
        start_ms = int(klines[-1][6]) + 1   # son kapanış zamanı + 1ms
        if len(klines) < 1500:
            break
        time.sleep(0.1)

    df = pd.DataFrame(all_klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore",
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df[["open","high","low","close","volume"]].copy()


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """5m veriyi istenilen timeframe'e yeniden örnekle."""
    resampled = df.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    return resampled


def run_backtest(
    symbol:          str   = "BTCUSDT",
    days:            int   = 60,
    initial_balance: float = 1000.0,
    verbose:         bool  = True,
) -> dict:

    client = Client(API_KEY, API_SECRET)

    if verbose:
        print(f"\n{'='*55}")
        print(f"  BACKTEST: {symbol} | Son {days} gün")
        print(f"  Başlangıç bakiyesi: {initial_balance:.2f} USDT")
        print(f"{'='*55}")
        print("  Veriler çekiliyor...")

    df_5m = _fetch_historical(client, symbol, "5m", days)
    if len(df_5m) < 300:
        print("❌ Yeterli veri yok.")
        return {}

    if verbose:
        print(f"  {len(df_5m)} adet 5m mum yüklendi.")

    # BTC verisi — altcoin backtestlerinde gerçek BTC trendi için
    df_btc_1h = None
    if symbol != "BTCUSDT":
        if verbose:
            print("  BTC 1h verisi çekiliyor...")
        df_btc_raw  = _fetch_historical(client, "BTCUSDT", "5m", days)
        df_btc_1h   = _resample(df_btc_raw, "1h")

    if verbose:
        print("  Simülasyon başlıyor...\n")

    # Diğer TF'leri yeniden örnekle
    df_15m = _resample(df_5m, "15min")
    df_1h  = _resample(df_5m, "1h")
    df_4h  = _resample(df_5m, "4h")

    # ── Simülasyon ────────────────────────────────────────────────────────────
    balance    = initial_balance
    position   = None   # {'side', 'entry', 'sl', 'tp', 'margin', 'leverage', 'qty'}
    trades     = []
    WARMUP     = 200    # indikatör ısınması için minimum mum sayısı

    for i in range(WARMUP, len(df_5m)):
        candle = df_5m.iloc[i]
        ts     = df_5m.index[i]

        # ── Açık pozisyon SL/TP kontrolü ──────────────────────────────────
        if position is not None:
            hi, lo = candle["high"], candle["low"]
            hit = reason = None

            if position["side"] == "BUY":
                if lo <= position["sl"]:
                    hit = position["sl"]; reason = "STOP_LOSS"
                elif hi >= position["tp"]:
                    hit = position["tp"]; reason = "TAKE_PROFIT"
            else:
                if hi >= position["sl"]:
                    hit = position["sl"]; reason = "STOP_LOSS"
                elif lo <= position["tp"]:
                    hit = position["tp"]; reason = "TAKE_PROFIT"

            if hit and reason:
                gross = ((hit - position["entry"]) if position["side"] == "BUY"
                         else (position["entry"] - hit)) * position["qty"]
                comm  = (position["margin"] * position["leverage"]) * COMMISSION_RATE * 2
                net   = gross - comm
                balance += position["margin"] + net
                trades.append({
                    "time":    ts,
                    "symbol":  symbol,
                    "side":    position["side"],
                    "entry":   position["entry"],
                    "exit":    hit,
                    "reason":  reason,
                    "gross":   gross,
                    "comm":    comm,
                    "net":     net,
                    "margin":  position["margin"],
                    "leverage":position["leverage"],
                })
                if verbose:
                    sign = "+" if net >= 0 else ""
                    print(f"  {ts.strftime('%m-%d %H:%M')}  {position['side']:5s}  "
                          f"{reason:13s}  {sign}{net:.2f}$  "
                          f"Bakiye: {balance:.2f}$")
                position = None

        # ── Yeni sinyal üret (açık pozisyon yoksa) ───────────────────────
        if position is None:
            slice_5m  = df_5m.iloc[max(0, i-200):i+1]

            # Eşleşen diğer TF dilimleri
            slice_15m = df_15m[df_15m.index <= ts].iloc[-120:] if len(df_15m) > 0 else None
            slice_1h  = df_1h[df_1h.index  <= ts].iloc[-120:] if len(df_1h)  > 0 else None
            slice_4h  = df_4h[df_4h.index  <= ts].iloc[-120:] if len(df_4h)  > 0 else None

            if (slice_15m is None or len(slice_15m) < 30 or
                slice_1h  is None or len(slice_1h)  < 20 or
                slice_4h  is None or len(slice_4h)  < 5):
                continue

            try:
                # BTC trendini gerçek BTC verisinden hesapla
                if symbol == "BTCUSDT":
                    btc_slice = slice_1h
                else:
                    btc_slice = (df_btc_1h[df_btc_1h.index <= ts].iloc[-120:]
                                 if df_btc_1h is not None and len(df_btc_1h) > 0
                                 else None)
                btc_trend_bt = _calc_btc_trend(btc_slice)

                sig = _analyze_single(
                    symbol,
                    slice_5m, slice_15m, slice_1h, slice_4h,
                    price=float(candle["close"]),
                    funding=0.0,
                    btc_trend=btc_trend_bt,
                    oi_trend="FLAT",
                )
            except Exception:
                continue

            if sig.action == "HOLD" or sig.score < MIN_SIGNAL_SCORE:
                continue

            # Risk-based position sizing
            sl_pct  = abs(sig.price - sig.sl) / sig.price if sig.sl else 0
            if sl_pct < 0.001:
                continue
            leverage   = calculate_leverage(sig.confidence)
            risk_amt   = balance * RISK_PER_TRADE_PCT
            notional   = risk_amt / sl_pct
            margin     = min(notional / leverage, balance * MAX_POSITION_PCT)
            margin     = round(margin, 2)
            if margin < 1.0 or margin > balance:
                continue

            # Giriş fiyatı = bir sonraki mumun açılışı (gerçekçi)
            if i + 1 >= len(df_5m):
                continue
            entry_price = float(df_5m.iloc[i+1]["open"])

            # SL/TP'yi gerçek giriş fiyatına göre yeniden ölçeklendir
            sl_dist = abs(sig.price - sig.sl)
            tp_dist = abs(sig.price - sig.tp)
            if sig.action == "BUY":
                real_sl = entry_price - sl_dist
                real_tp = entry_price + tp_dist
            else:
                real_sl = entry_price + sl_dist
                real_tp = entry_price - tp_dist

            # Komisyon giriş
            comm_open = (margin * leverage) * COMMISSION_RATE
            balance  -= margin + comm_open

            qty = (margin * leverage) / entry_price
            position = {
                "side":     sig.action,
                "entry":    entry_price,
                "sl":       real_sl,
                "tp":       real_tp,
                "margin":   margin,
                "leverage": leverage,
                "qty":      qty,
            }

    # ── Sonuç hesapla ─────────────────────────────────────────────────────────
    if not trades:
        print("⚠️  Hiç işlem gerçekleşmedi.")
        return {}

    wins   = [t for t in trades if t["net"] >= 0]
    losses = [t for t in trades if t["net"] <  0]
    net_pnl    = sum(t["net"]   for t in trades)
    gross_pnl  = sum(t["gross"] for t in trades)
    total_comm = sum(t["comm"]  for t in trades)
    win_rate   = len(wins) / len(trades)

    gross_wins  = sum(t["gross"] for t in wins)   if wins   else 0
    gross_loss  = abs(sum(t["gross"] for t in losses)) if losses else 0.001
    profit_factor = gross_wins / gross_loss

    # Max drawdown
    running_balance = initial_balance
    peak    = initial_balance
    max_dd  = 0.0
    for t in trades:
        running_balance += t["net"]
        peak = max(peak, running_balance)
        dd   = (peak - running_balance) / peak
        max_dd = max(max_dd, dd)

    avg_win  = sum(t["net"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(t["net"] for t in losses) / len(losses) if losses else 0

    report = {
        "symbol":        symbol,
        "days":          days,
        "initial":       initial_balance,
        "final":         initial_balance + net_pnl,
        "total_trades":  len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      win_rate,
        "gross_pnl":     gross_pnl,
        "commission":    total_comm,
        "net_pnl":       net_pnl,
        "profit_factor": profit_factor,
        "max_drawdown":  max_dd,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "max_win":       max(t["net"] for t in trades),
        "max_loss":      min(t["net"] for t in trades),
    }

    if verbose:
        _print_report(report)

    return report


def _print_report(r: dict):
    sep = "─" * 45
    pf_ok = "✅" if r["profit_factor"] >= 1.3 else ("⚠️" if r["profit_factor"] >= 1.0 else "❌")
    wr_ok = "✅" if r["win_rate"] >= 0.55 else ("⚠️" if r["win_rate"] >= 0.45 else "❌")
    net_c = "+" if r["net_pnl"] >= 0 else ""

    print(f"\n{'='*55}")
    print(f"  BACKTEST RAPORU — {r['symbol']} | Son {r['days']} Gün")
    print(f"{'='*55}")
    print(f"  Başlangıç Bakiye : {r['initial']:.2f} USDT")
    print(f"  Final Bakiye     : {r['final']:.2f} USDT")
    print(sep)
    print(f"  Toplam İşlem     : {r['total_trades']}")
    print(f"  Kazanan          : {r['wins']}  |  Kaybeden: {r['losses']}")
    print(f"  Win Rate         : {wr_ok} {r['win_rate']*100:.1f}%")
    print(sep)
    print(f"  Brüt PnL         : {r['gross_pnl']:+.2f}$")
    print(f"  Komisyon         : -{r['commission']:.2f}$")
    print(f"  Net PnL          : {net_c}{r['net_pnl']:.2f}$")
    print(sep)
    print(f"  Profit Factor    : {pf_ok} {r['profit_factor']:.2f}")
    print(f"  Max Drawdown     : {r['max_drawdown']*100:.1f}%")
    print(f"  Ort. Kazanç      : +{r['avg_win']:.2f}$")
    print(f"  Ort. Kayıp       : {r['avg_loss']:.2f}$")
    print(f"  En İyi İşlem     : +{r['max_win']:.2f}$")
    print(f"  En Kötü İşlem    : {r['max_loss']:.2f}$")
    print(f"{'='*55}")

    # Canlıya geçiş kriterleri
    print("\n  📋 CANLI GEÇİŞ DEĞERLENDİRMESİ:")
    checks = [
        (r["total_trades"] >= 50,         f"En az 50 işlem ({r['total_trades']})"),
        (r["profit_factor"] >= 1.3,       f"Profit Factor ≥ 1.3 ({r['profit_factor']:.2f})"),
        (r["win_rate"] >= 0.50,           f"Win Rate ≥ 50% ({r['win_rate']*100:.1f}%)"),
        (r["net_pnl"] > 0,               f"Net PnL pozitif ({r['net_pnl']:+.2f}$)"),
        (r["max_drawdown"] < 0.15,       f"Max Drawdown < 15% ({r['max_drawdown']*100:.1f}%)"),
    ]
    all_pass = True
    for ok, desc in checks:
        icon = "✅" if ok else "❌"
        print(f"    {icon}  {desc}")
        if not ok:
            all_pass = False

    verdict = "🟢 CANLI MOD İÇİN UYGUN" if all_pass else "🔴 HENÜZ CANLI MODA GEÇME"
    print(f"\n  Sonuç: {verdict}\n")


def monte_carlo(report: dict, simulations: int = 10_000, verbose: bool = True) -> dict:
    """
    Geçmiş trade sonuçlarından 10,000 rastgele senaryo üret.
    Ruin (hesap %50 düşüşü) ve beklenen getiri dağılımını hesapla.
    """
    import random
    if not report:
        return {}

    wins   = report["wins"]
    losses = report["losses"]
    total  = report["total_trades"]
    if total == 0:
        return {}

    avg_win  = report["avg_win"]
    avg_loss = report["avg_loss"]   # negatif sayı
    initial  = report["initial"]
    win_rate = wins / total

    results = []
    ruin_count = 0
    RUIN_THRESHOLD = 0.50   # başlangıcın %50'si altına düşerse "ruin"

    for _ in range(simulations):
        balance = initial
        peak    = initial
        max_dd  = 0.0
        for _ in range(total):
            if random.random() < win_rate:
                balance += avg_win
            else:
                balance += avg_loss   # avg_loss zaten negatif
            peak   = max(peak, balance)
            dd     = (peak - balance) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        results.append(balance)
        if balance < initial * RUIN_THRESHOLD:
            ruin_count += 1

    results.sort()
    n = len(results)
    p5   = results[int(n * 0.05)]
    p25  = results[int(n * 0.25)]
    p50  = results[int(n * 0.50)]
    p75  = results[int(n * 0.75)]
    p95  = results[int(n * 0.95)]
    ruin_pct = ruin_count / simulations * 100

    mc = {
        "simulations": simulations,
        "p5":   p5,  "p25":  p25,  "p50":  p50,
        "p75":  p75, "p95":  p95,
        "ruin_pct": ruin_pct,
        "avg_result": sum(results) / n,
    }

    if verbose:
        sign = lambda x: "+" if x >= initial else ""
        ok   = "✅" if ruin_pct < 10 else ("⚠️" if ruin_pct < 25 else "❌")
        print(f"\n  🎲 MONTE CARLO ({simulations:,} senaryo)")
        print(f"  {'─'*43}")
        print(f"  En kötü  %5  : {sign(p5)}{p5-initial:.2f}$  ({p5:.2f} USDT)")
        print(f"  Alt çeyrek    : {sign(p25)}{p25-initial:.2f}$  ({p25:.2f} USDT)")
        print(f"  Medyan        : {sign(p50)}{p50-initial:.2f}$  ({p50:.2f} USDT)")
        print(f"  Üst çeyrek   : {sign(p75)}{p75-initial:.2f}$  ({p75:.2f} USDT)")
        print(f"  En iyi  %95  : {sign(p95)}{p95-initial:.2f}$  ({p95:.2f} USDT)")
        print(f"  {'─'*43}")
        print(f"  Ruin riski    : {ok} %{ruin_pct:.1f}  (bakiye yarıya düşme)")
        print(f"  Beklenen ort. : {sign(mc['avg_result'])}{mc['avg_result']-initial:.2f}$\n")

    return mc


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days   = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    report = run_backtest(symbol=symbol, days=days)
    if report:
        monte_carlo(report)
