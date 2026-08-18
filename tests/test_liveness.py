"""
BOT ÖLMESİN — dayanıklılık testleri.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
Kullanıcı şartı: "botlar kesinlikle ölmesin, mutlaka sonuçları öğrenelim."

Denetimde bulundu (2026-08-18): ana işlem döngüsünün gövdesinde HİÇBİR genel
istisna koruması yoktu. Tek bir beklenmedik hata — bozuk bir API cevabı, bir
göstergede KeyError, geçici ağ kesintisi — `run()`'dan kaçıyor ve süreci
öldürüyordu. systemd 15 saniye sonra yeniden başlatıyor, üstüne ~35 saniyelik
piyasa verisi ısınması biniyor: açık pozisyonlar yaklaşık BİR DAKİKA boyunca
stopsuz kalıyordu.

Ölüm sessizdi de: dışarıdan "servis yeniden başladı" görünüyordu, "pozisyonlar
korumasız kaldı" görünmüyordu.

Buradaki testler eşiği değil DAVRANIŞI sabitler:
  1. Döngü hata yiyince ölmez, döner.
  2. Pahalı taramanın hatası, ucuz ve hayati stop kontrolünü durdurmaz.
  3. Hata sessiz kalmaz.
  4. Hata sıcak döngüye dönüşmez ama bekleme de sonsuza uzamaz.
"""

import ast
import pathlib

import pytest

_MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"
_KAYNAK = _MAIN.read_text(encoding="utf-8")


def _run_fonksiyonu() -> ast.FunctionDef:
    agac = ast.parse(_KAYNAK)
    for d in agac.body:
        if isinstance(d, ast.FunctionDef) and d.name == "run":
            return d
    raise AssertionError("run() bulunamadı")


def _ana_dongu() -> ast.While:
    """`while running:` — içinde `_watch_positions` geçen ana işlem döngüsü."""
    adaylar = [
        n for n in ast.walk(_run_fonksiyonu())
        if isinstance(n, ast.While)
        and "_watch_positions" in ast.dump(n)
    ]
    assert adaylar, "ana işlem döngüsü bulunamadı"
    return max(adaylar, key=lambda n: len(ast.dump(n)))


def test_dongu_govdesi_istisnaya_karsi_korumali():
    """Gövde try/except ile sarılı olmalı — yoksa tek hata botu öldürür."""
    dongu = _ana_dongu()
    korumali = [n for n in dongu.body if isinstance(n, ast.Try)]
    assert korumali, (
        "ana döngü gövdesinde genel istisna koruması yok — "
        "tek bir beklenmedik hata süreci öldürür ve açık pozisyonlar "
        "yeniden başlatma boyunca stopsuz kalır"
    )
    handlers = korumali[0].handlers
    assert handlers, "try var ama except yok"
    # Geniş yakalama şart: dar yakalama, öngörmediğimiz hatada yine öldürür.
    yakalanan = [
        h.type.id for h in handlers
        if isinstance(h.type, ast.Name)
    ]
    assert "Exception" in yakalanan, (
        f"yalnız {yakalanan} yakalanıyor — öngörülmeyen hata yine öldürür"
    )


def test_hata_isleyicisi_istisnayi_yeniden_firlatmaz():
    """`raise` varsa döngü yine ölür — koruma sahte olur."""
    dongu = _ana_dongu()
    tryblok = [n for n in dongu.body if isinstance(n, ast.Try)][0]
    for h in tryblok.handlers:
        for n in ast.walk(h):
            if isinstance(n, ast.Raise):
                pytest.fail("hata işleyicisi istisnayı yeniden fırlatıyor")


def test_hata_turunda_da_stop_kontrolu_calisir():
    """EN KRİTİK: tarama çökse bile stoplar kontrol edilmeli.

    Sinyal taraması pahalı ve kırılgan (yüzlerce sembol, göstergeler, ağ);
    stop kontrolü ucuz ve hayati. Pahalı olanın hatası ucuz olanı durdurursa
    pozisyonlar korumasız kalır — hatanın en pahalıya patladığı an tam da
    budur.
    """
    dongu = _ana_dongu()
    tryblok = [n for n in dongu.body if isinstance(n, ast.Try)][0]
    handler_kaynak = "\n".join(ast.dump(h) for h in tryblok.handlers)
    assert "_watch_positions" in handler_kaynak, (
        "hata işleyicisi stop kontrolünü çağırmıyor — tarama çöktüğünde "
        "açık pozisyonlar yönetilmeden kalır"
    )


def test_hata_sessiz_kalmaz():
    """Yutulan hata, olmayan hatadan tehlikelidir: bot 'çalışıyor' görünür."""
    dongu = _ana_dongu()
    tryblok = [n for n in dongu.body if isinstance(n, ast.Try)][0]
    kaynak = "\n".join(ast.dump(h) for h in tryblok.handlers)
    assert "logger" in kaynak, "hata loglanmıyor"
    assert "exc_info" in kaynak or "exception" in kaynak, (
        "yığın izi kaydedilmiyor — hatanın nerede doğduğu bilinemez"
    )


def test_hata_sicak_donguye_donusmez_ama_sonsuza_uzamaz():
    """Art arda hatada yavaşla, ama bekleme sınırsız büyümesin.

    Sınırsız backoff, botu teknik olarak yaşatıp fiilen öldürür — tam olarak
    kaçınmak istediğimiz 'yaşıyorum ama iş yapmıyorum' durumu.
    """
    dongu = _ana_dongu()
    tryblok = [n for n in dongu.body if isinstance(n, ast.Try)][0]
    kaynak = "\n".join(ast.dump(h) for h in tryblok.handlers)
    assert "sleep" in kaynak, "hata sonrası bekleme yok — sıcak döngü riski"
    assert "min" in kaynak, (
        "bekleme üst sınırı yok — art arda hatada bot fiilen durur"
    )


def test_toparlaninca_sayac_sifirlanir():
    """Sayaç sıfırlanmazsa bekleme kalıcı olarak uzun kalır."""
    dongu = _ana_dongu()
    tryblok = [n for n in dongu.body if isinstance(n, ast.Try)][0]
    assert tryblok.orelse, (
        "başarılı turda sayacı sıfırlayan `else` bloğu yok — "
        "tek bir eski hata yüzünden bot kalıcı olarak yavaşlar"
    )
    assert "_cycle_errors" in ast.dump(ast.Module(body=tryblok.orelse, type_ignores=[]))


def test_stop_kontrolu_kendi_icinde_de_korumali():
    """Son savunma hattı, kendisi patlarsa savunma değildir."""
    fn = [
        n for n in ast.walk(_run_fonksiyonu())
        if isinstance(n, ast.FunctionDef) and n.name == "_watch_positions"
    ]
    assert fn, "_watch_positions bulunamadı"
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn[0])), (
        "_watch_positions korumasız — hata işleyicisinden çağrıldığında "
        "patlarsa döngü yine ölür"
    )
