"""
Korelasyon ve stres limiti.

NEDEN GEREKLİ
`MAX_CORRELATED_POSITIONS` aynı yönde en fazla 4 pozisyona izin veriyor ve
`MAX_PORTFOLIO_RISK_R` toplam açık riski 4R ile sınırlıyor. İkisi de sayı
sayıyor; hiçbiri pozisyonların BİRLİKTE HAREKET EDİP ETMEDİĞİNE bakmıyor.

Kriptoda bu boşluk büyük: altcoin'lerin çoğu BTC ile yüksek korelasyonludur.
4 ayrı altcoin long'u "dört bağımsız 1R bahsi" değildir — BTC %5 düşerse
dördü birden aynı anda stop olur. Yani nominal 4R risk, gerçekte tek bir
4R'lik BTC bahsidir ve kill switch bunu göremez.

"Risk alsın ama toplamda zarar ettirmesin" isteğinin karşılığı tam da budur:
tek işlemde risk küçük kalabilir, ama aynı anda açık olan işlemler aynı şeye
bahis oynuyorsa portföy riski çarpılır.

YÖNTEM
Getiri korelasyonu, fiyat korelasyonu değil. Fiyat serileri trend yüzünden
neredeyse her zaman yüksek korele görünür; anlamlı olan getirilerin birlikte
hareket etmesidir.

Beta, portföyün BTC'ye karşı gerçek maruziyetini R cinsinden verir:
    stresli_zarar = Σ (pozisyon_riski_R × beta_i × şok / stop_mesafesi_i)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("correlation")


def returns(prices: list[float]) -> list[float]:
    """Yüzde getiriler. Korelasyon HAM FİYATTAN değil bundan hesaplanır."""
    out = []
    for a, b in zip(prices, prices[1:]):
        if a and a > 0:
            out.append((b - a) / a)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def correlation(a: list[float], b: list[float]) -> float:
    """Pearson korelasyonu. Örnek yetersizse 0 değil, MUHAFAZAKÂR 1.0 döner."""
    n = min(len(a), len(b))
    if n < 20:
        # Bilmiyorsak "korelasyon yok" varsaymak, tam da korunmak istediğimiz
        # riski görünmez kılar. Bilinmeyen = en kötü durum.
        return 1.0
    a, b = a[-n:], b[-n:]
    ma, mb = _mean(a), _mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da  = sum((x - ma) ** 2 for x in a) ** 0.5
    db  = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return 1.0
    return max(-1.0, min(1.0, num / (da * db)))


def beta_to(asset: list[float], market: list[float]) -> float:
    """
    Varlığın piyasaya (BTC) karşı betası — getiri serilerinden.

    beta = cov(asset, market) / var(market)
    """
    n = min(len(asset), len(market))
    if n < 20:
        return 1.0                      # bilinmiyor → tam maruziyet varsay
    a, m = asset[-n:], market[-n:]
    ma, mm = _mean(a), _mean(m)
    cov = sum((x - ma) * (y - mm) for x, y in zip(a, m)) / n
    var = sum((y - mm) ** 2 for y in m) / n
    if var == 0:
        return 1.0
    return cov / var


def net_directional_r(positions: dict, betas: dict[str, float] | None = None) -> float:
    """
    Portföyün NET yönlü maruziyeti, R cinsinden ve beta ile ağırlıklı.

    Long'lar pozitif, short'lar negatif. Birbirini gerçekten hedge eden
    pozisyonlar burada birbirini götürür — sayı saymak bunu yapamaz.
    """
    betas = betas or {}
    total = 0.0
    for sym, pos in positions.items():
        risk_amount = pos.get("risk_amount", 0.0)
        if risk_amount <= 0:
            continue
        entry = pos["entry_price"]
        stop  = pos.get("stop_loss", entry)
        qty   = pos.get("qty", 0.0)
        # `allocated_r` ile ağırlıklandır — risk_manager ile AYNI birim.
        # Aksi hâlde her pozisyon 1R sayılır ve büyük riskli pozisyonlar
        # portföy maruziyetinde görünmez olur.
        weight = float(pos.get("allocated_r", 1.0) or 1.0)
        if pos["side"] == "BUY":
            open_r = max(0.0, entry - stop) * qty / risk_amount
            sign = 1.0
        else:
            open_r = max(0.0, stop - entry) * qty / risk_amount
            sign = -1.0
        total += sign * open_r * weight * betas.get(sym, 1.0)
    return round(total, 3)


def stress_loss_r(positions: dict, shock_pct: float,
                  betas: dict[str, float] | None = None) -> float:
    """
    BTC'de `shock_pct` büyüklüğünde ani bir hareket olursa toplam zarar (R).

    Her pozisyon için beklenen fiyat hareketi `beta × şok`; bu hareket stop
    mesafesini aşarsa pozisyon kalan riskinin tamamını kaybeder, aşmazsa
    oransal kaybeder. Hedge eden pozisyonların kazancı DA sayılır — ama
    muhafazakâr olmak için kazanç yarım ağırlıkla girer (gerçek kriz anında
    korelasyonlar 1'e koşar ve hedge'ler beklendiği gibi çalışmaz).
    """
    betas = betas or {}
    total = 0.0
    for sym, pos in positions.items():
        risk_amount = pos.get("risk_amount", 0.0)
        if risk_amount <= 0:
            continue
        entry = pos["entry_price"]
        stop  = pos.get("stop_loss", entry)
        qty   = pos.get("qty", 0.0)
        weight = float(pos.get("allocated_r", 1.0) or 1.0)
        remaining_r = (max(0.0, entry - stop) if pos["side"] == "BUY"
                       else max(0.0, stop - entry)) * qty / risk_amount * weight
        if remaining_r <= 0:
            continue                              # breakeven'e taşınmış, risksiz

        stop_dist_pct = abs(entry - stop) / entry if entry else 1.0
        move_pct = betas.get(sym, 1.0) * shock_pct
        adverse = (move_pct < 0) if pos["side"] == "BUY" else (move_pct > 0)
        hit_frac = min(1.0, abs(move_pct) / stop_dist_pct) if stop_dist_pct > 0 else 1.0

        if adverse:
            total += remaining_r * hit_frac
        else:
            total -= remaining_r * hit_frac * 0.5   # hedge'e yarım güven
    return round(max(0.0, total), 3)


def can_add_correlated(positions: dict, symbol: str, side: str,
                       new_risk_r: float,
                       betas: dict[str, float] | None = None,
                       *, max_net_r: float, max_stress_r: float,
                       shock_pct: float) -> tuple[bool, str]:
    """
    Yeni pozisyon, portföyü korelasyon açısından kabul edilemez hâle getirir mi?

    İki kapı:
      1. Net yönlü maruziyet tavanı — tek yöne fazla yüklenme.
      2. Stres testi — BTC şokunda toplam zarar tavanı.
    """
    betas = betas or {}
    probe = dict(positions)
    # DENETİM BULGUSU: probe `qty=new_risk_r, risk_amount=new_risk_r*2` ile
    # kuruluyordu. open_r = (100-98)*qty/risk_amount = 1.0 — yani new_risk_r
    # ne olursa olsun SONUÇ HEP 1R çıkıyordu. %0.4 riskli işlemle %1.2 riskli
    # işlem aynı maruziyeti üretiyordu ve kapı gerçek riski hiç görmüyordu.
    #
    # Doğrusu: risk_amount'ı, kalan riskin tam olarak `new_risk_r` R çıkacağı
    # şekilde ölçekle. per_unit=2, qty=1 → risk_amount = 2/new_risk_r.
    r = max(float(new_risk_r), 1e-9)
    probe[f"__probe__{symbol}"] = {
        "symbol": symbol, "side": side, "entry_price": 100.0,
        "stop_loss": 98.0 if side == "BUY" else 102.0,
        "qty": 1.0, "risk_amount": 2.0, "allocated_r": r,
    }
    b = dict(betas)
    b[f"__probe__{symbol}"] = betas.get(symbol, 1.0)

    net = abs(net_directional_r(probe, b))
    if net > max_net_r:
        return False, f"Net yönlü maruziyet {net:.1f}R > {max_net_r:.1f}R"

    stress = stress_loss_r(probe, -abs(shock_pct) if side == "BUY" else abs(shock_pct), b)
    if stress > max_stress_r:
        return False, (
            f"Stres testi: BTC %{abs(shock_pct)*100:.0f} şokunda "
            f"{stress:.1f}R zarar > {max_stress_r:.1f}R"
        )
    return True, ""
