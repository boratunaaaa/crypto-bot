"""
FİYAT TAZELİĞİ — stop kararının dayandığı verinin yaşı.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
2026-08-18 ölçümü: `INITIAL_STOP` ile kapanan **32 işlemin 32'si de** planlanan
-1R'yi aştı. Ortalama -1.5R, en kötüsü -5.65R (STABLEUSDT: plan %0.89 stop,
gerçekleşen %4.72 — plana göre %428 aşım). Plan dışı fazla zarar: 119.50$.

Bu bir "piyasa hızlı hareket etti" hikâyesi değildi; 32/32 oranı sistematik bir
kusuru gösterir. İki neden üst üste biniyordu:

1. `get_prices_for` varsayılan tazelik eşiği **90 saniyeydi** ve stop kontrolü
   tam da bu varsayılanı kullanıyordu. Yani 89 saniyelik bir fiyat "taze"
   sayılıp stop kararına giriyordu. Giriş yolu ise `MAX_PRICE_AGE_SEC`'i (20s)
   AÇIKÇA geçiriyordu: güvenlik-kritik olan taraf gevşek, olmayan taraf sıkıydı.

2. Bayat fiyatı elemesi gereken koruma HİÇ ÇALIŞMAMIŞTI. Koruma yalnız
   `fresh` sözlüğü DIŞINDA kalan sembollere bakıyordu; ama getirme eşiği (90s)
   korumanın kendi eşiğinden (60s) GEVŞEK olduğu için elemek istediği fiyatlar
   `fresh` içinde dönüyor, korumaya hiç ulaşmıyordu. Ölü kod gibi duruyordu
   ama ölü değildi — yanlış eşikle yazılmıştı.

Canlı kanıt: PIEVERSEUSDT için log boyunca `yaş=120s` uyarısı; o sembolde stop
kontrolü fiilen 2 dakikada bir çalışıyordu.

Bu testler eşiği DEĞİL, DAVRANIŞI sabitler: bayat fiyat stop kararına giremez.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from config import MAX_PRICE_AGE_SEC, POSITION_CHECK_INTERVAL, SIGNAL_INTERVAL
from market_data import MarketDataManager


def _mdm(prices: dict, ages: dict) -> MarketDataManager:
    """Gerçek WS/REST kurmadan fiyat önbelleğini doldurulmuş bir yönetici."""
    m = MarketDataManager.__new__(MarketDataManager)
    m._prices = dict(prices)
    now = time.time()
    m._price_ts = {s: now - yas for s, yas in ages.items()}
    import threading

    m._lock = threading.Lock()
    m._client_lock = threading.Lock()
    return m


# ── 1) Varsayılan eşik ────────────────────────────────────────────────────────

def test_varsayilan_tazelik_esigi_giris_yoluyla_ayni():
    """Varsayılan, projenin kendi güvenlik sabitiyle aynı olmalı.

    Asıl hata buydu: iki farklı eşik vardı ve gevşek olanı stop kontrolüne
    düşmüştü. Tek sayı olursa "hangisi geçerli" sorusu ortadan kalkar.
    """
    import inspect

    varsayilan = inspect.signature(MarketDataManager.get_prices_for).parameters[
        "max_age_seconds"
    ].default
    assert varsayilan == MAX_PRICE_AGE_SEC, (
        f"stop kontrolünün varsayılan tazelik eşiği {varsayilan}s, "
        f"giriş yolununki {MAX_PRICE_AGE_SEC}s — güvenlik-kritik yol gevşek kalmış"
    )


# ── 2) Bayat fiyat REST'e düşer ───────────────────────────────────────────────

def test_bayat_fiyat_taze_sayilmaz_ve_rest_ile_yenilenir():
    """90 saniyelik fiyat artık 'taze' dönmemeli; REST'ten tazelenmeli."""
    m = _mdm({"AAAUSDT": 100.0}, {"AAAUSDT": 85})  # eski eşikte "taze" sayılırdı
    with patch.object(m, "_fetch_price_rest", return_value=101.0) as rest:
        out = m.get_prices_for(["AAAUSDT"])
    rest.assert_called_once_with("AAAUSDT")
    assert out["AAAUSDT"] == 101.0, "bayat fiyat olduğu gibi döndürülmüş"


def test_gercekten_taze_fiyat_rest_cagirmaz():
    """Tazelik eşiğinin altındaki fiyat için gereksiz REST çağrısı yapılmamalı."""
    m = _mdm({"AAAUSDT": 100.0}, {"AAAUSDT": 2})
    with patch.object(m, "_fetch_price_rest", return_value=999.0) as rest:
        out = m.get_prices_for(["AAAUSDT"])
    rest.assert_not_called()
    assert out["AAAUSDT"] == 100.0


