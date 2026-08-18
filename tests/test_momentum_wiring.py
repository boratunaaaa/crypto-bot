"""
MOMENTUM'un kendi portföyüne bağlanması — izolasyon sözleşmesi.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
MOMENTUM 2026-08-18'de gölge moddan çıkarıldı ve kendi sanal sermayesine
bağlandı. Buradaki risk kodun patlaması değil, SIZINTIdır: iki defter
birbirine karışırsa ölçtüğümüz şey "hangi strateji iyi" olmaktan çıkar,
"hangisi önce davrandı" olur — ve deneyin tamamı boşa gider.

Sızıntının sessiz olduğuna dikkat: karışan portföyler hata vermez, sadece
yanlış sayı üretir. O yüzden testler davranışı değil, AYRIŞMAYI ölçüyor.

Ayrıca `main.py` bir betik gibi kurulu (ağır yan etkiler, `run()` içinde
kapanışlar) ve doğrudan import edilip çalıştırılamıyor; bu yüzden bağlantı
kuralları kaynak üzerinden (AST/metin) sabitleniyor. Zayıf bir yöntem, ama
"hiç test yok"tan iyi ve tam olarak bu sınıf regresyonu yakalıyor.
"""

import pathlib
import re

import pytest

import config
from portfolios import StrategyBook, state_key
from trader import PaperPortfolio

_MAIN = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")


def _govde(imza: str) -> str:
    """Bir fonksiyonun tam gövdesi — girintiye göre kesilir.

    İlk hâli boş satıra göre kesiyordu ve docstring'in ortasında kalıyordu;
    test kodu doğru olan koda hata veriyordu.
    """
    yer = _MAIN.index(imza)
    # SATIR BAŞINDAN al: `index` imzanın ilk harfine konumlanır ve baştaki
    # boşluklar dışarıda kalır; o zaman girinti 0 hesaplanır ve gövde hiç
    # bitmez (bir keresinde 1054 satır yuttu, test anlamsızlaştı).
    satir_basi = _MAIN.rfind("\n", 0, yer) + 1
    satirlar = _MAIN[satir_basi:].split("\n")
    girinti = len(satirlar[0]) - len(satirlar[0].lstrip())
    out = [satirlar[0]]
    for satir in satirlar[1:]:
        if satir.strip() and (len(satir) - len(satir.lstrip())) <= girinti:
            break
        out.append(satir)
    return "\n".join(out)


# ── Yapılandırma sözleşmesi ───────────────────────────────────────────────────

def test_momentum_kendi_sermayesine_sahip():
    assert config.MOMENTUM_BALANCE == config.PAPER_BALANCE == 1000.0


def test_momentum_artik_golgede_degil():
    assert config.MOMENTUM_SHADOW_MODE is False


# ── Defter ayrımı ─────────────────────────────────────────────────────────────

def test_iki_defterin_state_anahtarlari_CAKISMAZ():
    """Çakışsalardı biri diğerinin bakiyesini ezerdi — sessiz sermaye kaybı."""
    for alan in ("paper_balance", "total_pnl", "wins", "losses"):
        assert state_key("TREND", alan) != state_key("MOMENTUM_FAST", alan)


def test_TREND_anahtarlari_DEGISMEDI():
    """Canlı portföyün anahtarı değişirse bakiye sıfırdan başlar."""
    assert state_key("TREND", "paper_balance") == "paper_balance"


def test_defterler_bagimsiz_sermaye_tutar():
    trend = StrategyBook("TREND", PaperPortfolio(1000.0))
    mom = StrategyBook("MOMENTUM_FAST", PaperPortfolio(1000.0))
    mom.portfolio.balance -= 600.0
    assert trend.portfolio.balance == 1000.0, "MOMENTUM zararı TREND'e sızdı"


# ── main.py bağlantı kuralları ────────────────────────────────────────────────

def test_giris_KENDI_defterine_acar():
    """`trader.open_position` kalsaydı MOMENTUM, TREND'in kasasından harcardı."""
    assert "ok, msg = pf.open_position(" in _MAIN, (
        "giriş hâlâ tek portföye bağlı — MOMENTUM TREND'in parasını harcar"
    )
    assert "ok, msg = trader.open_position(" not in _MAIN


