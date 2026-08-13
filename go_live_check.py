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
    # "30 gün aralık" tek başına kesintisizlik demek değil — aktif gün ve
    # en uzun sessiz boşluk ayrıca ölçülür.
    "min_active_days":       20,
    "max_silent_gap_days":   2.0,
    # Gerçek çalışma kanıtı: equity snapshot akışındaki en uzun kesinti.
    "max_heartbeat_gap_hours": 2.0,
}


def _starting_equity() -> float:
    """
    KESİM ANINDAKİ sermaye.

    DENETİM BULGUSU: burada `bot_state.paper_balance` okunuyordu — o GÜNCEL
    bakiye, kesim anındaki değil. Bugün 1200$ olan bir hesap için, 1000$'dan
    başlayıp 1200$'a çıkmış bir eğrinin drawdown'ı yanlış tabana bölünüyordu.
    Doğrusu, kesimden sonraki İLK equity snapshot'ı.
    """
    from config import PAPER_BALANCE
    try:
        rows = _rows(
            "SELECT equity FROM equity_snapshots WHERE at >= ? ORDER BY at LIMIT 1",
            (REFACTOR_CUTOFF,),
        )
        if rows:
            return float(rows[0]["equity"])
    except Exception:
        pass
    return float(PAPER_BALANCE)


def _heartbeat_uptime() -> tuple[float, float]:
    """
    Gerçek çalışma süresi — equity snapshot akışından.

    Snapshot dakikada bir yazılır ve süreç ölünce durur. İşlem aralıklarının
    aksine, "bot ayaktaydı ama sinyal bulamadı" ile "bot kapalıydı" ayrımını
    yapabilen tek kayıt budur.

    Dönüş: (kapsanan gün, en uzun kesinti saati)
    """
    from datetime import datetime as _dt
    try:
        rows = _rows(
            "SELECT at FROM equity_snapshots WHERE at >= ? ORDER BY at",
            (REFACTOR_CUTOFF,),
        )
    except Exception:
        return 0.0, float("inf")
    if len(rows) < 2:
        return 0.0, float("inf")
    ts = [_dt.fromisoformat(r["at"]) for r in rows]
    span = (ts[-1] - ts[0]).total_seconds() / 86400
    gap = max((b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:]))
    return span, gap


def _snapshot_drawdown() -> float | None:
    """
    Periyodik equity snapshot'larından GERÇEK drawdown.

    Kapanmış işlem eğrisi açık pozisyonun anlık zararını göremez: -3R'ye inip
    +1R'de kapanan bir işlem kayıtta sıfır drawdown'lı bir kazançtır. Gerçek
    ölçüm ancak düzenli equity örneklemesiyle yapılabilir.
    """
    try:
        rows = _rows(
            "SELECT equity FROM equity_snapshots WHERE at >= ? ORDER BY at",
            (REFACTOR_CUTOFF,),
        )
    except Exception:
        return None
    if len(rows) < 50:
        return None
    peak = dd = 0.0
    for r in rows:
        e = float(r["equity"])
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak)
    return dd


