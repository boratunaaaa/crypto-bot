"""
Haftalık gerçekleşmiş PnL ölçümü.

2026-08-16 canlı arızası: kill switch bellekteki `weekly_pnl` sayacına bakıyordu.
12 Ağustos deploy'unda state şeması değişince sayaç 0'a düştü, haftanın
+284.88$'lık kârı silindi ve bot GERÇEKTE KÂRDA olduğu bir haftada 2,5 gün
boyunca tek işlem açmadı (4.061 sinyal kaçtı).

Buradaki testler ölçümün penceresini ve mod ayrımını sabitler — sayacın yerini
alan şey yanlışsa, düzeltme düzeltmez.
"""

from datetime import datetime, timedelta

import pytest

from database import get_week_realized_pnl, save_trade


def _closed(net: float, close_time: datetime, symbol: str = "AAAUSDT"):
    save_trade(
        symbol=symbol, side="BUY", entry_price=100, close_price=101,
        margin=50, leverage=3, gross_pnl=net + 0.3, commission=0.3, net_pnl=net,
        reason_open="test", reason_close="TRAIL_STOP",
        open_time=close_time - timedelta(minutes=30), close_time=close_time,
        strategy="TREND", r_multiple=0.0,
    )


def _this_monday(ref: datetime) -> datetime:
    y, w, _ = ref.isocalendar()
    return datetime.fromisocalendar(y, w, 1)


def test_only_current_iso_week_is_counted():
    """Asıl hata buydu: pencere yanlışsa tüm geçmiş 'bu hafta' sayılır."""
    now = datetime.now()
    monday = _this_monday(now)

    _closed(+40.0, monday + timedelta(hours=2), "INUSDT")     # bu hafta
    _closed(-15.0, now - timedelta(minutes=5), "IN2USDT")     # bu hafta
    _closed(-500.0, monday - timedelta(days=3), "OLDUSDT")    # GEÇEN hafta

    assert get_week_realized_pnl() == pytest.approx(25.0)


def test_previous_week_loss_cannot_halt_this_week():
    """Geçen haftanın zararı bu haftanın limitine sızmamalı."""
    now = datetime.now()
    monday = _this_monday(now)
    _closed(-9999.0, monday - timedelta(hours=1), "LASTWEEK")
    assert get_week_realized_pnl() == pytest.approx(0.0)


def test_next_week_trade_is_excluded():
    now = datetime.now()
    monday = _this_monday(now)
    _closed(+123.0, monday + timedelta(days=8), "NEXTWEEK")
    assert get_week_realized_pnl() == pytest.approx(0.0)


def test_rows_without_close_time_are_not_counted():
    """`close_time` NULL olan satır gerçekleşmiş PnL değildir.

    Doğrudan INSERT ediliyor: `save_trade` süre hesapladığı için NULL kapanış
    yazamaz, ama eski/yarım kalmış satırlar veritabanında bulunabilir.
    """
    import sqlite3
    import database

    now = datetime.now()
    _closed(+7.0, now, "CLOSEDUSDT")
    with sqlite3.connect(str(database.DB_PATH)) as con:
        con.execute(
            "INSERT INTO trades (symbol, side, net_pnl, open_time, close_time) "
            "VALUES ('OPENUSDT', 'BUY', 999.0, ?, NULL)", (now.isoformat(),)
        )
    assert get_week_realized_pnl() == pytest.approx(7.0)


def test_empty_week_is_zero_not_none():
    """Boş hafta 0 döner; None 'ölçemedim' demektir ve sayaca düşülür."""
    v = get_week_realized_pnl()
    assert v == pytest.approx(0.0) and v is not None


# ── Bakiye sıfırlaması ────────────────────────────────────────────────────────
# 2026-08-18: bakiyeyi 1000$'a sıfırlamak TEK BAŞINA sessiz bir kilit üretiyordu.
# Haftalık zarar limiti %10; o hafta ölçülen PnL -103.90$'dı. Yeni hafta başı
# bakiyesi 1000$ olunca eşik -100$ oluyor ve bot AÇILIR AÇILMAZ kendini
# durduruyordu — yani "temiz başlangıç", hiç işlem açmayan bir başlangıç olurdu.
# Çözüm geçmişi silmek değil (veri veridir), ölçümün başlangıcını kaydetmek.

def test_sifirlamadan_onceki_islemler_yeni_doneme_yazilmaz():
    from database import state_set_many

    now = datetime.now()
    monday = _this_monday(now)
    sifirlama = monday + timedelta(hours=3)

    _closed(-103.90, monday + timedelta(hours=1), "ONCEKI")   # sıfırlamadan ÖNCE
    _closed(+7.50,   monday + timedelta(hours=5), "SONRAKI")  # sıfırlamadan SONRA

    assert get_week_realized_pnl() == pytest.approx(-96.40, abs=0.01), (
        "işaret yokken haftanın tamamı sayılmalı"
    )

    state_set_many({"balance_reset_at": sifirlama.isoformat()})
    assert get_week_realized_pnl() == pytest.approx(7.50, abs=0.01), (
        "sıfırlamadan önceki zarar yeni dönemin risk bütçesine yazıldı — "
        "bot temiz başlangıçta kendini durdurur"
    )


def test_sifirlama_isareti_hafta_basindan_eskiyse_yok_sayilir():
    """Geçen haftaki bir sıfırlama, bu haftanın ölçümünü genişletmemeli."""
    from database import state_set_many

    now = datetime.now()
    monday = _this_monday(now)
    state_set_many({"balance_reset_at": (monday - timedelta(days=3)).isoformat()})
    _closed(-12.0, monday + timedelta(hours=2), "BUHAFTA")
    assert get_week_realized_pnl() == pytest.approx(-12.0, abs=0.01)