def test_pozisyon_limiti_KENDI_defterine_gore():
    """Limit ortak sayılsaydı MOMENTUM, TREND doluyken hiç işlem açamazdı;
    o zaman ölçtüğümüz şey stratejinin kendisi olmazdı."""
    assert 'positions = getattr(pf, "positions", {})' in _MAIN


def test_risk_tahsisi_anahtari_strateji_tasir():
    """İki defter aynı sembolü tutabilir. Tek anahtar kullanılsaydı biri
    diğerinin tahsisini ezer, risk bütçesi sessizce yanlış iade edilirdi."""
    assert '_alloc_of[f"{strategy}:{sig.symbol}"]' in _MAIN
    # Ön eksiz kalan bir kullanım olmamalı.
    kalan = re.findall(r"_alloc_of\.pop\((?!f\")", _MAIN)
    assert not kalan, f"strateji öneki olmayan _alloc_of kullanımı: {kalan}"


def test_her_defter_AYRI_yonetiliyor():
    """Tek `check_positions` çağrısı kalsaydı MOMENTUM pozisyonları hiç
    yönetilmez, stopları hiç kontrol edilmezdi."""
    assert "for ad, pf in defterler:" in _MAIN
    assert "defterler.append((\"MOMENTUM_FAST\"" in _MAIN


def test_bir_defterin_hatasi_digerini_yonetimsiz_birakmaz():
    """`return` yerine `continue`: ilk defter patlarsa ikincisi yine yönetilir."""
    # Pencere içinde "continue" aramak YETMİYOR: aynı döngüde başka bir
    # `continue` daha var (boş defteri atlayan) ve mutasyon testi geçiyordu.
    # Hedef, TAM OLARAK check_positions hata işleyicisinin son satırı.
    isaret = 'logger.exception(f"{ad}: check_positions başarısız")'
    assert isaret in _MAIN, "hata işleyicisi bulunamadı"
    sonrasi = _MAIN[_MAIN.index(isaret) + len(isaret):].lstrip("\n")
    ilk_satir = sonrasi.split("\n")[0].strip()
    assert ilk_satir == "continue", (
        f"hata işleyicisi `{ilk_satir}` ile bitiyor; `continue` olmalı — "
        "`return` olsaydı ilk defterin hatası İKİNCİ defteri de yönetimsiz "
        "bırakır, MOMENTUM pozisyonlarının stopları hiç kontrol edilmezdi"
    )


def test_fiyat_bir_kez_cekiliyor():
    """Portföyler bağımsız, piyasa verisi ORTAK. Aynı sembolü iki strateji
    tutuyorsa iki ayrı REST çağrısı yapmak gereksiz yük ve rate-limit riski."""
    assert "semboller = {" in _MAIN
    assert "px = mdm.get_prices_for(sorted(semboller)" in _MAIN


def test_kaydetme_TREND_satirlarini_strateji_adiyla_yazar():
    assert 'save_open_positions(trader.positions, strategy="TREND")' in _MAIN


def test_momentum_defteri_de_kaydediliyor():
    assert "mom_book.save()" in _MAIN


def test_gercek_modda_coklu_portfoy_YOK():
    """Deney kâğıt üzerinde yürüyor; gerçek para yolunu bu refactor'a maruz
    bırakmak gereksiz risk."""
    bolum = _govde("def book_of(")
    assert "PAPER_TRADING" in bolum, (
        "book_of gerçek modda da ikinci portföye yönlendiriyor olabilir"
    )


def test_bilinmeyen_strateji_TREND_defterine_duser():
    """Yeni bir strateji eklenip defteri unutulursa sessizce kaybolmasın."""
    bolum = _govde("def book_of(")
    assert bolum.rstrip().endswith("return trader"), (
        "book_of'un son satırı `return trader` değil — tanımsız strateji "
        "sessizce kaybolabilir"
    )
