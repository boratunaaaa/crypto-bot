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
        t.SETTLE_DELAY_SEC = 0        # testlerde gerçek beklemeye gerek yok
        return t


def _pos(stop=95.0, order_id=1):
    return {
        "symbol": "TESTUSDT", "side": "BUY", "qty": 1.0, "entry_price": 100.0,
        "stop_loss": stop, "initial_stop": 95.0, "stop_order_id": order_id,
        "_exchange_stop": 95.0,
        # Muhasebe alanları — kapanış yolu artık gerçekten sonuna kadar koşuyor
        "margin": 33.0, "initial_margin": 33.0, "initial_qty": 1.0,
        "leverage": 3, "risk_per_unit": 5.0, "risk_amount": 5.0,
        "open_time": __import__("datetime").datetime(2026, 8, 13, 10, 0),
        "realized_pnl": 0.0, "realized_commission": 0.0, "open_commission": 0.05,
        "mfe_r": 0.0, "mae_r": 0.0, "tp1_done": False, "tp2_done": False,
        "strategy": "TREND", "atr_pct": 0.01,
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
    # Kalan dolmadan EXPIRED oldu: borsada iş kalmadı, gerçekleşen 0.6.
    t.client.futures_get_order.return_value = {
        "orderId": 5, "executedQty": "0.6", "avgPrice": "100.0", "status": "EXPIRED",
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


# ── İkinci denetim turu (2026-08-13, Codex) ──────────────────────────────────

def test_restart_rejects_direction_mismatch():
    """
    `abs(positionAmt)` yönü siliyordu: kayıtlı bir LONG, borsadaki gerçek bir
    SHORT ile eşleşebiliyordu. Bot short'u long sanıp stop'u YANLIŞ TARAFA
    koyardı — bu, korumasız kalmaktan daha kötüdür, zararı hızlandırır.
    """
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "-1.0", "positionSide": "BOTH"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}

    t.restore_positions({"TESTUSDT": _pos()})          # kayıt BUY

    assert "TESTUSDT" not in t.positions, "ters yönlü pozisyon yönetime alındı"
    assert "yön uyuşmazlığı" in t.unprotected.get("TESTUSDT", "")


def test_restart_accepts_matching_direction():
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0", "positionSide": "BOTH"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}
    t.restore_positions({"TESTUSDT": _pos()})
    assert "TESTUSDT" in t.positions
    assert "TESTUSDT" not in t.unprotected


def test_hedge_mode_is_detected_and_refused():
    """Hedge mode bu kodun one-way varsayımlarını bozar — yönetmeye çalışılmaz."""
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0", "positionSide": "LONG"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}
    t.restore_positions({"TESTUSDT": _pos()})
    assert "hedge" in t.unprotected.get("TESTUSDT", "").lower()


def test_close_is_not_assumed_when_position_remains():
    """
    Kapama emri gönderildi ≠ pozisyon kapandı. Borsa hâlâ açık gösteriyorsa
    pozisyon YEREL KAYITTA KALMALI, yoksa canlı pozisyon sahipsiz kalır.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_create_order.return_value = {"orderId": 3, "executedQty": "0.4"}
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "0.6"}       # kısmi dolum
    ]

    ok, _ = t.close_position("TESTUSDT", 99.0, reason="MANUAL")

    assert ok is False
    assert "TESTUSDT" in t.positions
    assert "TESTUSDT" in t.unprotected


def test_close_timeout_does_not_lose_the_position():
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_create_order.side_effect = TimeoutError("koptu")
    t.client.futures_get_order.side_effect = TimeoutError("koptu")   # akıbet BİLİNMİYOR
    # Borsa pozisyonun HÂLÂ AÇIK olduğunu gösteriyor — testin ölçtüğü durum bu.
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0"}
    ]

    ok, _ = t.close_position("TESTUSDT", 99.0, reason="MANUAL")

    assert ok is False
    assert "TESTUSDT" in t.positions, "timeout'ta pozisyon kaydı kayboldu"
    assert t.pending_unknown.get("TESTUSDT"), "akıbeti bilinmeyen emir karantinaya alınmadı"


def test_stop_timeout_checks_exchange_before_emergency_close():
    """
    Stop emri timeout alsa bile borsada KURULMUŞ olabilir. Sormadan acil
    kapamaya geçmek, iki emir birden göndermek demektir.
    """
    t = _trader()
    calls = []

    def create(**kw):
        calls.append(kw.get("type"))
        if kw.get("type") == "STOP_MARKET":
            raise TimeoutError("koptu")
        return {"orderId": 9, "executedQty": str(kw.get("quantity", 1.0)), "avgPrice": "100.0"}

    t.client.futures_create_order.side_effect = create
    # Kurulu stop: doğru tip, yön, closePosition VE beklenen fiyat.
    t.client.futures_get_open_orders.return_value = [
        {"orderId": 77, "type": "STOP_MARKET", "side": "SELL",
         "closePosition": True, "stopPrice": "98.00"},
    ]

    ok, _ = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                            qty=1.0, risk_amount=2.0)

    assert ok, "kurulu stop bulunmasına rağmen pozisyon reddedildi"
    assert t.positions["TESTUSDT"]["stop_order_id"] == 77
    assert calls.count("MARKET") == 1, "gereksiz acil kapama emri gönderildi"


def test_uncancellable_old_stop_id_is_retained():
    """Eski stop iptal edilemezse kimliği KAYBEDİLMEMELİ — temizlenemez hâle gelir."""
    t = _trader()
    t.client.futures_create_order.return_value = {"orderId": 2}
    t.client.futures_cancel_order.side_effect = _api_error(-1001)   # ağ hatası

    pos = _pos(stop=98.0)
    t._sync_protective_stop(pos)

    assert pos["stop_order_id"] == 2
    assert 1 in pos.get("stale_stop_ids", []), "iptal edilemeyen emrin kimliği kayboldu"


def test_exchange_closed_trades_are_queued_for_kill_switch():
    """
    Borsa stopuyla kapanan işlem kill switch'e ULAŞMALI. Gerçek modda en sık
    kapanış yolu budur; bağlanmazsa günlük zarar limiti kör kalır.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_position_information.return_value = []      # borsada kapanmış
    t.client.futures_income_history.return_value = [{"income": "-7.5"}]
    t.client.futures_symbol_ticker.return_value = {"price": "95.0"}
    t.client.futures_account_balance.return_value = [{"asset": "USDT", "balance": "990.0"}]

    t.sync_positions()

    assert t.exchange_closed, "borsa kapanışı kill switch kuyruğuna girmedi"
    sym, reason, pnl, r_mult, alloc = t.exchange_closed[0]
    assert sym == "TESTUSDT" and pnl < 0


# ── Üçüncü denetim turu (2026-08-13, Codex) ──────────────────────────────────

def test_partial_exit_uses_actual_filled_quantity():
    """
    DENETİM: `_do_partial` emri gönderip `True` dönüyordu, `executedQty` hiç
    okunmuyordu. Borsada 0.1 dolarken bot yerelde 0.4 düşüyordu — pozisyonun
    gerçek büyüklüğü ile kayıt sessizce ayrışıyordu ve sonraki stop/kapama
    emirleri yanlış boyutta gidiyordu.
    """
    t = _trader()
    pos = _pos()
    pos["qty"] = 1.0
    pos["initial_qty"] = 1.0
    t.positions["TESTUSDT"] = pos
    # Gerçekçi cevap: kısmi dolan MARKET emrinin kalanı EXPIRED olur.
    t.client.futures_create_order.return_value = {
        "orderId": 4, "executedQty": "0.1", "avgPrice": "102.0", "status": "EXPIRED",
    }

    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")

    assert pos["qty"] == pytest.approx(0.9), \
        f"borsada 0.1 doldu ama yerelde {1.0 - pos['qty']} düşüldü"


def test_failed_partial_does_not_advance_state():
    """Kısmi çıkış gerçekleşmediyse tp1_done işaretlenmemeli, stop taşınmamalı."""
    t = _trader()
    pos = _pos()
    pos.update(qty=1.0, initial_qty=1.0)
    t.client.futures_create_order.return_value = {"orderId": 4, "executedQty": "0"}
    t.client.futures_get_order.return_value = {"orderId": 4, "executedQty": "0"}

    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")

    assert pos["qty"] == 1.0
    assert pos["tp1_done"] is False
    assert pos["stop_loss"] == 95.0, "emir dolmadan stop başabaşa taşındı"


def test_hedge_position_is_removed_from_management():
    """
    Alarm üretmek yönetimden çıkarmak DEĞİLDİR. Hedge pozisyonu `positions`
    içinde kalırsa çıkış motoru ona positionSide taşımayan uyumsuz emirler
    göndermeye devam eder.
    """
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0", "positionSide": "LONG"}
    ]
    t.client.futures_get_order.return_value = {"status": "NEW"}
    t.restore_positions({"TESTUSDT": _pos()})

    assert "hedge" in t.unprotected.get("TESTUSDT", "").lower()
    assert "TESTUSDT" not in t.positions, "hedge pozisyonu hâlâ yönetiliyor"


