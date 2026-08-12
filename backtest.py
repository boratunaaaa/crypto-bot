"""
Backtest / replay motoru.

KRİTİK TASARIM KARARI:
Bu motor, canlı botun kullandığı ÇIKIŞ MOTORUNUN TA KENDİSİNİ kullanır
(`position.evaluate`). Eski backtest basit SL/TP simüle ediyordu; yani canlıda
çalışan sistemden FARKLI bir sistemi ölçüyordu ve sonuçları anlamsızdı.
Artık kısmi çıkışlar, breakeven, Chandelier trailing ve time-stop birebir aynı
kodla simüle edilir.

Muhafazakâr varsayımlar (iyimser sonuç üretmemek için):
- Giriş, sinyalin oluştuğu mumun DEĞİL, bir sonraki mumun açılışından yapılır.
- Mum içi sıralama aleyhte varsayılır: BUY için önce low, sonra high görülür.
- Komisyon her açılış/kapanış/kısmi çıkışta notional üzerinden alınır.
- Slippage giriş ve çıkışta aleyhte uygulanır.
- Funding maliyeti 8 saatte bir tahakkuk ettirilir.

Kullanım:
  python3 backtest.py [symbol] [days]
  python3 backtest.py BTCUSDT 90
"""

import sys
import time
import statistics
from datetime import datetime, timedelta

import pandas as pd
from binance.client import Client

from config import (
    API_KEY, API_SECRET,
    ATR_PERIOD, SL_ATR_MULT, TP_ATR_MULT,
    MAX_SL_PCT, MAX_TP_PCT,
    MIN_ATR_PCT, MAX_ATR_PCT,
    MIN_SIGNAL_SCORE,
    COMMISSION_RATE, SLIPPAGE_PCT,
    RISK_PER_TRADE_PCT,
)
from risk_manager import calculate_leverage, calculate_position
from position import new_position, evaluate, realized_r
from analyzer import _analyze_single

# Funding 8 saatte bir, tipik oran. Gerçek geçmiş funding çekilmediğinde
# muhafazakâr bir sabit kullanılır (long pozisyonlar için maliyet).
DEFAULT_FUNDING_RATE_8H = 0.0001


def _calc_btc_trend(df_1h: pd.DataFrame) -> str:
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
    end_ms   = int(datetime.utcnow().timestamp() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=days + 5)).timestamp() * 1000)

    all_klines = []
    while start_ms < end_ms:
        klines = client.futures_klines(
            symbol=symbol, interval=interval,
            startTime=start_ms, endTime=end_ms, limit=1500,
        )
        if not klines:
            break
        all_klines.extend(klines)
        start_ms = int(klines[-1][6]) + 1
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
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def _intrabar_path(candle, side: str) -> list[float]:
    """
    Mum içi fiyat yolu — muhafazakâr sıralama.

    Gerçek sıra bilinmediği için ALEYHTE olan uç önce görülmüş kabul edilir.
    Bu, stop'ların erken tetiklenmesine ve kârın olduğundan düşük çıkmasına yol
    açar; tersi (iyimser sıralama) backtest'i sistematik olarak şişirirdi.
    """
    o, h, l, c = (float(candle["open"]), float(candle["high"]),
                  float(candle["low"]), float(candle["close"]))
    if side == "BUY":
        return [o, l, h, c]
    return [o, h, l, c]


def _apply_slippage(price: float, side: str, is_entry: bool) -> float:
    """Giriş ve çıkışta aleyhte kayma."""
    adverse = (side == "BUY") == is_entry
    return price * (1 + SLIPPAGE_PCT) if adverse else price * (1 - SLIPPAGE_PCT)


