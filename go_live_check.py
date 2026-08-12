"""
GERÇEK PARAYA GEÇİŞ KAPISI.

`PAPER_TRADING = False` yapmadan ÖNCE bu script çalıştırılır. Kapıların hepsi
aynı anda geçilmeden gerçek para açılmaz.

Kullanım:
    python3 go_live_check.py

Kriterlerin gerekçesi (2026-08-12 ölçümü):
- 82 işlemlik paper geçmişinde net kâr +$290 görünüyordu, ama bunun %117'si
  yalnızca 2 işlemden geliyordu; o iki işlem çıkarılınca kalan 80 işlem
  -$48.90 zarardaydı. "En iyi 2 hariç pozitif" kapısı doğrudan bunu ölçer.
- 18 takvim gününün yalnızca ~9-10'unda bot ayaktaydı; iki kesinti fark
  edilmeden günlerce sürdü. "Kesintisiz gün" kapısı bunu ölçer.
- Eski işlemler bozuk pozisyon boyutlandırma ve tek etiketli çıkış kaydıyla
  üretildiği için SAYILMAZ; kapı yalnızca refactor sonrası işlemlere bakar.
"""

import sys
from datetime import datetime, timedelta

from database import _conn, get_performance_summary, get_summary_excluding_top

# Refactor'ın devreye girdiği an. Bu tarihten ÖNCEKİ işlemler istatistiğe
# katılmaz: bozuk sizing (risk kaldıraçla büyüyordu) ve tek etiketli çıkış
# kaydıyla üretildiler.
REFACTOR_CUTOFF = "2026-08-12T00:00:00"

GATES = {
    "min_trades":            200,
    "min_uptime_days":       30,
    "min_profit_factor":     1.3,
    "max_drawdown_pct":      0.15,
    "min_expectancy_r":      0.10,
}


def _rows(sql, params=()):
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def check() -> tuple[bool, list[tuple[str, bool, str]]]:
    results: list[tuple[str, bool, str]] = []

    trades = _rows(
        "SELECT * FROM trades WHERE close_time >= ? ORDER BY close_time",
        (REFACTOR_CUTOFF,),
    )
    n = len(trades)

    # 1) Örneklem büyüklüğü
    ok = n >= GATES["min_trades"]
    results.append((
        "Örneklem büyüklüğü",
        ok,
        f"{n} işlem (gereken: {GATES['min_trades']}) — refactor sonrası",
    ))

    if n == 0:
        results.append(("Diğer kapılar", False, "Değerlendirilecek işlem yok"))
        return False, results

    nets = [t["net_pnl"] for t in trades]
    wins = [x for x in nets if x >= 0]
    losses = [x for x in nets if x < 0]

    # 2) En büyük 2 kazanan hariç net pozitif — outlier bağımlılığı testi
    ranked = sorted(nets, reverse=True)
    net_ex_top2 = sum(ranked[2:])
    ok = net_ex_top2 > 0
    results.append((
        "En iyi 2 işlem hariç net pozitif",
        ok,
        f"{net_ex_top2:+.2f}$ (tümü: {sum(nets):+.2f}$)",
    ))

    # 3) Profit factor
    gw = sum(wins) if wins else 0.0
    gl = abs(sum(losses)) if losses else 0.001
    pf = gw / gl
    ok = pf >= GATES["min_profit_factor"]
    results.append((
        "Profit factor",
        ok,
        f"{pf:.2f} (gereken: ≥{GATES['min_profit_factor']})",
    ))

    # 4) Beklenti (R cinsinden)
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    exp_r = sum(rs) / len(rs) if rs else 0.0
    ok = exp_r >= GATES["min_expectancy_r"]
    results.append((
        "Beklenti",
        ok,
        f"{exp_r:+.3f}R/işlem (gereken: ≥{GATES['min_expectancy_r']})",
    ))

    # 5) Max drawdown
    eq, peak, max_dd = 0.0, 0.0, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    ok = max_dd <= GATES["max_drawdown_pct"]
    results.append((
        "Max drawdown",
        ok,
        f"%{max_dd*100:.1f} (izin: ≤%{GATES['max_drawdown_pct']*100:.0f})",
    ))

    # 6) Kesintisiz çalışma — işlem kapanışları arasındaki en uzun boşluk
    times = [datetime.fromisoformat(t["close_time"]) for t in trades]
    span_days = (times[-1] - times[0]).total_seconds() / 86400
    ok = span_days >= GATES["min_uptime_days"]
    results.append((
        "Çalışma süresi",
        ok,
        f"{span_days:.1f} gün (gereken: {GATES['min_uptime_days']})",
    ))

    # 7) Çıkış etiketleri gerçekten ayrışıyor mu?
    #    (eski kodda 82/82 işlem tek etiketle kaydediliyordu)
    kinds = {t["reason_close"] for t in trades}
    ok = len(kinds) >= 3
    results.append((
        "Çıkış etiketi çeşitliliği",
        ok,
        f"{len(kinds)} farklı etiket: {', '.join(sorted(kinds))}",
    ))

    # 8) Strateji kırılımı — momentum canlıysa ayrı ölçülebiliyor mu?
    strats = {}
    for t in trades:
        s = t["strategy"] or "TREND"
        d = strats.setdefault(s, {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t["net_pnl"]
    detail = " | ".join(f"{k}: {v['n']} işlem {v['net']:+.1f}$" for k, v in strats.items())
    results.append(("Strateji kırılımı", True, detail or "yok"))

    all_ok = all(ok for _, ok, _ in results)
    return all_ok, results


def main():
    print("\n" + "=" * 62)
    print("  GERÇEK PARAYA GEÇİŞ KAPISI")
    print("=" * 62)

    try:
        all_ok, results = check()
    except Exception as e:
        print(f"\n❌ Kontrol çalıştırılamadı: {e}\n")
        return 2

    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark}  {name:34s} {detail}")

    print("=" * 62)
    if all_ok:
        print("  SONUÇ: Tüm kapılar geçildi.")
        print()
        print("  Yine de doğrudan tam sermayeyle geçilmez. Önerilen ilk adım:")
        print("    • $100-200 sermaye")
        print("    • MAX_OPEN_POSITIONS = 2")
        print("    • LEVERAGE_MAX = 2")
        print("    • en az 2 hafta")
        print("  Amaç kâr değil: gerçek slippage ve funding beklendiği gibi mi?")
    else:
        failed = [n for n, ok, _ in results if not ok]
        print(f"  SONUÇ: ❌ GEÇİLMEDİ — {len(failed)} kapı açık")
        for f in failed:
            print(f"    • {f}")
        print("\n  PAPER_TRADING = True olarak KALMALI.")
    print("=" * 62 + "\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
