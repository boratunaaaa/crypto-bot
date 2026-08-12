"""
Çıkış motoru testleri — kısmi çıkış, breakeven, runner trailing, time-stop.

Bu testlerin varlık sebebi: eski kodda çıkış davranışı hiç test edilmemişti ve
82 işlemin 82'si tek bir "STOP_LOSS" etiketiyle kaydedilmişti; yani kârla trail
olan ile zararla stop olan ayırt edilemiyordu.
"""

from datetime import datetime, timedelta

import pytest

import config
from position import (
    new_position, evaluate,
    EXIT_INITIAL_STOP, EXIT_BREAKEVEN_STOP, EXIT_TRAIL_STOP,
    EXIT_TIME_STOP, EXIT_TP1, EXIT_TP2,
    STOP_KIND_INITIAL, STOP_KIND_BREAKEVEN, STOP_KIND_TRAIL,
)


def make_pos(side="BUY", entry=100.0, stop=98.0, qty=10.0, atr_pct=0.01):
    """1R = 2.0 fiyat birimi olan standart test pozisyonu."""
    return new_position(
        symbol="TESTUSDT", side=side, entry_price=entry, qty=qty,
        margin=100.0, leverage=3, stop_loss=stop, take_profit=entry + 7,
        atr_pct=atr_pct, strategy="TREND",
    )


def drive(pos, prices, start=None):
    """
    Fiyat serisini motora sırayla ver, aksiyonları topla.

    Canlı kodda (`ExitEngineMixin.check_positions`) CLOSE gelince pozisyon
    sözlükten silinir ve bir daha değerlendirilmez; bu yardımcı da aynı şekilde
    ilk CLOSE'da durur.
    """
    now = start or datetime.now()
    acts = []
    for i, px in enumerate(prices):
        new_acts = evaluate(pos, px, now=now + timedelta(seconds=30 * i))
        acts.extend(new_acts)
        if any(a["type"] == "CLOSE" for a in new_acts):
            break
    return acts


# ── İlk stop ──────────────────────────────────────────────────────────────────

def test_initial_stop_hit_is_labelled_as_real_loss():
    pos = make_pos()
    acts = drive(pos, [100.0, 99.0, 97.9])
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["reason"] == EXIT_INITIAL_STOP


def test_short_initial_stop():
    pos = make_pos(side="SELL", entry=100.0, stop=102.0)
    acts = drive(pos, [100.0, 101.0, 102.1])
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert closes and closes[0]["reason"] == EXIT_INITIAL_STOP


# ── Kısmi kâr alma ────────────────────────────────────────────────────────────

def test_tp1_takes_partial_and_moves_stop_to_breakeven():
    pos = make_pos()               # entry 100, stop 98 → 1R = 2.0
    acts = drive(pos, [100.0, 101.0, 102.0])   # +1R
    partials = [a for a in acts if a["type"] == "PARTIAL"]
    assert len(partials) == 1
    assert partials[0]["reason"] == EXIT_TP1
    assert partials[0]["fraction"] == config.PARTIAL_TP1_PCT
    assert pos["tp1_done"] is True
    # Stop artık girişin ÜSTÜNDE olmalı (komisyon dahil breakeven)
    assert pos["stop_loss"] > pos["entry_price"]
    assert pos["stop_kind"] == STOP_KIND_BREAKEVEN


def test_breakeven_stop_covers_round_trip_commission():
    """
    "Kâr etmesek bile zarar etmeyelim" garantisi.

    Girişe tam eşit stop aslında zarardır: açılış+kapanış komisyonu cepten
    çıkar. Breakeven stop bunu aşmalı.
    """
    pos = make_pos()
    drive(pos, [100.0, 102.0])
    round_trip = config.COMMISSION_RATE * 2
    assert pos["stop_loss"] >= pos["entry_price"] * (1 + round_trip)


def test_tp2_takes_second_partial_and_activates_runner():
    pos = make_pos()
    acts = drive(pos, [100.0, 102.0, 104.0])   # +1R sonra +2R
    reasons = [a["reason"] for a in acts if a["type"] == "PARTIAL"]
    assert EXIT_TP1 in reasons and EXIT_TP2 in reasons
    assert pos["tp2_done"] is True
    assert pos["trailing_active"] is True


def test_partial_fractions_never_exceed_position():
    pos = make_pos(qty=10.0)
    drive(pos, [100.0, 102.0, 104.0])
    total_frac = config.PARTIAL_TP1_PCT + config.PARTIAL_TP2_PCT
    assert total_frac < 1.0, "kısmi çıkışların toplamı pozisyonun tamamı olamaz"


# ── Runner: ÜST LİMİT YOK ─────────────────────────────────────────────────────

