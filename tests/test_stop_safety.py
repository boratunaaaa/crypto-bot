"""
KORUYUCU STOP GÜVENLİĞİ — gerçek parayla ilgili en kritik testler.

Mevcut 89 test büyük ölçüde API'nin BAŞARILI olduğu akışı doğruluyordu. Gerçek
zarar ise API'nin başarısız olduğu anda doğar: emir reddedilir, bağlantı kopar,
süreç ölür. Bu dosya yalnız o senaryoları test eder.

Denetimde bulunan açıklar (2026-08-13):
  1. `_sync_protective_stop` önce eski stop'u iptal edip sonra yenisini kuruyordu.
     Yeni emir reddedilince pozisyon korumasız kalıyor, üstelik `_exchange_stop`
     yine de güncellendiği için bir sonraki tick eşiği geçemeyip TEKRAR DENEMİYORDU.
  2. Restart'ta borsa durumu hiç doğrulanmıyordu.
  3. Borsada olup yerelde olmayan pozisyon (sahipsiz) hiç yönetilmiyordu.
  4. Stop fiyatı tickSize'a yuvarlanmıyordu — Binance -1111 ile reddeder ve bu,
     1 numaralı açığı tetikleyen en olası sebepti.
"""

from unittest.mock import MagicMock, patch

import pytest

from trader import FuturesTrader, _round_tick


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _trader(tick="0.01"):
    with patch("trader.Client") as MockClient:
        cl = MockClient.return_value
        cl.futures_exchange_info.return_value = {
            "symbols": [{
                "symbol": "TESTUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                ],
            }]
        }
        cl.futures_create_order.return_value = {
            "orderId": 1, "avgPrice": "100.0",
            "executedQty": "999999", "status": "FILLED",
        }
        cl.futures_cancel_order.return_value = {}
        cl.futures_account_balance.return_value = [{"asset": "USDT", "balance": "1000.0"}]
        t = FuturesTrader()
        t.balance = 1000.0
        return t


def _pos(stop=95.0, order_id=1):
    return {
        "symbol": "TESTUSDT", "side": "BUY", "qty": 1.0, "entry_price": 100.0,
        "stop_loss": stop, "initial_stop": 95.0, "stop_order_id": order_id,
        "_exchange_stop": 95.0,
    }


def _api_error(code):
    from binance.exceptions import BinanceAPIException
    e = BinanceAPIException(MagicMock(status_code=400), 400, "{}")
    e.code = code
    e.message = f"hata {code}"
    return e


# ── tickSize yuvarlama (açık #4) ─────────────────────────────────────────────

def test_stop_price_rounded_to_tick_size():
    """`round(px, 6)` Binance PRICE_FILTER'ını sağlamaz; -1111 ile reddedilir."""
    t = _trader(tick="0.01")
    t._place_stop("TESTUSDT", "SELL", 95.123456789)
    sent = t.client.futures_create_order.call_args.kwargs
    assert sent["stopPrice"] == 95.12


def test_long_stop_rounds_away_from_price():
    """LONG stop'u aşağı yuvarlanmalı — yukarı yuvarlamak 'hemen tetiklenir' riski."""
    assert _round_tick(95.126, 0.01, side_up=False) == 95.12


def test_short_stop_rounds_away_from_price():
    assert _round_tick(104.874, 0.01, side_up=True) == 104.88


def test_coarse_tick_size_handled():
    """BTCUSDT gibi tickSize=0.10 olan sembollerde de geçerli fiyat üretilmeli."""
    assert _round_tick(43521.237, 0.10, side_up=False) == 43521.20


# ── Stop güncelleme sırası (açık #1 — en kritik) ─────────────────────────────

def test_new_stop_placed_before_old_is_cancelled():
    """Hiçbir an koruma boşluğu olmamalı: önce kur, sonra iptal et."""
    t = _trader()
    calls = []
    t.client.futures_create_order.side_effect = lambda **kw: (
        calls.append("create"), {"orderId": 2})[1]
    t.client.futures_cancel_order.side_effect = lambda **kw: calls.append("cancel")

    pos = _pos(stop=98.0)
    t._sync_protective_stop(pos)

    assert calls == ["create", "cancel"], "eski emir yeni emirden ÖNCE iptal edildi"
    assert pos["stop_order_id"] == 2


def test_failed_stop_update_keeps_old_protection():
    """
    Yeni emir reddedilirse eski stop YERİNDE KALMALI ve pozisyon korumasız
    sayılmamalı. Eski kod eskisini iptal etmişti.
    """
    t = _trader()
    t.client.futures_create_order.side_effect = _api_error(-1111)
    t.client.futures_get_order.return_value = {"status": "NEW"}

    pos = _pos(stop=98.0)
    t._sync_protective_stop(pos)

    t.client.futures_cancel_order.assert_not_called()
    assert pos["stop_order_id"] == 1, "eski koruma kaybedildi"
    assert "TESTUSDT" not in t.unprotected


