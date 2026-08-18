"""
Strateji başına portföy izolasyonu.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
Deneyin geçerliliği izolasyona bağlı. Portföyler sızdırırsa ölçtüğümüz şey
"A stratejisi iyi mi" değil, "A, B'nin sermayesini ne kadar tüketti" olur.

Ve burada SESSİZ bir veri kaybı tuzağı vardı: `open_positions` tablosunun
birincil anahtarı yalnız `symbol`di, kaydetme işlemi de tabloyu KOMPLE
siliyordu. Tek strateji varken zararsızdı. İki strateji çalıştığında:
  * aynı sembolü tutan iki portföyden biri diğerini sessizce EZERDİ,
  * bir stratejinin kaydı DİĞERLERİNİN pozisyonlarını SİLERDİ.
Pozisyon diskten yok olur, restart'ta yönetilmez ve hiçbir gösterge bunu
göstermez. Testlerin çoğu tam olarak bu iki senaryoyu kovalıyor.
"""

import pytest

from database import load_open_positions, save_open_positions, state_get
from portfolios import PortfolioSet, StrategyBook, state_key
from trader import PaperPortfolio


def _poz(symbol, entry=100.0, margin=50.0):
    return {
        "symbol": symbol, "side": "BUY", "entry_price": entry, "qty": 1.0,
        "initial_qty": 1.0, "margin": margin, "initial_margin": margin,
        "leverage": 3, "stop_loss": entry * 0.98, "initial_stop": entry * 0.98,
        "risk_per_unit": entry * 0.02, "risk_amount": entry * 0.02,
        "stop_kind": "INITIAL", "peak": entry, "trough": entry,
        "mfe_r": 0.0, "mae_r": 0.0, "tp1_done": False, "tp2_done": False,
        "realized_pnl": 0.0, "realized_commission": 0.0, "trailing_active": False,
    }


# ── Sessiz veri kaybı: asıl tuzak ─────────────────────────────────────────────

def test_bir_stratejinin_kaydi_DIGERININ_pozisyonlarini_silmez():
    """Eski `DELETE FROM open_positions` tüm stratejileri siliyordu."""
    save_open_positions({"AAAUSDT": _poz("AAAUSDT")}, strategy="A")
    save_open_positions({"BBBUSDT": _poz("BBBUSDT")}, strategy="B")

    # A yeniden kaydediyor — B'ye dokunmamalı.
    save_open_positions({"CCCUSDT": _poz("CCCUSDT")}, strategy="A")

    assert set(load_open_positions("A")) == {"CCCUSDT"}
    assert set(load_open_positions("B")) == {"BBBUSDT"}, (
        "A'nın kaydı B'nin pozisyonunu sildi — sessiz veri kaybı"
    )


def test_ayni_sembol_iki_stratejide_birlikte_yasar():
    """Eski birincil anahtar (yalnız symbol) birini ezerdi."""
    save_open_positions({"BTCUSDT": _poz("BTCUSDT", entry=100.0)}, strategy="A")
    save_open_positions({"BTCUSDT": _poz("BTCUSDT", entry=200.0)}, strategy="B")

    a = load_open_positions("A")
    b = load_open_positions("B")
    assert a["BTCUSDT"]["entry_price"] == 100.0
    assert b["BTCUSDT"]["entry_price"] == 200.0, (
        "aynı sembolü tutan ikinci strateji birinciyi ezdi"
    )


def test_bos_kayit_kendi_stratejisini_temizler_digerine_dokunmaz():
    save_open_positions({"AAAUSDT": _poz("AAAUSDT")}, strategy="A")
    save_open_positions({"BBBUSDT": _poz("BBBUSDT")}, strategy="B")
    save_open_positions({}, strategy="A")
    assert load_open_positions("A") == {}
    assert set(load_open_positions("B")) == {"BBBUSDT"}


def test_varsayilan_strateji_TREND_geriye_donuk_calisir():
    """Mevcut bot argümansız çağırıyor; davranışı değişmemeli."""
    save_open_positions({"XXXUSDT": _poz("XXXUSDT")})
    assert set(load_open_positions()) == {"XXXUSDT"}
    assert set(load_open_positions("TREND")) == {"XXXUSDT"}


# ── State anahtarları ─────────────────────────────────────────────────────────