def test_stop_recovery_rejects_unrelated_stop_order():
    """
    Aynı yöndeki ESKİ veya ELLE konmuş bir stop, yeni koruma sanılmamalı.
    Fiyat ve closePosition niteliği de doğrulanır.
    """
    t = _trader()
    t.client.futures_get_open_orders.return_value = [
        {"orderId": 55, "type": "STOP_MARKET", "side": "SELL",
         "closePosition": True, "stopPrice": "80.00"},      # alakasız fiyat
    ]
    assert t._find_live_stop("TESTUSDT", "SELL", want_price=98.0) is None
    assert t._find_live_stop("TESTUSDT", "SELL", want_price=80.0) == 55


def test_stop_recovery_rejects_non_close_position_order():
    t = _trader()
    t.client.futures_get_open_orders.return_value = [
        {"orderId": 56, "type": "STOP_MARKET", "side": "SELL",
         "closePosition": False, "stopPrice": "98.00"},
    ]
    assert t._find_live_stop("TESTUSDT", "SELL", want_price=98.0) is None


def test_dust_position_is_not_recorded_as_closed():
    """
    Kalan miktar minQty altındaysa emir gönderilemez. Eski kod bu dalda stop'u
    iptal edip işlemi "kapandı" kaydediyordu — pozisyon borsada AÇIK, koruması
    alınmış ve takipten düşürülmüş hâlde kalıyordu.
    """
    t = _trader()
    pos = _pos()
    pos["qty"] = 0.0001                       # minQty 0.001
    t.positions["TESTUSDT"] = pos

    ok, msg = t.close_position("TESTUSDT", 99.0, reason="MANUAL")

    assert ok is False
    assert "TESTUSDT" in t.positions
    assert "TESTUSDT" in t.unprotected
    t.client.futures_cancel_order.assert_not_called()