def test_failed_update_retries_on_next_tick():
    """
    ASIL HATA: başarısız güncellemede `_exchange_stop` yine de yazılıyordu.
    STOP_UPDATE_THRESHOLD %0,1 olduğu için sonraki tick eşiği geçemiyor ve
    koruma bir daha HİÇ denenmiyordu.
    """
    t = _trader()
    t.client.futures_create_order.side_effect = _api_error(-1111)
    t.client.futures_get_order.return_value = {"status": "NEW"}

    pos = _pos(stop=98.0)
    t._sync_protective_stop(pos)
    assert pos["_exchange_stop"] == 95.0, "başarısız güncelleme kaydedilmiş sayıldı"

    # İkinci tick: API düzeldi, güncelleme gerçekleşmeli.
    t.client.futures_create_order.side_effect = None
    t.client.futures_create_order.return_value = {"orderId": 7}
    t._sync_protective_stop(pos)
    assert pos["stop_order_id"] == 7
    assert pos["_exchange_stop"] == 98.0


def test_position_marked_unprotected_when_no_stop_survives():
    """Ne yeni ne eski emir varsa: acil kapama + korumasız işareti."""
    t = _trader()
    t.client.futures_create_order.side_effect = _api_error(-2021)
    t.client.futures_get_order.side_effect = _api_error(-2013)   # emir yok
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]

    pos = _pos(stop=98.0)
    t.positions["TESTUSDT"] = pos
    t._sync_protective_stop(pos)

    assert "TESTUSDT" in t.unprotected


def test_emergency_close_verified_not_assumed():
    """
    Kapama emri gönderilmesi kapandığı anlamına gelmez. Borsa hâlâ açık
    gösteriyorsa pozisyon korumasız listesinde KALMALI.
    """
    t = _trader()
    t.client.futures_get_order.side_effect = _api_error(-2013)
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}       # kapanmamış
    ]
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if kw.get("type") == "STOP_MARKET":
            raise _api_error(-2021)
        return {"orderId": 9}                              # kapama emri "başarılı"

    t.client.futures_create_order.side_effect = create
    pos = _pos(stop=98.0)
    t.positions["TESTUSDT"] = pos
    t._sync_protective_stop(pos)

    assert "TESTUSDT" in t.unprotected, "kapandı varsayıldı ama pozisyon açık"
    assert "TESTUSDT" in t.positions


def test_emergency_close_clears_flag_when_verified_closed():
    t = _trader()
    t.client.futures_get_order.side_effect = _api_error(-2013)
    t.client.futures_position_information.return_value = []    # gerçekten kapandı

    def create(**kw):
        if kw.get("type") == "STOP_MARKET":
            raise _api_error(-2021)
        return {"orderId": 9}

    t.client.futures_create_order.side_effect = create
    pos = _pos(stop=98.0)
    t.positions["TESTUSDT"] = pos
    t._sync_protective_stop(pos)

    assert "TESTUSDT" not in t.unprotected
    assert "TESTUSDT" not in t.positions


# ── Restart doğrulaması (açık #2) ────────────────────────────────────────────

def test_restart_drops_position_closed_while_offline():
    t = _trader()
    t.client.futures_position_information.return_value = []
    t.restore_positions({"TESTUSDT": _pos()})
    assert t.positions == {}


def test_restart_replaces_missing_protective_stop():
    """Bot kapalıyken stop tetiklenmiş/iptal olmuşsa yeniden kurulmalı."""
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]
    t.client.futures_get_order.return_value = {"status": "CANCELED"}
    t.client.futures_create_order.return_value = {"orderId": 42}

    t.restore_positions({"TESTUSDT": _pos()})

    assert t.positions["TESTUSDT"]["stop_order_id"] == 42
    assert "TESTUSDT" not in t.unprotected


def test_restart_keeps_existing_valid_stop():
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}

    t.restore_positions({"TESTUSDT": _pos()})

    t.client.futures_create_order.assert_not_called()
    assert t.positions["TESTUSDT"]["stop_order_id"] == 1


def test_restart_adopts_exchange_quantity_on_mismatch():
    """Bot kapalıyken kısmi çıkış olmuşsa borsa esas alınmalı."""
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "0.4"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}

    t.restore_positions({"TESTUSDT": _pos()})

    assert t.positions["TESTUSDT"]["qty"] == 0.4


def test_restart_marks_all_suspect_when_exchange_unreachable():
    """Borsa okunamıyorsa hiçbir pozisyon 'doğrulanmış' sayılamaz."""
    t = _trader()
    t.client.futures_position_information.side_effect = _api_error(-1001)
    t.restore_positions({"TESTUSDT": _pos()})
    assert "TESTUSDT" in t.unprotected


# ── Sahipsiz pozisyon (açık #3) ──────────────────────────────────────────────

def test_orphan_position_detected():
    """
    `open_position` piyasa emrini gönderdikten sonra, kaydetmeden önce süreç
    ölürse: pozisyon borsada açık, yerel kaydı yok. Eski kod bu yönü hiç
    kontrol etmiyordu.
    """
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "ORPHANUSDT", "positionAmt": "5.0"}
    ]
    t.restore_positions({})
    assert "ORPHANUSDT" in t.orphans


