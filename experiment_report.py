#!/usr/bin/env python3
"""
Deney durum raporu — skor kartları, terfi kapısı, korelasyon.

Günlük çalışır ve Telegram'a özet gönderir. Amacı "hangi bot daha çok kazandı"
sorusunu cevaplamak DEĞİL; deneyin sağlıklı yürüyüp yürümediğini görünür
kılmak:

  * bir strateji sessizce durdu mu (işlem üretmiyor mu),
  * kapı ne diyor ve NEDEN öyle diyor,
  * "nitelikli" görünen stratejiler aslında aynı riski mi taşıyor.

Son madde kritik: `qualified` birden fazla strateji döndürebilir, ama üçü de
aynı hareketlerden kazanıyorsa elimizde üç strateji değil, üç kılığa girmiş
TEK risk vardır. Korelasyon raporda olmasa, çeşitlendirdiğimizi sanarak aynı
bahsi üç kere oynardık.

Kullanım:
    python experiment_report.py            # ekrana yaz
    python experiment_report.py --telegram # Telegram'a da gönder
"""

from __future__ import annotations

import sqlite3
import sys

from database import DB_PATH, state_get
from strategy_lab import (
    build_scorecard,
    correlation_warnings,
    effective_trials,
    evaluate_gate,
    init_ledger,
    overlap_ratio,
    stalled_strategies,
)

STRATEGIES = ["TREND", "MOMENTUM_FAST", "BREAKOUT", "PULLBACK", "MEANREV"]


def topla() -> tuple[dict, list, object, list[str], list[str]]:
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    kesim = state_get("balance_reset_at") or "1970-01-01"

    islemler = {
        ad: [
            dict(r)
            for r in con.execute(
                "SELECT * FROM trades WHERE strategy=? AND close_time IS NOT NULL "
                "AND close_time>=?",
                (ad, kesim),
            )
        ]
        for ad in STRATEGIES
    }
    kartlar = [build_scorecard(ad, islemler[ad]) for ad in STRATEGIES]

    init_ledger(con)
    sonuc = evaluate_gate(kartlar, trials=effective_trials(con) or len(kartlar))
    uyarilar = correlation_warnings(islemler)

    # Sessiz ölüm: son işlemi ne zaman AÇTI?
    son_gorulme = {}
    for ad in STRATEGIES:
        row = con.execute(
            "SELECT MAX(open_time) t FROM trades WHERE strategy=?", (ad,)
        ).fetchone()
        val = row["t"] if row else None
        if val:
            from datetime import datetime

            try:
                val = datetime.fromisoformat(val)
            except ValueError:
                val = None
        son_gorulme[ad] = val
    durmus = stalled_strategies(son_gorulme, max_silence_hours=24)

    con.close()
    return islemler, kartlar, sonuc, uyarilar, durmus


def metin() -> str:
    islemler, kartlar, sonuc, uyarilar, durmus = topla()
    s = ["📊 <b>STRATEJİ DENEYİ</b>", ""]

    s.append("<b>Skor kartları</b> (R = risk birimi)")
    for sc in sorted(kartlar, key=lambda k: k.expectancy_r, reverse=True):
        s.append(
            f"  {sc.strategy}: {sc.expectancy_r:+.3f}R/işlem · "
            f"{sc.trades} işlem (etkin {sc.effective_n}) · "
            f"düşüş {sc.max_drawdown_r:.1f}R"
        )

    s += ["", f"<b>Kapı:</b> {sonuc.status}"]
    if sonuc.reasons:
        s.append(f"  sebep: {sonuc.reasons[0]}")
    if sonuc.qualified:
        s.append(f"  nitelikli: {', '.join(sonuc.qualified)}")

    if uyarilar:
        s += ["", "<b>⚠ Korelasyon</b>"]
        s += [f"  {u}" for u in uyarilar]

    if durmus:
        s += ["", f"<b>⚠ 24 saattir işlem açmayan:</b> {', '.join(durmus)}"]

    s += ["", "<i>Kapı 'LIVE_CANDIDATE' demeden gerçek paraya geçilmez; "
          "o da yalnız ADAY demektir, karar insanındır.</i>"]
    return "\n".join(s)


def main() -> int:
    rapor = metin()
    print(rapor.replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", ""))
    if "--telegram" in sys.argv:
        try:
            from telegram_bot import TelegramBot

            TelegramBot().send(rapor)
        except Exception as exc:  # noqa: BLE001 — rapor gönderilemese de çıktı basıldı
            print(f"[uyarı] Telegram'a gönderilemedi: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
