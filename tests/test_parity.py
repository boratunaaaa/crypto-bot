"""
PARİTE TESTİ — gerçek paraya geçişin ön koşulu.

Bulunan hata (2026-08-12): trailing/breakeven mantığının TAMAMI yalnızca
`PaperPortfolio.check_positions()` içindeydi. `FuturesTrader.check_positions()`
düz stop kontrolünden ibaretti ve `open_position` Binance'e `closePosition=True`
ile sabit bir TAKE_PROFIT_MARKET emri koyuyordu.

Sonuç: `PAPER_TRADING=False` yapıldığı anda strateji sessizce değişiyordu —
"kazananı koştur" mekanizması gerçek modda hiç çalışmıyordu. Ölçülen +$290
kârın %117'si tam da o mekanizmanın ürettiği 2 işlemden geliyordu.

Bu testler iki modun AYNI çıkış kararını ürettiğini doğrular.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import config
from position import EXIT_TP1, EXIT_TP2, EXIT_TRAIL_STOP, EXIT_INITIAL_STOP
from trader import PaperPortfolio, FuturesTrader, ExitEngineMixin


PRICE_PATH_WINNER = [100.0] + [100.0 + i * 0.4 for i in range(1, 80)] + \
                    [131.0, 126.0, 120.0, 112.0, 104.0]
PRICE_PATH_LOSER  = [100.0, 99.5, 99.0, 98.5, 97.5]


def _make_futures_trader():
    """Ağa çıkmayan FuturesTrader — tüm Binance çağrıları sahte."""
    with patch("trader.Client") as MockClient:
        cl = MockClient.return_value
        cl.futures_exchange_info.return_value = {
            "symbols": [{
                "symbol": "TESTUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ],
            }]
        }
        # Emir yanıtı istenen miktarı tamamen doldurmuş gibi döner —
        # kısmi çıkış artık executedQty okuduğu için bu şart.
        cl.futures_create_order.side_effect = lambda **kw: {
            "orderId": 1, "avgPrice": "100.0", "status": "FILLED",
            "executedQty": str(kw.get("quantity", 999999)),
        }
        cl.futures_change_leverage.return_value = {}
        cl.futures_change_margin_type.return_value = {}
        cl.futures_cancel_order.return_value = {}
        cl.futures_account_balance.return_value = [{"asset": "USDT", "balance": "1000.0"}]
        t = FuturesTrader()
        t.balance = 1000.0
        return t


def _run(trader, path):
    """Fiyat yolunu trader'a ver, çıkış olaylarını topla."""
    events = []
    for px in path:
        closed = trader.check_positions({"TESTUSDT": px})
        for sym, reason, pnl, r in closed:
            events.append(("CLOSE", reason))
        if not trader.positions:
            break
    return events


def _partial_events(trader):
    return [
        e["action"] for e in trader.trade_log if e["action"].startswith("PARTIAL")
    ]


# ── Ana parite testi ──────────────────────────────────────────────────────────

def test_paper_and_futures_produce_identical_exit_decisions_on_winner():
    paper = PaperPortfolio(1000.0)
    paper.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                        atr_pct=0.01, qty=1.5, risk_amount=3.0)

    fut = _make_futures_trader()
    fut.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                      atr_pct=0.01, qty=1.5, risk_amount=3.0)

    ev_paper = _run(paper, PRICE_PATH_WINNER)
    ev_fut   = _run(fut,   PRICE_PATH_WINNER)

    assert ev_paper == ev_fut, f"paper={ev_paper} futures={ev_fut}"
    assert ev_paper and ev_paper[0][1] == EXIT_TRAIL_STOP
    assert _partial_events(paper) == _partial_events(fut)


def test_paper_and_futures_identical_on_loser():
    paper = PaperPortfolio(1000.0)
    paper.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                        atr_pct=0.01, qty=1.5, risk_amount=3.0)
    fut = _make_futures_trader()
    fut.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                      atr_pct=0.01, qty=1.5, risk_amount=3.0)

    ev_paper = _run(paper, PRICE_PATH_LOSER)
    ev_fut   = _run(fut,   PRICE_PATH_LOSER)
    assert ev_paper == ev_fut
    assert ev_paper[0][1] == EXIT_INITIAL_STOP


def test_both_traders_use_the_same_exit_engine():
    """Yapısal garanti: iki sınıf da ortak mixin'den türemeli."""
    assert issubclass(PaperPortfolio, ExitEngineMixin)
    assert issubclass(FuturesTrader, ExitEngineMixin)
    assert PaperPortfolio.check_positions is ExitEngineMixin.check_positions
    assert FuturesTrader.check_positions is ExitEngineMixin.check_positions


# ── Gerçek modda sabit TP emri olmamalı ───────────────────────────────────────

def test_futures_does_not_place_fixed_take_profit_order():
    """
    Sabit TAKE_PROFIT_MARKET emri runner'ı öldürür — konmamalı.
    Yalnızca koruyucu STOP_MARKET konmalı.
    """
    fut = _make_futures_trader()
    fut.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                      atr_pct=0.01, qty=1.5, risk_amount=3.0)

    order_types = [
        c.kwargs.get("type") for c in fut.client.futures_create_order.call_args_list
    ]
    assert "TAKE_PROFIT_MARKET" not in order_types, \
        "sabit TP emri konmuş — gerçek modda runner çalışmaz"
    assert "STOP_MARKET" in order_types, "koruyucu stop emri konmamış"