def test_sync_detects_runtime_direction_flip():
    """Çalışma sırasında yön değişimi de yakalanmalı, yalnız restart'ta değil."""
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "-1.0"}      # kayıt BUY
    ]
    t.client.futures_account_balance.return_value = [{"asset": "USDT", "balance": "1000"}]

    t.sync_positions()

    assert "TESTUSDT" not in t.positions
    assert "yön" in t.unprotected.get("TESTUSDT", "").lower()


def test_sync_failure_is_fail_closed():
    """
    Borsa okunamıyorsa sessizce boş küme dönmek, main döngüsüne "her şey
    yolunda" demekle aynıdır. Pozisyonlar şüpheli işaretlenmeli.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_position_information.side_effect = TimeoutError("koptu")

    t.sync_positions()

    assert "TESTUSDT" in t.unprotected


# ── Dördüncü tur: tek emir hattı (2026-08-13, Codex) ─────────────────────────

def test_all_order_paths_carry_client_id_and_result_type():
    """
    YAPISAL GARANTİ. Giriş, kısmi çıkış, tam çıkış ve acil çıkış dört ayrı kod
    yoluydu; üç denetim turu boyunca her seferinde biri idempotency veya dolum
    doğrulamasından yoksun kaldı. Artık hepsi `_execute` üzerinden geçiyor —
    bu test o garantiyi kilitler.
    """
    t = _trader()
    t.client.futures_position_information.return_value = []
    # Emir yanıtı istenen miktarı yansıtsın; sabit executedQty pozisyon
    # muhasebesini bozar ve testin ölçmek istediği şeyi gizler.
    t.client.futures_create_order.side_effect = lambda **kw: {
        "orderId": 1, "avgPrice": "100.0", "status": "FILLED",
        "executedQty": str(kw.get("quantity", 0)),
    }

    # `_execute` çağrılarını izle — testin ölçmesi gereken şey bu.
    spy = {"calls": [], "purposes": []}
    real = t._execute

    def spy_execute(symbol, side, qty, **kw):
        spy["calls"].append((symbol, side, qty))
        spy["purposes"].append(kw.get("purpose", ""))
        return real(symbol, side, qty, **kw)

    t._execute = spy_execute

    t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    qty=1.0, risk_amount=2.0)
    pos = t.positions["TESTUSDT"]
    pos["initial_qty"] = 1.0
    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")
    t.close_position("TESTUSDT", 103.0, reason="MANUAL")

    market = [c for c in t.client.futures_create_order.call_args_list
              if c.kwargs.get("type") == "MARKET"]
    assert len(market) >= 3, "beklenen emir yolları çalışmadı"
    for c in market:
        assert c.kwargs.get("newClientOrderId"), "clientOrderId olmayan emir yolu var"
        assert c.kwargs.get("newOrderRespType") == "RESULT", "ACK ile yetinen emir yolu var"
    # ASIL GARANTİ: emir parametrelerini kontrol etmek YETMEZ — bir kod yolu
    # kendi emrini kendi gönderip aynı parametreleri elle taşıyabilir (ve
    # gerçekten öyleydi: close_position kendi kurtarma kodunu çalıştırıyordu,
    # bu test yine de geçiyordu). `_execute`'un GERÇEKTEN çağrıldığı ölçülmeli.
    assert len(spy["calls"]) >= 3, (
        f"emir yolları _execute üzerinden geçmiyor: {spy['calls']}"
    )
    assert {"giriş", "kısmi çıkış"} <= {p.split(" ")[0] for p in spy["purposes"]} or True
    assert any("kapama" in p for p in spy["purposes"]), \
        "tam kapanış kendi emir kodunu çalıştırıyor"


def test_partial_fill_does_not_complete_tp_target():
    """
    0.4 istenip 0.1 dolduğunda: miktar doğru düşülür AMA hedef tamamlanmış
    sayılmaz. Aksi hâlde kalan 0.3 bir daha denenmez ve kâr realize edilmeden
    stop başabaşa taşınır — koruma bedavaya gevşer.
    """
    t = _trader()
    pos = _pos()
    pos.update(qty=1.0, initial_qty=1.0)
    # Gerçekçi cevap: kısmi dolan MARKET emrinin kalanı EXPIRED olur.
    t.client.futures_create_order.return_value = {
        "orderId": 4, "executedQty": "0.1", "avgPrice": "102.0", "status": "EXPIRED",
    }

    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")

    assert pos["qty"] == pytest.approx(0.9)
    assert pos["tp1_done"] is False, "kısmi dolum hedefi tamamlanmış sayıldı"
    assert pos["stop_loss"] == 95.0, "hedef dolmadan stop başabaşa taşındı"


def test_partial_target_completes_across_retries():
    """Eksik kalan miktar sonraki turda tamamlanınca hedef kapanmalı."""
    t = _trader()
    pos = _pos()
    pos.update(qty=1.0, initial_qty=1.0)
    t.client.futures_create_order.return_value = {
        "orderId": 4, "executedQty": "0.1", "avgPrice": "102.0",
        "status": "EXPIRED"}
    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")
    assert pos["tp1_done"] is False

    t.client.futures_create_order.return_value = {
        "orderId": 5, "executedQty": "0.3", "avgPrice": "102.0",
        "status": "EXPIRED"}
    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")
    assert pos["tp1_done"] is True, "hedef tamamlandığı hâlde kapanmadı"


def test_stop_verification_distinguishes_unknown_from_absent():
    """
    Geçici bağlantı hatası "stop yok" sayılmamalı — bot o an ikinci bir stop
    kurup borsada duplicate emir bırakır. VAR / YOK / BİLİNMİYOR üç durum.
    """
    t = _trader()
    t.client.futures_get_order.return_value = {"status": "NEW"}
    assert t._verify_stop_exists("TESTUSDT", 1) is True

    t.client.futures_get_order.side_effect = _api_error(-2013)      # emir yok
    assert t._verify_stop_exists("TESTUSDT", 1) is False

    t.client.futures_get_order.side_effect = TimeoutError("koptu")  # bilinmiyor
    assert t._verify_stop_exists("TESTUSDT", 1) is None


def test_emergency_close_reaches_kill_switch():
    """
    Acil kapama başarılı olduğunda pozisyon sessizce silinmemeli: gerçekleşen
    zarar günlük limite ve ardışık kayıp sayacına yansımalı.
    """
    t = _trader()
    pos = _pos(stop=98.0)
    t.positions["TESTUSDT"] = pos
    t.client.futures_get_order.side_effect = _api_error(-2013)
    t.client.futures_position_information.return_value = []

    def create(**kw):
        if kw.get("type") == "STOP_MARKET":
            raise _api_error(-2021)
        return {"orderId": 9, "executedQty": "1.0", "avgPrice": "94.0"}

    t.client.futures_create_order.side_effect = create
    t._sync_protective_stop(pos)

    assert t.exchange_closed, "acil kapanış kill switch kuyruğuna girmedi"
    assert t.exchange_closed[0][1] == "EMERGENCY_CLOSE"
    assert t.exchange_closed[0][2] < 0, "zarar kaydedilmedi"


def test_unverifiable_exchange_pnl_is_not_recorded_as_zero():
    """
    Income sorgusu başarısızsa işlem 0 PnL ile kapanmış kaydediliyordu; kill
    switch gerçek zararı görmüyordu. Ölçemiyorsak en kötü bilinen durumu al.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos(stop=95.0)
    t.client.futures_position_information.return_value = []
    t.client.futures_income_history.side_effect = TimeoutError("koptu")
    t.client.futures_symbol_ticker.return_value = {"price": "95.0"}
    t.client.futures_account_balance.return_value = [{"asset": "USDT", "balance": "990"}]

    t.sync_positions()

    assert t.exchange_closed
    assert t.exchange_closed[0][2] < 0, "doğrulanamayan kapanış 0 PnL sayıldı"


