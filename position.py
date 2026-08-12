"""
Pozisyon yaşam döngüsü ve çıkış motoru.

BU MODÜL HEM PAPER HEM GERÇEK MODDA KULLANILIR.

Neden ayrı bir modül: eski kodda trailing/breakeven mantığının tamamı yalnızca
`PaperPortfolio.check_positions()` içindeydi; `FuturesTrader.check_positions()`
düz stop/TP kontrolünden ibaretti. Yani `PAPER_TRADING=False` yapıldığı anda
strateji sessizce değişiyordu — kârın büyük kısmını üreten "kazananı koştur"
mekanizması gerçek modda hiç çalışmıyordu. Çıkış kararı artık tek bir yerde
üretilir, iki trader da aynı kararı alır (bkz. tests/test_parity.py).

Çıkış modeli — kullanıcının iki isteğini aynı anda karşılar:
  "kâra geçince ani düşüşten etkilenmeyeyim"  → kısmi realize + breakeven stop
  "üst limitsiz maksimum kazanç"              → runner'da SABİT TP YOK

  +1R  → pozisyonun %40'ı nakde çevrilir, stop breakeven+komisyona taşınır
  +2R  → %30 daha nakde çevrilir
  kalan %30 → Chandelier trailing ile SINIRSIZ koşar

Sabit bir dolar/yüzde stop yerine ATR-ölçekli stop kullanılmasının sebebi
ölçümdür: işlem yapılan coinlerin ortalama 5dk ATR'si %1.26 (max %5.11).
%1'lik sabit bir stop, coinin normal gürültü bandının İÇİNDE kalır ve tam da
yakalamak istediğimiz büyük hareketleri daha başlamadan keser.
"""

from datetime import datetime, timedelta

from config import (
    COMMISSION_RATE,
    PARTIAL_EXITS_ENABLED, PARTIAL_TP1_R, PARTIAL_TP1_PCT,
    PARTIAL_TP2_R, PARTIAL_TP2_PCT,
    BREAKEVEN_AFTER_TP1, BREAKEVEN_BUFFER_PCT,
    CHANDELIER_ATR_MULT_STRONG, CHANDELIER_ATR_MULT_WEAK,
    CHANDELIER_MIN_LOCK_R, TRAIL_MOMENTUM_LOOKBACK,
    TIME_STOP_ENABLED, TIME_STOP_MINUTES, TIME_STOP_MIN_MFE_R, TIME_STOP_MAX_MINUTES,
)

# ── Çıkış nedenleri ───────────────────────────────────────────────────────────
# Eski kodda TEK bir "STOP_LOSS" etiketi vardı ve 82/82 işlem onunla
# kaydedilmişti — kârla trail olan ile zararla stop olan ayırt edilemiyordu.
# Bu etiketler o körlüğü kapatır.
EXIT_INITIAL_STOP   = "INITIAL_STOP"      # ilk stop'a çarptı — gerçek zarar
EXIT_BREAKEVEN_STOP = "BREAKEVEN_STOP"    # TP1 sonrası başabaş stop — zarar yok
EXIT_TRAIL_STOP     = "TRAIL_STOP"        # trailing stop — kârla çıkış
EXIT_TIME_STOP      = "TIME_STOP"         # hareket gelmedi, sermaye serbest
EXIT_TP1            = "TP1_PARTIAL"
EXIT_TP2            = "TP2_PARTIAL"
EXIT_MANUAL         = "MANUAL"

STOP_KIND_INITIAL   = "INITIAL"
STOP_KIND_BREAKEVEN = "BREAKEVEN"
STOP_KIND_TRAIL     = "TRAIL"

_EXIT_BY_STOP_KIND = {
    STOP_KIND_INITIAL:   EXIT_INITIAL_STOP,
    STOP_KIND_BREAKEVEN: EXIT_BREAKEVEN_STOP,
    STOP_KIND_TRAIL:     EXIT_TRAIL_STOP,
}


def new_position(
    symbol: str,
    side: str,
    entry_price: float,
    qty: float,
    margin: float,
    leverage: int,
    stop_loss: float,
    take_profit: float = 0.0,
    atr_pct: float = 0.01,
    strategy: str = "TREND",
    open_time: datetime | None = None,
    **meta,
) -> dict:
    """Yeni pozisyon sözlüğü. R (birim risk) burada sabitlenir ve bir daha değişmez."""
    risk_per_unit = abs(entry_price - stop_loss)
    return dict(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        qty=qty,                      # kalan miktar
        initial_qty=qty,              # ilk miktar (kısmi çıkış oranları için)
        margin=margin,
        initial_margin=margin,
        leverage=leverage,
        stop_loss=stop_loss,
        initial_stop=stop_loss,
        take_profit=take_profit,
        risk_per_unit=risk_per_unit,
        risk_amount=risk_per_unit * qty,
        stop_kind=STOP_KIND_INITIAL,
        open_time=open_time or datetime.now(),
        strategy=strategy,
        atr_pct=atr_pct,
        # izleme
        peak=entry_price,             # lehte gidilen en uç fiyat
        trough=entry_price,           # aleyhte gidilen en uç fiyat
        mfe_r=0.0,
        mae_r=0.0,
        ticks_since_peak=0,
        tp1_done=False,
        tp2_done=False,
        realized_pnl=0.0,             # kısmi çıkışlardan biriken net kâr
        realized_commission=0.0,
        trailing_active=False,
        **meta,
    )


