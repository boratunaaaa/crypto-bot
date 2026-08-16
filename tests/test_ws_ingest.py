"""
WebSocket ticker ayrıştırma.

2026-08-16 canlı arızası: futures modunda `_prices` HİÇ dolmuyordu. Callback
yalnız `isinstance(msg, list)` işliyor ve yalnız `c` alanını okuyordu; oysa
python-binance'in `start_all_ticker_futures_socket`'i `!bookTicker`'a abone olur
ve mesajı `{'stream':..., 'data': {...}}` sarmalında, `c` alanı OLMADAN gönderir.
Yeni tazelik kontrolüyle birleşince her giriş "Fiyat bayat" diye reddedildi.

Bu testler gerçek gözlemlenen mesaj şekillerini sabitler.
"""

import time

import pytest

from market_data import MarketDataManager


@pytest.fixture
def md():
    m = MarketDataManager.__new__(MarketDataManager)   # __init__ ağa çıkar
    import threading
    m._lock = threading.RLock()
    m._prices, m._price_ts, m._spread = {}, {}, {}
    return m


# Canlı sunucudan 2026-08-16'da birebir gözlemlenen bookTicker kaydı
BOOK = {"e": "bookTicker", "s": "AEROUSDT", "b": "0.9990", "B": "12",
        "a": "1.0010", "A": "8", "u": 1, "E": 1, "T": 1, "ps": "AEROUSDT", "st": "PERP"}


def test_book_ticker_wrapped_in_stream_envelope_is_parsed(md):
    """Asıl hata: sarmalı açmıyor ve bookTicker'ı tanımıyorduk."""
    assert md._ingest_ticker(BOOK, time.time()) == 1
    assert md._prices["AEROUSDT"] == pytest.approx(1.0)   # orta fiyat


def test_mid_price_used_not_bid_or_ask(md):
    md._ingest_ticker(BOOK, time.time())
    px = md._prices["AEROUSDT"]
    assert px != pytest.approx(0.999) and px != pytest.approx(1.001)


def test_spread_recorded_from_book_ticker(md):
    md._ingest_ticker(BOOK, time.time())
    assert md._spread["AEROUSDT"] == pytest.approx(0.002, rel=1e-3)


def test_mini_ticker_close_field_still_supported(md):
    """Spot miniTicker yolu bozulmamalı."""
    assert md._ingest_ticker({"s": "BTCUSDT", "c": "50000.5"}, time.time()) == 1
    assert md._prices["BTCUSDT"] == pytest.approx(50000.5)


@pytest.mark.parametrize("bad", [
    None, [], "metin", {}, {"s": "XUSDT"},
    {"s": "XUSDT", "c": "abc"}, {"s": "XUSDT", "c": "0"},
    {"s": "XUSDT", "b": "0", "a": "0"}, {"c": "5"},
])
def test_garbage_never_writes_a_price(md, bad):
    """Bozuk kayıt fiyat yazmamalı — bayat/yanlış fiyat en pahalı hatadır."""
    assert md._ingest_ticker(bad, time.time()) == 0
    assert md._prices == {}


def test_timestamp_recorded_so_freshness_check_works(md):
    now = time.time()
    md._ingest_ticker(BOOK, now)
    assert md._price_ts["AEROUSDT"] == pytest.approx(now)