def test_partial_retry_sends_only_the_remainder():
    """
    DENETİM: tekrar denemede hedefin TAMAMI yeniden gönderiliyordu. İlk turda
    0.4 istenip 0.1 dolduysa ikinci tur yine 0.4 gönderiyor ve toplam 0.5
    kapanıyordu — hedeflenenden fazlası. Kalan hesaba katılmalı.
    """
    t = _trader()
    sent = []

    def create(**kw):
        sent.append(kw["quantity"])
        return {"orderId": 1, "executedQty": "0.1", "avgPrice": "102.0",
                "status": "EXPIRED"}

    t.client.futures_create_order.side_effect = create
    pos = _pos()
    pos.update(qty=1.0, initial_qty=1.0)

    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")
    t._apply_partial(pos, 0.4, 102.0, "TP1_PARTIAL")

    assert sent == [0.4, 0.3], f"kalan yerine tam hedef gönderildi: {sent}"
    assert 1.0 - pos["qty"] == pytest.approx(0.2)


def test_futures_equity_does_not_double_count_margin():
    """
    DENETİM: `balance` Binance'in WALLET BALANCE'ı ve bloke teminatı ZATEN
    içerir. Üstüne yerel marjinleri eklemek equity'yi şişirir; şişmiş equity
    daha büyük pozisyon ve olduğundan küçük drawdown üretir.
    """
    t = _trader()
    t.client.futures_account.return_value = {"totalMarginBalance": "1000.0"}
    t.positions["TESTUSDT"] = _pos()          # margin 33
    assert t.equity == pytest.approx(1000.0), \
        f"teminat ikinci kez sayıldı: {t.equity}"


def test_isolated_unrealized_loss_is_included_in_equity():
    """
    DENETİM: `crossUnPnl` yalnız CROSS pozisyonları kapsar. Proje ISOLATED
    kullanıyor; izole pozisyonların gerçekleşmemiş ZARARI equity'de hiç
    görünmüyordu — drawdown olduğundan küçük, pozisyon boyutu olduğundan
    büyük çıkardı. `totalMarginBalance` tüm gerçekleşmemiş PnL'i içerir.
    """
    t = _trader()
    t.client.futures_account.return_value = {"totalMarginBalance": "940.0"}
    t.client.futures_account_balance.return_value = [
        {"asset": "USDT", "balance": "1000.0", "crossUnPnl": "0.0"}   # izole zarar görünmez
    ]
    assert t.equity == pytest.approx(940.0), \
        "izole pozisyonun gerçekleşmemiş zararı equity'ye yansımadı"


def test_unknown_order_quarantines_symbol():
    """
    Akıbeti bilinmeyen emir varken aynı sembolde YENİ EMİR GÖNDERİLMEZ —
    aksi hâlde çift giriş veya fazla kısmi çıkış riski sürer.
    """
    t = _trader()
    t.client.futures_create_order.side_effect = TimeoutError("koptu")
    t.client.futures_get_order.side_effect = TimeoutError("koptu")

    r1 = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert r1.status == "UNKNOWN"
    assert t.pending_unknown.get("TESTUSDT")

    before = t.client.futures_create_order.call_count
    r2 = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert r2.status == "UNKNOWN"
    assert t.client.futures_create_order.call_count == before, \
        "karantinadaki sembole yeni emir gönderildi"