def _r_multiple(pos: dict, price: float) -> float:
    """Fiyatın giriş noktasına göre kaç R uzakta olduğu (lehte pozitif)."""
    r = pos["risk_per_unit"]
    if r <= 0:
        return 0.0
    diff = (price - pos["entry_price"]) if pos["side"] == "BUY" else (pos["entry_price"] - price)
    return diff / r


def _breakeven_price(pos: dict) -> float:
    """
    Başabaş stop — komisyonun ÜSTÜNE konur.

    Girişe tam eşit bir stop aslında zarardır: açılış + kapanış komisyonu
    (round-trip ≈ %0.10) cepten çıkar. Buffer bunu telafi eder, böylece
    "kâr etmesek bile zarar etmeyelim" gerçekten sağlanır.
    """
    buffer = max(BREAKEVEN_BUFFER_PCT, COMMISSION_RATE * 2 + 0.0002)
    if pos["side"] == "BUY":
        return pos["entry_price"] * (1 + buffer)
    return pos["entry_price"] * (1 - buffer)


def _tighten_stop(pos: dict, new_stop: float, kind: str) -> bool:
    """
    Stop'u YALNIZCA lehte yönde taşı.

    Bu fonksiyon dışında stop'a dokunulmaz. Garantisi: trailing hiçbir koşulda
    stop-loss riskini yeniden büyütemez.
    """
    if pos["side"] == "BUY":
        if new_stop > pos["stop_loss"]:
            pos["stop_loss"] = new_stop
            pos["stop_kind"] = kind
            return True
    else:
        if new_stop < pos["stop_loss"]:
            pos["stop_loss"] = new_stop
            pos["stop_kind"] = kind
            return True
    return False


def _update_excursions(pos: dict, price: float) -> None:
    """MFE/MAE takibi + zirveden bu yana geçen tick sayısı (momentum göstergesi)."""
    if pos["side"] == "BUY":
        if price > pos["peak"]:
            pos["peak"] = price
            pos["ticks_since_peak"] = 0
        else:
            pos["ticks_since_peak"] += 1
        pos["trough"] = min(pos["trough"], price)
    else:
        if price < pos["peak"]:
            pos["peak"] = price
            pos["ticks_since_peak"] = 0
        else:
            pos["ticks_since_peak"] += 1
        pos["trough"] = max(pos["trough"], price)

    pos["mfe_r"] = max(pos["mfe_r"], _r_multiple(pos, pos["peak"]))
    pos["mae_r"] = min(pos["mae_r"], _r_multiple(pos, pos["trough"]))


def _chandelier_stop(pos: dict) -> float:
    """
    Chandelier trailing: zirveden N×ATR geride.

    Çarpan momentuma göre uyarlanır — hareket devam ederken geniş (hareketi
    boğmamak için), zirveden uzaklaşıldıkça dar (kârı korumak için).
    SABİT BİR ÜST HEDEF YOKTUR; coin ne kadar giderse pozisyon o kadar taşınır.
    """
    atr_abs = max(pos.get("atr_pct", 0.01), 0.001) * pos["entry_price"]
    strong = pos["ticks_since_peak"] <= TRAIL_MOMENTUM_LOOKBACK
    mult = CHANDELIER_ATR_MULT_STRONG if strong else CHANDELIER_ATR_MULT_WEAK
    pad = atr_abs * mult

    # Trailing devredeyse en az bu kadar kâr her hâlükârda kilitli kalır.
    min_lock = pos["risk_per_unit"] * CHANDELIER_MIN_LOCK_R

    if pos["side"] == "BUY":
        return max(pos["peak"] - pad, pos["entry_price"] + min_lock)
    return min(pos["peak"] + pad, pos["entry_price"] - min_lock)


def _stop_hit(pos: dict, price: float) -> bool:
    if pos["side"] == "BUY":
        return price <= pos["stop_loss"]
    return price >= pos["stop_loss"]