def test_runner_has_no_maximum_take_profit():
    """
    Kullanıcının en net isteği: "%20, %50 veya daha fazla yükselirse sistem
    teorik olarak pozisyonda kalabilmeli."

    Fiyat girişin 5 katına çıkarken hiçbir noktada tam kapanış OLMAMALI.
    """
    pos = make_pos(entry=100.0, stop=98.0, atr_pct=0.01)
    prices = [100.0] + [100.0 + i for i in range(1, 400)]   # 100 → 499
    acts = drive(pos, prices)
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert closes == [], "runner sabit bir TP'de kapandı — üst limit olmamalı"
    assert pos["qty"] > 0
    assert pos["mfe_r"] > 100   # 1R=2 birim → ~200R


def test_runner_trails_up_and_exits_on_reversal_with_profit():
    pos = make_pos(entry=100.0, stop=98.0, atr_pct=0.01)
    # Yukarı koş, sonra sert dön
    up = [100.0 + i * 0.5 for i in range(0, 60)]     # 100 → 129.5
    down = [129.0, 125.0, 120.0, 115.0, 110.0, 105.0]
    acts = drive(pos, up + down)
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["reason"] == EXIT_TRAIL_STOP
    # Trailing kârla çıkmalı — giriş fiyatının üstünde
    assert closes[0]["price"] > pos["entry_price"]


def test_trailing_never_widens_risk():
    """Trailing stop hiçbir koşulda geriye (aleyhte) gitmemeli."""
    pos = make_pos(entry=100.0, stop=98.0)
    stops = []
    now = datetime.now()
    for i, px in enumerate([100, 102, 105, 108, 106, 104, 109, 107]):
        evaluate(pos, float(px), now=now + timedelta(seconds=30 * i))
        stops.append(pos["stop_loss"])
    assert stops == sorted(stops), f"stop geriye gitti: {stops}"


def test_short_runner_trails_down():
    pos = make_pos(side="SELL", entry=100.0, stop=102.0, atr_pct=0.01)
    down = [100.0 - i * 0.5 for i in range(0, 60)]
    up   = [71.0, 75.0, 80.0, 85.0, 90.0]
    acts = drive(pos, down + up)
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert closes and closes[0]["reason"] == EXIT_TRAIL_STOP
    assert closes[0]["price"] < pos["entry_price"]


# ── Breakeven stop'a dönüş ────────────────────────────────────────────────────

def test_breakeven_stop_exit_is_labelled_separately():
    """TP1 alındıktan sonra geri dönen pozisyon 'zarar' olarak etiketlenmemeli."""
    pos = make_pos(entry=100.0, stop=98.0)
    acts = drive(pos, [100.0, 102.0, 101.0, 100.05])
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["reason"] == EXIT_BREAKEVEN_STOP


# ── Time stop ─────────────────────────────────────────────────────────────────

def test_time_stop_closes_stagnant_position():
    pos = make_pos()
    now = datetime.now()
    evaluate(pos, 100.2, now=now)
    late = now + timedelta(minutes=config.TIME_STOP_MINUTES + 1)
    acts = evaluate(pos, 100.3, now=late)
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert closes and closes[0]["reason"] == EXIT_TIME_STOP


def test_time_stop_does_not_kill_a_running_winner():
    """
    Time-stop, hareket eden pozisyonu ÖLDÜRMEMELİ — aksi hâlde tam olarak
    yakalamak istediğimiz büyük hareketleri keser.
    """
    pos = make_pos(entry=100.0, stop=98.0, atr_pct=0.01)
    now = datetime.now()
    # Önce TP1'i geç (runner'a dönüşsün)
    for i, px in enumerate([100.0, 101.0, 102.0, 103.0]):
        evaluate(pos, px, now=now + timedelta(seconds=30 * i))
    assert pos["tp1_done"]
    # Çok sonra, hâlâ kârda
    late = now + timedelta(minutes=config.TIME_STOP_MAX_MINUTES + 60)
    acts = evaluate(pos, 104.0, now=late)
    closes = [a for a in acts if a["type"] == "CLOSE"]
    assert closes == [], "runner time-stop ile kapatıldı — büyük hareket kaçar"


# ── Sıralama / look-ahead ─────────────────────────────────────────────────────

def test_stop_checked_before_trailing_update():
    """
    Aynı tick'te stop hem taşınıp hem 'değmedi' sayılamaz — bu, backtest'i
    sistematik olarak iyimser gösteren klasik look-ahead hatasıdır.
    """
    pos = make_pos(entry=100.0, stop=98.0)
    drive(pos, [100.0, 102.0])          # breakeven'e taşındı
    be = pos["stop_loss"]
    acts = evaluate(pos, be - 0.01)     # stop'un hemen altı
    assert any(a["type"] == "CLOSE" for a in acts)


def test_zero_qty_position_produces_no_actions():
    pos = make_pos()
    pos["qty"] = 0.0
    assert evaluate(pos, 150.0) == []