def _rows(sql, params=()):
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def check() -> tuple[bool, list[tuple[str, bool, str]]]:
    results: list[tuple[str, bool, str]] = []

    all_rows = _rows(
        "SELECT * FROM trades WHERE close_time >= ? ORDER BY close_time",
        (REFACTOR_CUTOFF,),
    )
    # DENETİM BULGUSU: bütün satırlar ayrımsız örnekleme katılıyordu. Kapanış
    # PnL'i borsadan doğrulanamayan işlemlerde MUHAFAZAKÂR TAHMİN yazılır;
    # bunları gerçek ölçüm gibi saymak hem 200 işlem hedefini şişirir hem de
    # profit factor / beklentiyi uydurma sayılarla kirletir. Gerçek paraya
    # geçiş kararı yalnız DOĞRULANMIŞ işlemlere dayanmalıdır.
    def _verified(t):
        v = t.get("pnl_verified")
        return v is None or int(v) == 1        # eski satırlarda kolon yok
    trades = [t for t in all_rows if _verified(t)]
    unverified = len(all_rows) - len(trades)
    n = len(trades)

    # 1) Örneklem büyüklüğü
    ok = n >= GATES["min_trades"]
    detail = f"{n} işlem (gereken: {GATES['min_trades']}) — refactor sonrası"
    if unverified:
        detail += f"; {unverified} doğrulanmamış işlem örnekleme KATILMADI"
    results.append(("Örneklem büyüklüğü", ok, detail))

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

    # 5) Max drawdown — SERMAYE üzerinden
    #
    # DENETİM BULGUSU: eğri 0.0'dan başlayan kümülatif KÂR idi, sermaye değil.
    # İki sonucu vardı: (a) yüzde matematiği anlamsızdı — 100$ kârdan 50$
    # geri vermek "%50 drawdown" sayılıyordu, oysa 1000$'lık hesapta bu %5'tir;
    # (b) ilk işlemler zararlıysa `peak` 0 kaldığı için o dönem HİÇ sayılmıyordu.
    # Artık başlangıç sermayesinden başlayan gerçek equity eğrisi kullanılıyor.
    #
    # Not: bu hâlâ KAPANMIŞ işlem eğrisidir; açık pozisyonun anlık zararını
    # görmez, yani gerçek drawdown'ın ALT SINIRIDIR. Gerçek ölçüm için
    # `equity_snapshots` tablosu kullanılır (varsa aşağıdaki kapı onu okur).
    start_eq = _starting_equity()
    eq, peak, max_dd = start_eq, start_eq, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    snap_dd = _snapshot_drawdown()
    if snap_dd is not None:
        max_dd = max(max_dd, snap_dd)
        source = "equity snapshot (açık pozisyonlar dahil)"
        ok = max_dd <= GATES["max_drawdown_pct"]
    else:
        # DENETİM BULGUSU: snapshot yoksa kapı kapanmış işlemlerle GEÇEBİLİYORDU.
        # Kapanmış işlem eğrisi açık pozisyonun anlık zararını görmez; -3R'ye
        # inip +1R'de kapanan işlem kayıtta sıfır drawdown'lı bir kazançtır.
        # Ölçemediğimiz bir şeyi "geçti" saymak, kapıyı anlamsız kılar.
        source = ("YETERSİZ equity snapshot — gerçek drawdown ÖLÇÜLEMEDİ, "
                  "kapı geçilemez")
        ok = False
    results.append((
        "Max drawdown",
        ok,
        f"%{max_dd*100:.1f} (izin: ≤%{GATES['max_drawdown_pct']*100:.0f}) — {source}",
    ))

    # 6) Kesintisiz çalışma — işlem kapanışları arasındaki en uzun boşluk
    #
    # DENETİM BULGUSU: bu kapı "kesintisiz çalışma" diyordu ama ölçtüğü şey
    # yalnızca İLK ve SON işlem arasındaki takvim farkıydı. Arada 29 gün kapalı
    # kalsa bile geçiyordu — tam da ölçmesi gereken şeyi ölçmüyordu.
    # Artık işlem AKTİVİTESİ olan gün sayısı ve en uzun sessiz boşluk ölçülüyor.
    times = sorted(datetime.fromisoformat(t["close_time"]) for t in trades)
    span_days   = (times[-1] - times[0]).total_seconds() / 86400
    active_days = len({t.date() for t in times})
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
    max_gap = max(gaps) if gaps else 0.0

    # DENETİM: işlem aralıkları "çalışıyordu"nun kanıtı DEĞİL — bot ayakta olup
    # hiç sinyal bulmamış da olabilir, ya da tamamen kapalı da. Gerçek kanıt
    # equity snapshot'larıdır: dakikada bir yazılır, süreç ölünce durur.
    hb_days, hb_gap = _heartbeat_uptime()
    ok = (span_days >= GATES["min_uptime_days"]
          and active_days >= GATES["min_active_days"]
          and max_gap <= GATES["max_silent_gap_days"]
          and hb_days >= GATES["min_uptime_days"]
          and hb_gap <= GATES["max_heartbeat_gap_hours"])
    results.append((
        "Kesintisiz çalışma",
        ok,
        f"{span_days:.1f} gün aralık, {active_days} aktif gün, "
        f"heartbeat {hb_days:.1f} gün / en uzun kesinti {hb_gap:.1f} saat "
        f"(izin ≤{GATES['max_heartbeat_gap_hours']}s)",
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
