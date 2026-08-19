"""
Açık pozisyonlara ADANMIŞ fiyat akışı.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
Toplu akış (`!bookTicker`) bazı sembolleri sessizce düşürüyor. Ölçülen zincir:

  2026-08-07  bir sembolün fiyatı SAATLERCE güncellenmedi, pozisyon gerçek
              fiyat stop'u çoktan geçmişken kapanmadı.
  2026-08-18  stop kontrolü 90 saniyeye kadar bayat fiyatı "taze" sayıyordu;
              `INITIAL_STOP` ile kapanan 32 işlemin 32'si de -1R'yi aştı,
              plan dışı fazla zarar 119.50$.
  düzeltme    tazelik eşiği 20s + REST yedeği + 3 saniyelik kontrol.
  ÖLÇÜM       yine de yetmedi: sıfırlamadan bu yana 41 stop işleminin
              ortalaması **-1.28R**. Her stop planlanandan ~%28 pahalı.

Kalan boşluk 20 saniyelik pencere. Sembole ADANMIŞ abonelik toplu akışın
seçiciliğine tabi değildir ve saniyenin altında güncellenir.

REST yedeği KALDIRILMADI: adanmış akış da düşebilir. Katmanlar birbirinin
yedeği — bu testler ikisinin de yerinde durduğunu sabitler.
"""

import threading
from unittest.mock import MagicMock

import pytest

from market_data import MarketDataManager


def _mdm(twm=None):
    m = MarketDataManager.__new__(MarketDataManager)
    m._prices = {}
    m._price_ts = {}
    m._spread = {}
    m._lock = threading.Lock()
    m._prio_lock = threading.Lock()
    m._prio_streams = {}
    m._twm = twm
    m._last_ws_msg = 0.0
    m._last_price_update = 0.0
    return m


def _twm():
    t = MagicMock()
    t.start_symbol_ticker_futures_socket.side_effect = (
        lambda callback, symbol: f"stream-{symbol}"
    )
    return t


# ── Abonelik yönetimi ─────────────────────────────────────────────────────────

def test_acik_pozisyonlar_icin_akis_acilir():
    t = _twm()
    m = _mdm(t)
    m.set_priority_symbols(["AAAUSDT", "BBBUSDT"])
    assert set(m._prio_streams) == {"AAAUSDT", "BBBUSDT"}
    assert t.start_symbol_ticker_futures_socket.call_count == 2


def test_kapanan_pozisyonun_akisi_kapatilir():
    """Kapatılmazsa abonelikler birikir ve bağlantı sınırına dayanır."""
    t = _twm()
    m = _mdm(t)
    m.set_priority_symbols(["AAAUSDT", "BBBUSDT"])
    m.set_priority_symbols(["AAAUSDT"])
    assert set(m._prio_streams) == {"AAAUSDT"}
    t.stop_socket.assert_called_once_with("stream-BBBUSDT")


def test_ayni_kume_ikinci_kez_abone_OLMAZ():
    """Her turda yeniden abone olmak soket sızıntısı üretirdi."""
    t = _twm()
    m = _mdm(t)
    m.set_priority_symbols(["AAAUSDT"])
    m.set_priority_symbols(["AAAUSDT"])
    assert t.start_symbol_ticker_futures_socket.call_count == 1
    t.stop_socket.assert_not_called()


def test_pozisyon_kalmayinca_hepsi_kapanir():
    t = _twm()
    m = _mdm(t)
    m.set_priority_symbols(["AAAUSDT"])
    m.set_priority_symbols([])
    assert m._prio_streams == {}


def test_akis_acilamazsa_pozisyon_yonetimi_DURMAZ():
    """Adanmış akış bir iyileştirme; kurulamazsa REST yedeği devrede kalır."""
    t = _twm()
    t.start_symbol_ticker_futures_socket.side_effect = RuntimeError("olmadı")
    m = _mdm(t)
    m.set_priority_symbols(["AAAUSDT"])          # patlamamalı
    assert m._prio_streams == {}


def test_ws_yokken_cagri_patlamaz():
    m = _mdm(None)
    m.set_priority_symbols(["AAAUSDT"])
    assert m._prio_streams == {}


# ── Fiyat işleme ──────────────────────────────────────────────────────────────

def test_adanmis_akistan_gelen_fiyat_islenir():
    m = _mdm(_twm())
    m._on_priority_msg({"e": "24hrTicker", "s": "AAAUSDT", "c": "12.5"})
    assert m._prices["AAAUSDT"] == 12.5
    assert m._price_ts["AAAUSDT"] > 0


def test_sarmalanmis_mesaj_da_islenir():
    """Bazı akışlar {'stream':..,'data':..} sarmalıyla gelir."""
    m = _mdm(_twm())
    m._on_priority_msg({"stream": "x", "data": {"s": "BBBUSDT", "c": "3.0"}})
    assert m._prices["BBBUSDT"] == 3.0


def test_bozuk_mesaj_patlatmaz():
    m = _mdm(_twm())
    for bozuk in (None, [], {}, {"s": None}, "metin", {"s": "X"}):
        m._on_priority_msg(bozuk)
    assert "X" not in m._prices


# ── Yeniden bağlanma ──────────────────────────────────────────────────────────

def test_WS_yeniden_baslayinca_abonelikler_temizlenir():
    """Soket anahtarları eski yöneticiye aitti; yenisinde geçersizdir.

    Temizlenmezse `set_priority_symbols` 'zaten abone' sanır ve adanmış akış
    bir daha HİÇ kurulmaz: pozisyonlar sessizce sorunun kaynağı olan toplu
    akışa geri düşer.
    """
    m = _mdm(_twm())
    m.set_priority_symbols(["AAAUSDT"])
    assert m._prio_streams
    m._stop_twm()
    assert m._prio_streams == {}


def test_REST_yedegi_KALDIRILMADI():
    """Adanmış akış da düşebilir; katmanlar birbirinin yedeği olmalı."""
    import inspect

    kaynak = inspect.getsource(MarketDataManager.get_prices_for)
    assert "_fetch_price_rest" in kaynak, "REST yedeği kaldırılmış"