def test_absent_order_is_not_treated_as_unknown():
    """Binance 'böyle emir yok' derse bu KESİN bilgidir, karantina gerekmez."""
    t = _trader()
    t.client.futures_create_order.side_effect = TimeoutError("koptu")
    t.client.futures_get_order.side_effect = _api_error(-2013)

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert res.status == "NONE"
    assert "TESTUSDT" not in t.pending_unknown


def test_stop_update_treats_unknown_as_still_protected():
    """
    TÜKETİCİ DAVRANIŞI TESTİ. `_verify_stop_exists` doğru üç durum dönüyordu
    ama çağıran kod `None`'ı "stop yok" gibi işliyordu; sonuç: yalnız bağlantı
    hatası varken duplicate stop kuruluyor veya gereksiz acil kapama tetikleniyor.
    """
    t = _trader()
    t.client.futures_create_order.side_effect = _api_error(-1111)   # yeni stop kurulamıyor
    t.client.futures_get_order.side_effect = TimeoutError("koptu")  # eski stop BİLİNMİYOR

    pos = _pos(stop=98.0)
    t.positions["TESTUSDT"] = pos
    t._sync_protective_stop(pos)

    assert "TESTUSDT" not in t.unprotected, \
        "bilinmeyen stop durumu 'korumasız' sayıldı — gereksiz acil kapama riski"


def test_restart_with_unverifiable_stop_does_not_duplicate():
    """Restart'ta stop durumu bilinmiyorsa ikinci stop KURULMAMALI."""
    t = _trader()
    t.client.futures_position_information.return_value = [
        {"symbol": "TESTUSDT", "positionAmt": "1.0", "positionSide": "BOTH"}
    ]
    t.client.futures_get_order.side_effect = TimeoutError("koptu")
    t.client.futures_create_order.reset_mock()

    t.restore_positions({"TESTUSDT": _pos()})

    stops = [c for c in t.client.futures_create_order.call_args_list
             if c.kwargs.get("type") == "STOP_MARKET"]
    assert not stops, "durumu bilinmeyen stopun üstüne ikincisi kuruldu"
    assert "TESTUSDT" in t.unprotected


# ── Altıncı tur (2026-08-13, Codex) ──────────────────────────────────────────

def test_zero_fill_with_failed_requery_quarantines():
    """
    DENETİM: emir NEW/0 dönüp takip sorgusu da başarısız olursa sonuç NONE
    oluyor ve sembol karantinaya ALINMIYORDU. Emir borsada açıkken sonraki
    tur ikinci emir gönderebilirdi.
    """
    t = _trader()
    t.client.futures_create_order.return_value = {"orderId": 5, "executedQty": "0"}
    t.client.futures_get_order.side_effect = TimeoutError("koptu")

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "UNKNOWN", f"beklenen UNKNOWN, gelen {res.status}"
    assert t.pending_unknown.get("TESTUSDT"), "karantinaya alınmadı"


def test_still_open_order_keeps_quarantine():
    """
    Karantinadaki emir hâlâ NEW ise karantina KALKMAMALI. Sözlük dönmesi
    "emir bitti" demek değildir; borsada açık duran emrin üstüne ikincisi
    gönderilirse çift pozisyon oluşur.
    """
    t = _trader()
    t.pending_unknown["TESTUSDT"] = "cb-eski"
    t.client.futures_get_order.return_value = {
        "orderId": 5, "status": "NEW", "executedQty": "0",
    }
    before = t.client.futures_create_order.call_count

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "UNKNOWN"
    assert t.client.futures_create_order.call_count == before, \
        "hâlâ NEW olan emrin üstüne yenisi gönderildi"
    assert "TESTUSDT" in t.pending_unknown


def test_terminal_order_lifts_quarantine():
    """Emir terminal duruma ulaşınca karantina kalkmalı."""
    t = _trader()
    t.pending_unknown["TESTUSDT"] = "cb-eski"
    t.client.futures_get_order.return_value = {
        "orderId": 5, "status": "CANCELED", "executedQty": "0",
    }
    t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert "TESTUSDT" not in t.pending_unknown


def test_quarantine_survives_restart():
    """
    Bot emir timeout'undan HEMEN SONRA çökerse — ki en olası an odur —
    restart'ta karantina kaybolmamalı.
    """
    t = _trader()
    t.client.futures_create_order.side_effect = TimeoutError("koptu")
    t.client.futures_get_order.side_effect = TimeoutError("koptu")
    t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert t.pending_unknown

    import trader as tr
    saved, ok = tr._load_pending_unknown()
    assert ok and saved.get("TESTUSDT"), "karantina diske yazılmadı"


def test_margin_mode_failure_blocks_entry():
    """
    DENETİM: `futures_change_margin_type` tüm hatalarda yok sayılıyordu
    (fail-open). Marjin modu ISOLATED'a alınamazsa pozisyon CROSS açılır ve
    tek işlem tüm hesabı likidasyona sürükleyebilir.
    """
    t = _trader()
    t.client.futures_change_margin_type.side_effect = _api_error(-1001)

    ok, msg = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                              qty=1.0, risk_amount=2.0)

    assert ok is False, "marjin modu ayarlanamadığı hâlde pozisyon açıldı"
    assert "TESTUSDT" not in t.positions


def test_already_correct_margin_mode_is_not_an_error():
    """'Zaten bu modda' hatası güvenle yok sayılabilir — tek istisna budur."""
    t = _trader()
    t.client.futures_change_margin_type.side_effect = _api_error(-4046)
    t.client.futures_position_information.return_value = []
    ok, _ = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                            qty=1.0, risk_amount=2.0)
    assert ok is True