def test_futures_moves_protective_stop_as_bot_trails():
    """Bot stop'u taşıdığında borsadaki koruyucu emir de taşınmalı."""
    fut = _make_futures_trader()
    fut.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                      atr_pct=0.01, qty=1.5, risk_amount=3.0)
    fut.client.futures_cancel_order.reset_mock()

    for px in [100.0, 102.0, 105.0, 108.0]:
        fut.check_positions({"TESTUSDT": px})

    assert fut.client.futures_cancel_order.called, \
        "stop taşındı ama borsadaki emir güncellenmedi"


def test_futures_partial_exit_uses_reduce_only():
    fut = _make_futures_trader()
    fut.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                      atr_pct=0.01, qty=1.5, risk_amount=3.0)
    fut.check_positions({"TESTUSDT": 102.0})    # +1R → TP1

    reduce_calls = [
        c for c in fut.client.futures_create_order.call_args_list
        if c.kwargs.get("reduceOnly")
    ]
    assert reduce_calls, "kısmi çıkış reduceOnly emri göndermedi"


# ── Muhasebe ──────────────────────────────────────────────────────────────────

def test_partial_exit_accounting_is_consistent():
    """Kısmi çıkışlar sonrası bakiye + realize edilen kâr tutarlı olmalı."""
    p = PaperPortfolio(1000.0)
    start = p.balance
    p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    atr_pct=0.01, qty=1.5, risk_amount=3.0)
    for px in PRICE_PATH_WINNER:
        p.check_positions({"TESTUSDT": px})
        if not p.positions:
            break
    assert not p.positions
    # Kazançlı senaryo: bakiye başlangıcın üstünde olmalı
    assert p.balance > start
    # DENETİM: bu invariant `or True` ile ölüydü ve ifadesi de kendini
    # sıfırlıyordu (`- comm + comm`). Gerçek invariant: bakiye değişimi,
    # biriken net PnL'e eşit olmalı — komisyon zaten net'in içinde.
    assert p.balance == pytest.approx(start + p.total_pnl, rel=1e-9)
    assert p.wins == 1 and p.losses == 0


def test_commission_charged_on_every_leg():
    """
    Komisyon her bacakta alınmalı: açılış + TP1 + TP2 + kapanış.

    Kısmi çıkışların komisyonu kapatılan miktarla ORANTILIDIR (tam notional
    üzerinden değil), bu yüzden toplam ≈ 2× açılış komisyonu civarındadır:
    açılış tüm notional'ı, üç çıkış bacağı birlikte yine tüm notional'ı kapsar.
    """
    p = PaperPortfolio(1000.0)
    qty = 1.5
    p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    atr_pct=0.01, qty=qty, risk_amount=3.0)
    open_comm = p.total_commission
    entry = p.positions["TESTUSDT"]["entry_price"]
    assert entry == pytest.approx(100.0 * (1 + config.SLIPPAGE_PCT))
    assert open_comm == pytest.approx(qty * entry * config.COMMISSION_RATE)

    for px in PRICE_PATH_WINNER:
        p.check_positions({"TESTUSDT": px})
        if not p.positions:
            break

    # Çıkış bacakları giriş fiyatının üstünde gerçekleştiği için toplam
    # komisyon, iki katı açılış komisyonunu aşmalı.
    assert p.total_commission > open_comm * 2

    # DB kaydındaki komisyon TÜM bacakları içermeli
    from database import get_recent_trades
    rec = get_recent_trades(1)[0]
    assert rec["commission"] == pytest.approx(p.total_commission, rel=1e-6)
    assert rec["partial_pnl"] > 0, "kısmi çıkış kârı kaydedilmedi"


def test_duplicate_position_prevented():
    p = PaperPortfolio(1000.0)
    ok1, _ = p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0, qty=1.0)
    ok2, msg = p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0, qty=1.0)
    assert ok1 and not ok2 and "Zaten açık" in msg
    assert len(p.positions) == 1


def test_insufficient_balance_rejected():
    p = PaperPortfolio(10.0)
    ok, msg = p.open_position("TESTUSDT", "BUY", 100.0, 500.0, 3, 98.0, 107.0, qty=15.0)
    assert not ok and "Yetersiz" in msg


def test_stale_symbol_price_is_skipped_not_guessed():
    """
    Fiyatı olmayan sembol için çıkış değerlendirmesi YAPILMAMALI.
    Bayat/eksik fiyatla stop kontrolü yanlış kapanışa yol açar.
    """
    p = PaperPortfolio(1000.0)
    p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    atr_pct=0.01, qty=1.5, risk_amount=3.0)
    closed = p.check_positions({})            # fiyat yok
    assert closed == []
    assert "TESTUSDT" in p.positions
    closed = p.check_positions({"BASKAUSDT": 50.0})   # alakasız sembol
    assert closed == []
    assert "TESTUSDT" in p.positions


def test_strategy_attribution_flows_into_position():
    p = PaperPortfolio(1000.0)
    p.open_position("TESTUSDT", "BUY", 100.0, 50.0, 3, 98.0, 107.0,
                    qty=1.0, strategy="MOMENTUM_FAST")
    assert p.positions["TESTUSDT"]["strategy"] == "MOMENTUM_FAST"