def test_TREND_eski_duz_anahtarlari_kullanmaya_devam_eder():
    """Mevcut botun bakiyesi taşınmamalı.

    Taşıma sırasında bir hata olsa bakiye sessizce sıfırlanır; canlı bir
    portföyü yeni bir anahtara taşımanın riski, kazancından büyük.
    """
    assert state_key("TREND", "paper_balance") == "paper_balance"
    assert state_key("TREND", "wins") == "wins"


def test_diger_stratejiler_kendi_ad_alanini_kullanir():
    assert state_key("BREAKOUT", "paper_balance") == "BREAKOUT:paper_balance"
    assert state_key("A", "wins") != state_key("B", "wins")


# ── Portföy izolasyonu ────────────────────────────────────────────────────────

def test_bir_stratejinin_zarari_digerinin_sermayesine_dokunmaz():
    ps = PortfolioSet({"A": 1000.0, "B": 1000.0})
    ps["A"].portfolio.balance -= 400.0
    assert ps["B"].portfolio.balance == 1000.0, "sermaye sızdı"
    assert ps["A"].portfolio.balance == 600.0


def test_pozisyonlar_ayrisir():
    ps = PortfolioSet({"A": 1000.0, "B": 1000.0})
    ps["A"].portfolio.positions["AAAUSDT"] = _poz("AAAUSDT")
    assert "AAAUSDT" not in ps["B"].portfolio.positions
    assert ps.held_symbols("A") == ["AAAUSDT"]
    assert ps.held_symbols("B") == []


def test_equity_kilitli_marji_da_sayar():
    """Yalnız nakde bakmak, pozisyonu olan stratejiyi fakir gösterir."""
    ps = PortfolioSet({"A": 1000.0})
    ps["A"].portfolio.balance = 700.0
    ps["A"].portfolio.positions["AAAUSDT"] = _poz("AAAUSDT", margin=300.0)
    assert ps["A"].equity == pytest.approx(1000.0)


def test_ortak_fiyat_kumesi_tekrarlanan_sembolu_bir_kez_sayar():
    """Portföyler bağımsız, piyasa verisi ortak."""
    ps = PortfolioSet({"A": 1000.0, "B": 1000.0})
    ps["A"].portfolio.positions["BTCUSDT"] = _poz("BTCUSDT")
    ps["B"].portfolio.positions["BTCUSDT"] = _poz("BTCUSDT")
    ps["B"].portfolio.positions["ETHUSDT"] = _poz("ETHUSDT")
    assert ps.all_held_symbols() == {"BTCUSDT", "ETHUSDT"}


def test_kaydet_yukle_turu_bagimsiz():
    ps = PortfolioSet({"A": 1000.0, "B": 500.0})
    ps["A"].portfolio.balance = 812.5
    ps["A"].portfolio.wins = 3
    ps["A"].portfolio.positions["AAAUSDT"] = _poz("AAAUSDT")
    ps["B"].portfolio.balance = 490.0
    ps["B"].portfolio.losses = 2
    ps.save_all()

    yeni = PortfolioSet({"A": 1.0, "B": 1.0})
    yeni.restore_all()
    assert yeni["A"].portfolio.balance == pytest.approx(812.5)
    assert yeni["A"].portfolio.wins == 3
    assert set(yeni["A"].portfolio.positions) == {"AAAUSDT"}
    assert yeni["B"].portfolio.balance == pytest.approx(490.0)
    assert yeni["B"].portfolio.losses == 2
    assert yeni["B"].portfolio.positions == {}


def test_bir_portfoyun_kaydi_patlarsa_digerleri_YINE_kaydedilir():
    """Tek bozuk strateji, toplu veri kaybına yol açmamalı."""
    ps = PortfolioSet({"A": 1000.0, "B": 1000.0})
    ps["B"].portfolio.balance = 777.0

    class _Patlayan(StrategyBook):
        def save(self):
            raise RuntimeError("bozuk")

    ps.books["A"] = _Patlayan("A", ps["A"].portfolio)
    ps.save_all()   # patlamamalı

    yeni = PortfolioSet({"B": 1.0})
    yeni.restore_all()
    assert yeni["B"].portfolio.balance == pytest.approx(777.0), (
        "bir portföyün hatası diğerinin kaydını engelledi"
    )


def test_hic_calismamis_strateji_baslangic_sermayesini_korur():
    ps = PortfolioSet({"YENISTRATEJI": 1000.0})
    varmi = ps.restore_all()
    assert varmi["YENISTRATEJI"] is False
    assert ps["YENISTRATEJI"].portfolio.balance == 1000.0


def test_bos_strateji_kumesi_reddedilir():
    with pytest.raises(ValueError):
        PortfolioSet({})