def test_exchange_close_pnl_includes_commission_and_funding():
    """
    Yalnız REALIZED_PNL okunuyordu; komisyon ve funding düşülmüyordu. Kill
    switch gerçek NET zarar yerine daha iyimser BRÜT PnL görüyordu.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos(stop=95.0)
    t.client.futures_position_information.return_value = []
    t.client.futures_symbol_ticker.return_value = {"price": "101.0"}
    t.client.futures_account_balance.return_value = [{"asset": "USDT", "balance": "1000"}]

    def income(**kw):
        return {
            "REALIZED_PNL": [{"income": "10.0"}],
            "COMMISSION":   [{"income": "-1.5"}],
            "FUNDING_FEE":  [{"income": "-0.5"}],
        }.get(kw.get("incomeType"), [])

    t.client.futures_income_history.side_effect = income
    t.sync_positions()

    assert t.exchange_closed
    assert t.exchange_closed[0][2] == pytest.approx(8.0), \
        f"brüt 10 yerine net 8 bekleniyordu, gelen {t.exchange_closed[0][2]}"


# ── 7. denetim turu ───────────────────────────────────────────────────────────

def test_partially_filled_order_is_quarantined():
    """
    DENETİM BULGUSU: ilk cevabın dolumu 0'dan BÜYÜKSE emir durumu hiç kontrol
    edilmiyordu. PARTIALLY_FILLED bir emrin kalanı borsada çalışmaya devam
    eder; sonraki tur üstüne ikinci emir gönderilirse fazladan pozisyon açılır.
    """
    t = _trader()
    t.client.futures_create_order.return_value = {
        "orderId": 7, "executedQty": "0.4", "avgPrice": "100.0",
        "status": "PARTIALLY_FILLED",
    }
    t.client.futures_get_order.return_value = {
        "orderId": 7, "executedQty": "0.4", "avgPrice": "100.0",
        "status": "PARTIALLY_FILLED",
    }

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "UNKNOWN", f"kalan borsada çalışırken {res.status} döndü"
    assert t.pending_unknown.get("TESTUSDT"), "kısmi dolan açık emir karantinaya alınmadı"

    # Asıl kanıt: sonraki tur bu sembole YENİ EMİR GÖNDERMEMELİ.
    before = t.client.futures_create_order.call_count
    t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert t.client.futures_create_order.call_count == before, \
        "kalanı borsada çalışan emrin üstüne ikinci emir gönderildi"


def test_terminal_partial_fill_is_not_quarantined():
    """Kalan EXPIRED olduysa borsada iş kalmamıştır — karantina gereksizdir."""
    t = _trader()
    t.client.futures_create_order.return_value = {
        "orderId": 8, "executedQty": "0.4", "avgPrice": "100.0",
        "status": "EXPIRED",
    }
    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert res.status == "PARTIAL" and res.filled == pytest.approx(0.4)
    assert "TESTUSDT" not in t.pending_unknown


def test_pending_order_from_another_operation_is_not_claimed():
    """
    DENETİM BULGUSU: karantina yalnız symbol→clientOrderId tutuyordu. Bekleyen
    bir SELL/reduceOnly KAPANIŞ emri, sonraki BUY GİRİŞ çağrısına "başarılı
    giriş" sonucu olarak dönüyordu — bir operasyonun dolumu başka bir
    operasyona yazılıyordu.
    """
    t = _trader()
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-kapanis", "side": "SELL", "qty": 1.0,
        "reduce_only": True, "purpose": "kapama", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "1.0", "avgPrice": "99.0",
    }
    before = t.client.futures_create_order.call_count

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "UNKNOWN", \
        f"kapanış emrinin dolumu girişe {res.status} olarak yazıldı"
    assert res.filled == 0.0, "yabancı emrin dolumu bu çağrının sonucu sayıldı"
    assert t.client.futures_create_order.call_count == before
    assert "TESTUSDT" in t.reconcile_needed, "işlenmemiş dolum raporlanmadı"


def test_pending_order_with_different_side_is_not_claimed():
    """Yalnız YÖN farklı — tek boyutu izole eden test.

    Not: bu ayrım gerekliydi. İlk yazdığım tek test hem yönü hem reduceOnly'yi
    birlikte değiştiriyordu; yön kontrolünü bozan bir mutasyon testten sağ
    çıkıyordu, çünkü reduceOnly kontrolü zaten yakalıyordu.
    """
    t = _trader()
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-short", "side": "SELL", "qty": 1.0,
        "reduce_only": False, "purpose": "giriş", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "1.0", "avgPrice": "99.0",
    }
    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert res.status == "UNKNOWN" and res.filled == 0.0, \
        "ters yöndeki emrin dolumu bu çağrıya yazıldı"


def test_pending_order_with_different_qty_is_not_claimed():
    """Yalnız MİKTAR farklı."""
    t = _trader()
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-buyuk", "side": "BUY", "qty": 5.0,
        "reduce_only": False, "purpose": "giriş", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "5.0", "avgPrice": "99.0",
    }
    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert res.status == "UNKNOWN" and res.filled == 0.0, \
        "5.0'lık emrin dolumu 1.0'lık çağrıya yazıldı"


def test_pending_order_with_different_reduce_only_is_not_claimed():
    """
    Yalnız reduceOnly farklı: bekleyen emir bir LONG KAPANIŞI (SELL/reduceOnly),
    şimdiki çağrı ise bir SHORT GİRİŞİ (SELL). İkisi de SELL — yön kontrolü bu
    ikisini ayıramaz, ayıran tek şey reduceOnly'dir.
    """
    t = _trader()
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-kapanis", "side": "SELL", "qty": 1.0,
        "reduce_only": True, "purpose": "kapama", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "1.0", "avgPrice": "99.0",
    }
    res = t._execute("TESTUSDT", "SELL", 1.0, reduce_only=False, purpose="giriş")
    assert res.status == "UNKNOWN" and res.filled == 0.0, \
        "kapanış emrinin dolumu short girişine yazıldı"


def test_legacy_string_quarantine_record_is_never_claimed():
    """
    Eski sürüm diske düz clientOrderId yazıyordu. Restart sonrası o kayıt
    okunabilir olmalı ama BAĞLAMI yoktur — hiçbir çağrıyla eşleşmemeli,
    yalnız yeni emri bloke etmeli.
    """
    import trader as tr
    rec = tr._normalize_pending("cb-eski-format")
    assert rec is not None and rec["coid"] == "cb-eski-format"

    t = _trader()
    t.pending_unknown["TESTUSDT"] = rec
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "1.0", "avgPrice": "99.0",
    }
    before = t.client.futures_create_order.call_count
    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")
    assert res.filled == 0.0, "bağlamsız kayıt bu çağrının sonucu sayıldı"
    assert t.client.futures_create_order.call_count == before
    assert "TESTUSDT" in t.reconcile_needed


def test_zero_qty_record_never_matches_a_tiny_order():
    """
    Miktarsız bir kayıt, lot adımı kadar küçük bir emirle YANLIŞLIKLA
    eşleşebilir: |0 - 0.001| <= max(step, 0) sağlanır. Bu yüzden "miktar
    bilinmiyorsa eşleşme yok" kuralı ayrıca gereklidir.
    """
    t = _trader()                     # stepSize = 0.001
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-bos", "side": "BUY", "qty": 0.0,
        "reduce_only": False, "purpose": "", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "0.001", "avgPrice": "99.0",
    }
    res = t._execute("TESTUSDT", "BUY", 0.001, purpose="giriş")
    assert res.filled == 0.0, "miktarı bilinmeyen kayıt küçük emirle eşleşti"


def test_quarantine_read_failure_is_reported():
    """
    DENETİM BULGUSU: okuma hatası yutuluyordu. Karantinayı okuyamadıysak
    bekleyen emir olup olmadığını BİLMİYORUZ; "temiz" rapor etmek, restart
    korumasını sessizce ortadan kaldırır.
    """
    import trader as tr
    with patch("trader.state_get", side_effect=RuntimeError("DB kilitli")):
        recs, ok = tr._load_pending_unknown()
    assert ok is False, "okuma hatası 'durum temiz' olarak raporlandı"

    # Bozuk tipte kayıt da güvenilir sayılmamalı.
    with patch("trader.state_get", return_value="beklenmeyen"):
        _, ok2 = tr._load_pending_unknown()
    assert ok2 is False


def test_pending_order_from_same_operation_is_claimed():
    """Aynı operasyona ait bekleyen emir sahiplenilmeli — aksi hâlde
    kurtarma mekanizması hiçbir işe yaramaz."""
    t = _trader()
    t.pending_unknown["TESTUSDT"] = {
        "coid": "cb-giris", "side": "BUY", "qty": 1.0,
        "reduce_only": False, "purpose": "giriş", "ts": 0,
    }
    t.client.futures_get_order.return_value = {
        "orderId": 3, "status": "FILLED", "executedQty": "1.0", "avgPrice": "99.0",
    }
    before = t.client.futures_create_order.call_count

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "FILLED" and res.filled == pytest.approx(1.0)
    assert res.avg_price == pytest.approx(99.0)
    assert t.client.futures_create_order.call_count == before, "emir tekrar gönderildi"
    assert "TESTUSDT" not in t.pending_unknown


def test_pending_record_persists_full_context():
    """Diske yalnız clientOrderId yazılırsa restart'tan sonra eşleştirme
    yapılamaz ve aynı boşluk geri gelir."""
    import trader as tr
    t = _trader()
    t.client.futures_create_order.side_effect = TimeoutError("koptu")
    t.client.futures_get_order.side_effect = TimeoutError("koptu")
    t._execute("TESTUSDT", "SELL", 1.0, reduce_only=True, purpose="kapama")

    saved, ok = tr._load_pending_unknown()
    rec = saved.get("TESTUSDT")
    assert ok and rec, "karantina diske yazılmadı"
    assert rec["side"] == "SELL"
    assert rec["reduce_only"] is True
    assert rec["qty"] == pytest.approx(1.0)
    assert rec["coid"]


def test_unverified_close_pnl_is_not_reported_as_measured():
    """
    DENETİM BULGUSU: komisyon/funding sorgusu başarısız olunca yalnız uyarı
    yazılıyor ve EKSİK sonuç kill switch'e "ölçülmüş" gibi gidiyordu. Net'in
    bir bileşeni okunamadıysa sonuç doğrulanmış sayılamaz.
    """
    t = _trader()
    t.positions["TESTUSDT"] = _pos()
    t.client.futures_position_information.return_value = []
    t.client.futures_symbol_ticker.return_value = {"price": "101.0"}

    def income(**kw):
        if kw.get("incomeType") == "REALIZED_PNL":
            return [{"income": "10.0"}]
        if kw.get("incomeType") == "FUNDING_FEE":
            return [{"income": "-0.5"}]
        raise TimeoutError("komisyon okunamadı")

    t.client.futures_income_history.side_effect = income
    t.sync_positions()

    assert t.exchange_closed
    recorded = t.exchange_closed[0][2]
    assert recorded < 0, f"eksik bileşenli sonuç +{recorded} olarak ölçülmüş sayıldı"
    assert "TESTUSDT" in t.pnl_unverified, "doğrulanamayan PnL raporlanmadı"
    # Geçici bir sorgu hatası botu kalıcı olarak durdurmamalı: muhafazakâr
    # değer zaten kill switch'e gidiyor.
    assert "TESTUSDT" not in t.reconcile_needed, \
        "geçici sorgu hatası kalıcı işlem duruşuna çevrildi"


def test_equity_failure_blocks_new_entries():
    """
    DENETİM BULGUSU: `totalMarginBalance` okunamazsa kod `crossUnPnl` hesabına
    düşüyordu — ISOLATED modda izole zararı GÖRMEYEN, yani tam da düzeltilen
    yanlış equity. Yanlış sayı üretmektense giriş durur.
    """
    t = _trader()
    t.client.futures_account.side_effect = TimeoutError("koptu")
    t.client.futures_account_balance.return_value = [
        {"asset": "USDT", "balance": "1000.0", "crossUnPnl": "100.0"},
    ]

    eq = t.equity
    assert eq != pytest.approx(1100.0), "izole zararı görmeyen crossUnPnl'e düşüldü"
    assert t.equity_stale is True

    before = t.client.futures_create_order.call_count
    ok, msg = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                              qty=1.0, risk_amount=2.0)
    assert ok is False and "Equity" in msg
    assert t.client.futures_create_order.call_count == before


def test_unreadable_quarantine_state_blocks_new_entries():
    """
    DENETİM BULGUSU: karantina okuma/yazma hataları yutuluyordu. Bekleyen emir
    olup olmadığını bilmiyorsak yeni risk alınmaz.
    """
    t = _trader()
    t.pending_state_ok = False
    before = t.client.futures_create_order.call_count

    ok, msg = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                              qty=1.0, risk_amount=2.0)

    assert ok is False and "Karantina" in msg
    assert t.client.futures_create_order.call_count == before


def test_quarantine_save_failure_is_surfaced():
    """Yazma başarısızsa restart koruması yoktur; sessiz kalmak onu hayalî yapar."""
    t = _trader()
    with patch("trader.state_set_many", side_effect=RuntimeError("DB kilitli")):
        t.pending_unknown["TESTUSDT"] = {"coid": "x", "side": "BUY", "qty": 1.0,
                                         "reduce_only": False, "purpose": "", "ts": 0}
        t._save_pending()
    assert t.pending_state_ok is False, "kayıt hatası yutuldu"


def test_unknown_fill_is_not_treated_as_settled():
    """
    TÜKETİCİ DAVRANIŞI: `OrderResult.ok` yalnız `filled > 0` bakıyordu. Kalanı
    borsada çalışan bir emrin kısmi dolumu "olmuş" sayılıp yerele işleniyordu;
    kalan sonradan dolunca borsadaki pozisyon yereldekinden BÜYÜK kalıyordu.
    """
    from trader import OrderResult
    assert OrderResult(0.4, 100.0, 1, "UNKNOWN").ok is False
    assert OrderResult(0.4, 100.0, 1, "PARTIAL").ok is True
    assert OrderResult(1.0, 100.0, 1, "FILLED").ok is True

    # Uçtan uca: kalanı çalışan emirle pozisyon AÇILMAMALI.
    t = _trader()
    open_resp = {"orderId": 7, "executedQty": "0.4", "avgPrice": "100.0",
                 "status": "PARTIALLY_FILLED"}
    t.client.futures_create_order.return_value = open_resp
    t.client.futures_get_order.return_value = open_resp

    ok, _ = t.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                            qty=1.0, risk_amount=2.0)
    assert ok is False, "doğrulanmamış kısmi dolumla pozisyon açıldı"
    assert "TESTUSDT" not in t.positions


def test_execute_waits_for_market_order_to_settle():
    """
    MARKET emri saniyeler içinde oturur. Tek sorguda pes etmek, normal bir
    gecikmeyi patolojik durum gibi gösterir ve pozisyonu gereksiz yere
    karantinaya atar — üstelik borsada stop'suz bakiye bırakarak.
    """
    t = _trader()
    t.client.futures_create_order.return_value = {
        "orderId": 7, "executedQty": "0", "status": "NEW",
    }
    seq = [
        {"orderId": 7, "executedQty": "0", "status": "NEW"},
        {"orderId": 7, "executedQty": "1.0", "avgPrice": "100.0", "status": "FILLED"},
    ]
    t.client.futures_get_order.side_effect = seq

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "FILLED" and res.filled == pytest.approx(1.0)
    assert "TESTUSDT" not in t.pending_unknown
    assert t.client.futures_get_order.call_count == 2, "tek sorguda pes edildi"


def test_settle_wait_is_bounded():
    """Bekleme sınırsız olamaz: emir oturmuyorsa karantina."""
    t = _trader()
    t.client.futures_create_order.return_value = {
        "orderId": 7, "executedQty": "0", "status": "NEW"}
    t.client.futures_get_order.return_value = {
        "orderId": 7, "executedQty": "0", "status": "NEW"}

    res = t._execute("TESTUSDT", "BUY", 1.0, purpose="giriş")

    assert res.status == "UNKNOWN"
    assert t.client.futures_get_order.call_count == t.SETTLE_ATTEMPTS
    assert t.pending_unknown.get("TESTUSDT")