def run_backtest(
    symbol:          str   = "BTCUSDT",
    days:            int   = 60,
    initial_balance: float = 1000.0,
    verbose:         bool  = True,
    strategy:        str   = "TREND",
) -> dict:

    client = Client(API_KEY, API_SECRET)

    if verbose:
        print(f"\n{'='*58}")
        print(f"  BACKTEST: {symbol} | Son {days} gün | Strateji: {strategy}")
        print(f"  Başlangıç: {initial_balance:.2f} USDT")
        print(f"  Komisyon %{COMMISSION_RATE*100:.3f} | Slippage %{SLIPPAGE_PCT*100:.3f}")
        print(f"{'='*58}\n  Veriler çekiliyor...")

    df_5m = _fetch_historical(client, symbol, "5m", days)
    if len(df_5m) < 300:
        print("❌ Yeterli veri yok.")
        return {}

    if verbose:
        print(f"  {len(df_5m)} adet 5m mum yüklendi.")

    df_btc_1h = None
    if symbol != "BTCUSDT":
        df_btc_1h = _resample(_fetch_historical(client, "BTCUSDT", "5m", days), "1h")

    df_15m = _resample(df_5m, "15min")
    df_1h  = _resample(df_5m, "1h")
    df_4h  = _resample(df_5m, "4h")

    if verbose:
        print("  Simülasyon başlıyor...\n")

    balance  = initial_balance
    pos      = None
    trades   = []
    equity_curve = [initial_balance]
    WARMUP   = 200

    for i in range(WARMUP, len(df_5m) - 1):
        candle = df_5m.iloc[i]
        ts     = df_5m.index[i]

        # ── Açık pozisyon: canlı çıkış motoruyla değerlendir ─────────────────
        if pos is not None:
            # Funding maliyeti (8 saatte bir)
            hours_open = (ts - pos["open_time"]).total_seconds() / 3600
            funding_periods = int(hours_open // 8)
            if funding_periods > pos.get("_funding_charged", 0):
                notional = pos["qty"] * pos["entry_price"]
                cost = notional * DEFAULT_FUNDING_RATE_8H
                pos["_funding_cost"] = pos.get("_funding_cost", 0.0) + cost
                pos["_funding_charged"] = funding_periods
                balance -= cost

            closed_this_bar = False
            for px in _intrabar_path(candle, pos["side"]):
                actions = evaluate(pos, px, now=ts)
                for act in actions:
                    if act["type"] == "PARTIAL":
                        qty_close = min(pos["initial_qty"] * act["fraction"], pos["qty"])
                        fill = _apply_slippage(px, pos["side"], is_entry=False)
                        gross = ((fill - pos["entry_price"]) if pos["side"] == "BUY"
                                 else (pos["entry_price"] - fill)) * qty_close
                        comm = qty_close * fill * COMMISSION_RATE
                        net = gross - comm
                        margin_rel = pos["initial_margin"] * (qty_close / pos["initial_qty"])
                        pos["qty"]    -= qty_close
                        pos["margin"] -= margin_rel
                        pos["realized_pnl"]        += net
                        pos["realized_commission"] += comm
                        balance += margin_rel + net
                    elif act["type"] == "CLOSE":
                        fill = _apply_slippage(px, pos["side"], is_entry=False)
                        gross = ((fill - pos["entry_price"]) if pos["side"] == "BUY"
                                 else (pos["entry_price"] - fill)) * pos["qty"]
                        comm = pos["qty"] * fill * COMMISSION_RATE
                        net = gross - comm
                        balance += pos["margin"] + net

                        total_net  = net + pos["realized_pnl"] - pos.get("_funding_cost", 0.0)
                        total_comm = (comm + pos["realized_commission"]
                                      + pos.get("open_commission", 0.0))
                        trades.append({
                            "time": ts, "symbol": symbol, "side": pos["side"],
                            "entry": pos["entry_price"], "exit": fill,
                            "reason": act["reason"],
                            "gross": total_net + total_comm,
                            "comm": total_comm,
                            "funding": pos.get("_funding_cost", 0.0),
                            "net": total_net,
                            "r": realized_r(pos, total_net),
                            "mfe_r": pos["mfe_r"], "mae_r": pos["mae_r"],
                            "margin": pos["initial_margin"],
                            "leverage": pos["leverage"],
                            "duration_min": (ts - pos["open_time"]).total_seconds() / 60,
                            "strategy": pos.get("strategy", strategy),
                            "tp1": pos["tp1_done"], "tp2": pos["tp2_done"],
                        })
                        if verbose:
                            sign = "+" if total_net >= 0 else ""
                            print(f"  {ts.strftime('%m-%d %H:%M')}  {pos['side']:4s}  "
                                  f"{act['reason']:16s} {sign}{total_net:7.2f}$  "
                                  f"{trades[-1]['r']:+.2f}R  Bakiye:{balance:.2f}$")
                        pos = None
                        closed_this_bar = True
                        break
                if closed_this_bar:
                    break
            equity_curve.append(balance + (pos["margin"] if pos else 0))

        # ── Yeni sinyal ──────────────────────────────────────────────────────
        if pos is None:
            slice_5m  = df_5m.iloc[max(0, i-200):i+1]
            slice_15m = df_15m[df_15m.index <= ts].iloc[-120:] if len(df_15m) > 0 else None
            slice_1h  = df_1h[df_1h.index  <= ts].iloc[-120:] if len(df_1h)  > 0 else None
            slice_4h  = df_4h[df_4h.index  <= ts].iloc[-120:] if len(df_4h)  > 0 else None

            if (slice_15m is None or len(slice_15m) < 30 or
                slice_1h  is None or len(slice_1h)  < 20 or
                slice_4h  is None or len(slice_4h)  < 5):
                continue

            try:
                btc_slice = (slice_1h if symbol == "BTCUSDT" else
                             (df_btc_1h[df_btc_1h.index <= ts].iloc[-120:]
                              if df_btc_1h is not None and len(df_btc_1h) > 0 else None))
                sig = _analyze_single(
                    symbol, slice_5m, slice_15m, slice_1h, slice_4h,
                    price=float(candle["close"]), funding=0.0,
                    btc_trend=_calc_btc_trend(btc_slice), oi_trend="FLAT",
                )
            except Exception:
                continue

            if sig.action == "HOLD" or sig.score < MIN_SIGNAL_SCORE:
                continue

            entry_raw = float(df_5m.iloc[i+1]["open"])
            entry_price = _apply_slippage(entry_raw, sig.action, is_entry=True)

            sl_dist = abs(sig.price - sig.sl)
            if sl_dist <= 0:
                continue
            real_sl = (entry_price - sl_dist) if sig.action == "BUY" else (entry_price + sl_dist)
            real_tp = (entry_price + abs(sig.price - sig.tp)) if sig.action == "BUY" \
                      else (entry_price - abs(sig.price - sig.tp))

            leverage = calculate_leverage(sig.confidence, sig.score)
            margin, qty, risk_amount, err = calculate_position(
                balance, leverage, entry_price, real_sl, RISK_PER_TRADE_PCT
            )
            if err or margin > balance:
                continue

            comm_open = qty * entry_price * COMMISSION_RATE
            balance -= margin + comm_open

            pos = new_position(
                symbol=symbol, side=sig.action, entry_price=entry_price,
                qty=qty, margin=margin, leverage=leverage,
                stop_loss=real_sl, take_profit=real_tp,
                atr_pct=sig.atr_pct or 0.01, strategy=strategy,
                open_time=ts, open_commission=comm_open,
            )
            pos["risk_amount"] = risk_amount
            pos["_funding_cost"] = 0.0
            pos["_funding_charged"] = 0

    if not trades:
        print("⚠️  Hiç işlem gerçekleşmedi.")
        return {}

    return _build_report(symbol, days, initial_balance, balance, trades,
                         equity_curve, verbose)


def _build_report(symbol, days, initial_balance, final_balance, trades,
                  equity_curve, verbose) -> dict:
    wins   = [t for t in trades if t["net"] >= 0]
    losses = [t for t in trades if t["net"] <  0]
    net_pnl    = sum(t["net"]   for t in trades)
    gross_pnl  = sum(t["gross"] for t in trades)
    total_comm = sum(t["comm"]  for t in trades)
    total_fund = sum(t["funding"] for t in trades)
    win_rate   = len(wins) / len(trades)

    gross_wins = sum(t["net"] for t in wins) if wins else 0.0
    gross_loss = abs(sum(t["net"] for t in losses)) if losses else 0.001
    profit_factor = gross_wins / gross_loss

    peak = initial_balance
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0)

    rs = [t["r"] for t in trades]
    expectancy_r = sum(rs) / len(rs)
    try:
        sharpe_like = (statistics.mean(rs) / statistics.stdev(rs)) if len(rs) > 1 and statistics.stdev(rs) > 0 else 0.0
    except statistics.StatisticsError:
        sharpe_like = 0.0

    # En büyük 2 kazanan hariç — kâr birkaç şanslı işleme mi bağlı?
    ranked = sorted(trades, key=lambda t: t["net"], reverse=True)
    net_ex_top2 = sum(t["net"] for t in ranked[2:])

    by_reason: dict[str, dict] = {}
    for t in trades:
        d = by_reason.setdefault(t["reason"], {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t["net"]

    by_strategy: dict[str, dict] = {}
    for t in trades:
        d = by_strategy.setdefault(t["strategy"], {"n": 0, "net": 0.0, "wins": 0})
        d["n"] += 1
        d["net"] += t["net"]
        d["wins"] += 1 if t["net"] >= 0 else 0

    report = {
        "symbol": symbol, "days": days,
        "initial": initial_balance, "final": final_balance,
        "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate,
        "gross_pnl": gross_pnl, "commission": total_comm, "funding": total_fund,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor, "max_drawdown": max_dd,
        "avg_win":  (sum(t["net"] for t in wins)/len(wins)) if wins else 0,
        "avg_loss": (sum(t["net"] for t in losses)/len(losses)) if losses else 0,
        "max_win":  max(t["net"] for t in trades),
        "max_loss": min(t["net"] for t in trades),
        "expectancy_r": expectancy_r,
        "sharpe_like": sharpe_like,
        "net_ex_top2": net_ex_top2,
        "avg_mfe_r": sum(t["mfe_r"] for t in trades) / len(trades),
        "avg_mae_r": sum(t["mae_r"] for t in trades) / len(trades),
        "r_distribution": rs,
        "by_reason": by_reason,
        "by_strategy": by_strategy,
        "trades": trades,
    }

    if verbose:
        _print_report(report)
    return report


def _print_report(r: dict):
    sep = "─" * 58
    pf_ok = "✅" if r["profit_factor"] >= 1.3 else ("⚠️" if r["profit_factor"] >= 1.0 else "❌")
    ex_ok = "✅" if r["net_ex_top2"] > 0 else "❌"
    exp_ok= "✅" if r["expectancy_r"] > 0.1 else ("⚠️" if r["expectancy_r"] > 0 else "❌")

    print(f"\n{sep}")
    print(f"  SONUÇ — {r['symbol']} / {r['days']} gün")
    print(sep)
    print(f"  Toplam işlem   : {r['total_trades']}  ({r['wins']}W / {r['losses']}L)")
    print(f"  Win rate       : %{r['win_rate']*100:.1f}")
    print(f"  Net PnL        : {r['net_pnl']:+.2f}$  ({(r['final']/r['initial']-1)*100:+.1f}%)")
    print(f"  Komisyon       : -{r['commission']:.2f}$")
    print(f"  Funding        : -{r['funding']:.2f}$")
    print(f"  Profit factor  : {pf_ok} {r['profit_factor']:.2f}")
    print(f"  Beklenti       : {exp_ok} {r['expectancy_r']:+.3f}R / işlem")
    print(f"  Sharpe benzeri : {r['sharpe_like']:.2f}")
    print(f"  Max drawdown   : %{r['max_drawdown']*100:.1f}")
    print(f"  Ort. MFE / MAE : {r['avg_mfe_r']:+.2f}R / {r['avg_mae_r']:+.2f}R")
    print(sep)
    print(f"  En iyi 2 hariç : {ex_ok} {r['net_ex_top2']:+.2f}$")
    print(f"    (gerçek paraya geçiş kapısı — bu değer pozitif olmalı)")
    print(sep)
    print("  Çıkış nedenine göre:")
    for reason, d in sorted(r["by_reason"].items(), key=lambda x: -x[1]["net"]):
        print(f"    {reason:18s} {d['n']:4d} işlem  {d['net']:+9.2f}$")
    print(sep)
    print("  R dağılımı:")
    buckets = [("<-1R", 0), ("-1..0R", 0), ("0..1R", 0), ("1..2R", 0),
               ("2..3R", 0), (">3R", 0)]
    for x in r["r_distribution"]:
        if   x < -1: buckets[0] = (buckets[0][0], buckets[0][1] + 1)
        elif x < 0:  buckets[1] = (buckets[1][0], buckets[1][1] + 1)
        elif x < 1:  buckets[2] = (buckets[2][0], buckets[2][1] + 1)
        elif x < 2:  buckets[3] = (buckets[3][0], buckets[3][1] + 1)
        elif x < 3:  buckets[4] = (buckets[4][0], buckets[4][1] + 1)
        else:        buckets[5] = (buckets[5][0], buckets[5][1] + 1)
    for label, n in buckets:
        bar = "█" * min(n, 40)
        print(f"    {label:8s} {n:4d}  {bar}")
    print(f"{sep}\n")


def monte_carlo(report: dict, simulations: int = 10_000, verbose: bool = True) -> dict:
    """İşlem sırasını karıştırarak drawdown dağılımını tahmin et."""
    import random
    rs = report.get("r_distribution", [])
    if not rs:
        return {}
    worst_dds = []
    finals = []
    for _ in range(simulations):
        seq = rs[:]
        random.shuffle(seq)
        eq = 0.0; peak = 0.0; dd = 0.0
        for x in seq:
            eq += x
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        worst_dds.append(dd)
        finals.append(eq)
    worst_dds.sort()
    out = {
        "median_dd_r": worst_dds[len(worst_dds)//2],
        "p95_dd_r":    worst_dds[int(len(worst_dds)*0.95)],
        "median_final_r": sorted(finals)[len(finals)//2],
        "prob_negative": sum(1 for f in finals if f < 0) / len(finals),
    }
    if verbose:
        print(f"  Monte Carlo ({simulations} simülasyon):")
        print(f"    Medyan drawdown : {out['median_dd_r']:.1f}R")
        print(f"    %95 drawdown    : {out['p95_dd_r']:.1f}R")
        print(f"    Negatif bitirme : %{out['prob_negative']*100:.1f}")
    return out


if __name__ == "__main__":
    sym  = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    dys  = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    rep = run_backtest(sym, dys)
    if rep:
        monte_carlo(rep)