def evaluate(pos: dict, price: float, now: datetime | None = None) -> list[dict]:
    """
    Pozisyonu güncel fiyata göre değerlendir ve yapılacak aksiyonları döndür.

    Dönen liste elemanları:
        {"type": "CLOSE",   "fraction": 1.0, "reason": <EXIT_*>, "price": float}
        {"type": "PARTIAL", "fraction": 0.4, "reason": <EXIT_*>, "price": float}

    Aksiyon listesi boşsa pozisyon açık kalır (stop güncellenmiş olabilir).

    ÖNEMLİ — sıralama bilinçlidir: stop kontrolü, trailing güncellemesinden
    ÖNCE yapılır. Aksi hâlde aynı tick'te stop hem yukarı çekilir hem de
    "değmedi" sayılır; bu, geçmişe bakarak iyimser sonuç üreten klasik bir
    look-ahead hatasıdır.
    """
    now = now or datetime.now()
    actions: list[dict] = []

    if pos["qty"] <= 0:
        return actions

    # 1) Mevcut stop'a çarpıldı mı? (bir önceki tick'te belirlenmiş stop ile)
    if _stop_hit(pos, price):
        reason = _EXIT_BY_STOP_KIND.get(pos["stop_kind"], EXIT_INITIAL_STOP)
        actions.append({"type": "CLOSE", "fraction": 1.0, "reason": reason, "price": price})
        return actions

    # 2) İzleme güncelle
    _update_excursions(pos, price)
    r_now = _r_multiple(pos, price)

    # 3) Time stop — beklenen hareket gelmediyse sermayeyi serbest bırak
    if TIME_STOP_ENABLED:
        age_min = (now - pos["open_time"]).total_seconds() / 60
        if age_min >= TIME_STOP_MINUTES and pos["mfe_r"] < TIME_STOP_MIN_MFE_R:
            actions.append({"type": "CLOSE", "fraction": 1.0,
                            "reason": EXIT_TIME_STOP, "price": price})
            return actions
        # Runner'a dönüşmemiş (TP1 bile görmemiş) pozisyon için mutlak süre sınırı.
        # TP1 alınmışsa pozisyon artık runner'dır ve süre sınırı UYGULANMAZ —
        # "coin %50 giderse taşıyabilelim" isteği bunu gerektirir.
        if not pos["tp1_done"] and age_min >= TIME_STOP_MAX_MINUTES:
            actions.append({"type": "CLOSE", "fraction": 1.0,
                            "reason": EXIT_TIME_STOP, "price": price})
            return actions

    # 4) Kısmi kâr alma
    if PARTIAL_EXITS_ENABLED:
        if not pos["tp1_done"] and r_now >= PARTIAL_TP1_R:
            pos["tp1_done"] = True
            actions.append({"type": "PARTIAL", "fraction": PARTIAL_TP1_PCT,
                            "reason": EXIT_TP1, "price": price})
            if BREAKEVEN_AFTER_TP1:
                _tighten_stop(pos, _breakeven_price(pos), STOP_KIND_BREAKEVEN)

        if pos["tp1_done"] and not pos["tp2_done"] and r_now >= PARTIAL_TP2_R:
            pos["tp2_done"] = True
            pos["trailing_active"] = True
            actions.append({"type": "PARTIAL", "fraction": PARTIAL_TP2_PCT,
                            "reason": EXIT_TP2, "price": price})
    else:
        # Kısmi çıkış kapalıysa TP1 seviyesinde doğrudan trailing'e geç
        if not pos["trailing_active"] and r_now >= PARTIAL_TP1_R:
            pos["trailing_active"] = True
            if BREAKEVEN_AFTER_TP1:
                _tighten_stop(pos, _breakeven_price(pos), STOP_KIND_BREAKEVEN)

    # 5) Runner trailing — burada ÜST LİMİT YOK
    if pos["trailing_active"]:
        _tighten_stop(pos, _chandelier_stop(pos), STOP_KIND_TRAIL)

    return actions


def realized_r(pos: dict, final_pnl_total: float) -> float:
    """Toplam net PnL'in R cinsinden karşılığı (kısmi çıkışlar dahil)."""
    risk = pos.get("risk_amount", 0.0)
    if risk <= 0:
        return 0.0
    return final_pnl_total / risk


def position_summary(pos: dict) -> dict:
    """DB'ye ve rapora yazılacak özet."""
    return {
        "strategy":     pos.get("strategy", "TREND"),
        "initial_stop": pos.get("initial_stop", 0.0),
        "risk_amount":  pos.get("risk_amount", 0.0),
        "mfe_r":        round(pos.get("mfe_r", 0.0), 3),
        "mae_r":        round(pos.get("mae_r", 0.0), 3),
        "tp1_done":     pos.get("tp1_done", False),
        "tp2_done":     pos.get("tp2_done", False),
        "partial_pnl":  round(pos.get("realized_pnl", 0.0), 4),
    }