def test_known_position_is_not_flagged_as_orphan():
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}
    t.restore_positions({"TESTUSDT": _pos()})
    assert t.orphans == {}


# ── Kapama hatası sınıflandırması ────────────────────────────────────────────

def test_precision_error_is_not_treated_as_already_closed():
    """
    -1111 bir BİÇİM hatasıdır, "pozisyon zaten kapalı" demek değildir. Eski kod
    onu kapanmış sayıp pozisyonu yerel takipten düşürüyordu — canlı pozisyon
    sahipsiz kalıyordu.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_create_order.side_effect = _api_error(-1111)
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}      # borsada HÂLÂ AÇIK
    ]

    ok, _ = t.close_position("TESTUSDT", 99.0, reason="MANUAL")

    assert ok is False, "-1111 'kapandı' sayıldı"
    assert "TESTUSDT" in t.positions, "canlı pozisyon takipten düşürüldü"


@pytest.mark.parametrize("code", [-2022, -4061, -2011, -1111])
def test_no_error_code_alone_proves_position_closed(code):
    """
    Hiçbir hata kodu tek başına "pozisyon kapandı" kanıtı değildir.
    -2022 reduceOnly reddi, -4061 positionSide uyuşmazlığı, -2011 iptal reddi
    ve -1111 biçim hatası; dördü de pozisyon AÇIKKEN dönebilir. Tek geçerli
    kanıt borsadaki positionAmt.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_create_order.side_effect = _api_error(code)
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]
    ok, _ = t.close_position("TESTUSDT", 99.0, reason="MANUAL")
    assert ok is False and "TESTUSDT" in t.positions


def test_verified_closed_position_is_dropped():
    """Borsa positionAmt=0 diyorsa kapanmış sayılır — tek geçerli kanıt bu."""
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_create_order.side_effect = _api_error(-2022)
    t.client.futures_position_information.return_value = []
    ok, _ = t.close_position("TESTUSDT", 99.0, reason="MANUAL")
    assert ok is True and "TESTUSDT" not in t.positions


# ── Emir gerçekleşme doğrulaması ─────────────────────────────────────────────

def test_ack_response_without_fill_is_not_treated_as_filled():
    """
    Binance'in varsayılan cevabı ACK'tir: `executedQty` ve `avgPrice` 0 gelir.
    Eski kod `avgPrice or price` ile sinyaldeki fiyatı kullanıp pozisyonu
    açılmış sayıyordu — gerçekleşmemiş bir emir için yerel pozisyon kaydı.
    """
    t = _trader()
    t.client.futures_create_order.return_value = {"orderId": 5, "executedQty": "0"}
    t.client.futures_get_order.return_value = {"orderId": 5, "executedQty": "0"}

    ok, msg = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                              qty=1.0, risk_amount=2.0)

    assert ok is False
    assert "TESTUSDT" not in t.positions


def test_partial_fill_uses_actual_quantity():
    """Kısmen dolan emirde hayali miktarla değil, GERÇEK miktarla devam edilmeli."""
    t = _trader()
    t.client.futures_create_order.return_value = {
        "orderId": 5, "executedQty": "0.6", "avgPrice": "100.0", "status": "PARTIALLY_FILLED",
    }
    ok, _ = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                            qty=1.0, risk_amount=2.0)
    assert ok
    assert t.positions["TESTUSDT"]["qty"] == pytest.approx(0.6)


def test_order_carries_unique_client_id():
    """Timeout sonrası emri sorgulayabilmek için benzersiz kimlik şart."""
    t = _trader()
    t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    qty=1.0, risk_amount=2.0)
    market = [c for c in t.client.futures_create_order.call_args_list
              if c.kwargs.get("type") == "MARKET"]
    assert market and market[0].kwargs.get("newClientOrderId")
    assert market[0].kwargs.get("newOrderRespType") == "RESULT"


def test_timeout_recovers_order_state_instead_of_resending():
    """
    Bağlantı hatasında emir körlemesine TEKRAR GÖNDERİLMEZ — borsadan
    sorgulanır. Aksi hâlde çift pozisyon açılır.
    """
    t = _trader()
    # Yalnız İLK çağrı (piyasa emri) timeout alır; sonrakiler (koruyucu stop)
    # normal çalışır — böylece test tam olarak kurtarma yolunu ölçer.
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("bağlantı koptu")
        return {"orderId": 6, "executedQty": "1.0", "avgPrice": "100.0"}

    t.client.futures_create_order.side_effect = flaky
    t.client.futures_get_order.return_value = {
        "orderId": 5, "executedQty": "1.0", "avgPrice": "100.0", "status": "FILLED",
    }
    ok, _ = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                            qty=1.0, risk_amount=2.0)
    assert ok, "emir aslında gerçekleşmişti, kurtarılmalıydı"
    assert t.client.futures_get_order.called, "emir sorgulanmadı"
    market = [c for c in t.client.futures_create_order.call_args_list
              if c.kwargs.get("type") == "MARKET"]
    assert len(market) == 1, "emir körlemesine tekrar gönderildi — çift pozisyon riski"