def test_rest_de_veremezse_sembol_hic_donmez():
    """Taze fiyat üretilemiyorsa sonuçta yer ALMAMALI.

    Ana döngü 'sözlükte yoksa bu turda çıkış değerlendirme' kuralını buna
    dayandırıyor. Buraya bayat bir değer sızarsa koruma yine çöker.
    """
    m = _mdm({"AAAUSDT": 100.0}, {"AAAUSDT": 300})
    with patch.object(m, "_fetch_price_rest", return_value=None):
        out = m.get_prices_for(["AAAUSDT"])
    assert "AAAUSDT" not in out, "REST başarısızken bayat fiyat sonuca sızdı"


def test_bir_sembol_bayatken_digeri_etkilenmez():
    m = _mdm(
        {"AAAUSDT": 100.0, "BBBUSDT": 50.0},
        {"AAAUSDT": 2, "BBBUSDT": 400},
    )
    with patch.object(m, "_fetch_price_rest", return_value=None):
        out = m.get_prices_for(["AAAUSDT", "BBBUSDT"])
    assert out == {"AAAUSDT": 100.0}


def test_cagiran_bilerek_gevsetebilir():
    """Eşik yasak değil, varsayılan. Gevşetmek isteyen açıkça yazmalı."""
    m = _mdm({"AAAUSDT": 100.0}, {"AAAUSDT": 60})
    with patch.object(m, "_fetch_price_rest", return_value=None) as rest:
        out = m.get_prices_for(["AAAUSDT"], max_age_seconds=120)
    rest.assert_not_called()
    assert out["AAAUSDT"] == 100.0


# ── 3) Stop kontrol sıklığı ───────────────────────────────────────────────────

def test_pozisyon_kontrolu_sinyal_taramasindan_hizli():
    """Stop kontrolü sinyal taramasına bağlıysa gecikme taramanın periyodudur.

    Eskiden ikisi aynı döngüdeydi (30 sn): fiyat stop'u geçtiğinde bot bunu
    en kötü 30 saniye sonra fark ediyordu. Üstüne 90 saniyelik bayat fiyat
    toleransı binince gerçekleşen gecikme 30-120 saniyeye çıkıyordu.
    """
    assert POSITION_CHECK_INTERVAL < SIGNAL_INTERVAL, (
        "pozisyon kontrolü sinyal taramasından hızlı olmalı"
    )
    assert POSITION_CHECK_INTERVAL <= MAX_PRICE_AGE_SEC, (
        "kontrol aralığı fiyat tazelik eşiğinden büyükse, taze fiyat "
        "beklerken bile stop geç fark edilir"
    )
    assert POSITION_CHECK_INTERVAL > 0


# ── 4) Ana döngüdeki koruma gerçekten eliyor mu ───────────────────────────────

def _sahte_ana_dongu_koruma(held: list, fresh: dict, prices: dict) -> dict:
    """Ana döngüdeki korumanın KENDİSİNİ çağırır — taklidini değil.

    İlk hâli kuralı burada yeniden yazıyordu; mutasyon denetimi main.py'deki
    kuralı bozduğunda test yine yeşil kalıyordu (M65 sağ kalmıştı). Kural
    `drop_stale_prices` fonksiyonuna çıkarıldı ve test artık onu çağırıyor.
    """
    from main import drop_stale_prices

    out = dict(prices)
    drop_stale_prices(held, fresh, out)
    return out


def test_bayat_sembol_stop_motoruna_ulasmaz():
    """Asıl kusur: bayat fiyat `prices` sözlüğünde kalıp stop kararına giriyordu."""
    held = ["AAAUSDT", "BBBUSDT"]
    prices = {"AAAUSDT": 100.0, "BBBUSDT": 50.0}   # WS'ten gelen, biri bayat
    fresh = {"AAAUSDT": 100.5}                      # yalnız biri tazelenebildi
    sonuc = _sahte_ana_dongu_koruma(held, fresh, prices)
    assert "BBBUSDT" not in sonuc, (
        "taze fiyatı olmayan sembol stop motoruna geçti — "
        "bayat fiyatla stop kararı vermek, kararı hiç vermemekten kötüdür"
    )
    assert sonuc["AAAUSDT"] == 100.0


def test_hepsi_tazeyse_hicbiri_dusmez():
    held = ["AAAUSDT"]
    sonuc = _sahte_ana_dongu_koruma(held, {"AAAUSDT": 100.0}, {"AAAUSDT": 100.0})
    assert sonuc == {"AAAUSDT": 100.0}
