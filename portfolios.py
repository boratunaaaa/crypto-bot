"""
Strateji başına AYRI sanal portföy.

Her strateji kendi sermayesiyle, kendi pozisyonlarıyla ve kendi PnL'iyle
çalışır; piyasa verisi, çıkış motoru ve emir simülasyonu ORTAKTIR.

── NEDEN AYRI PORTFÖY ───────────────────────────────────────────────────────
Deneyin amacı stratejileri KARŞILAŞTIRMAK. Ortak kasada çalışsalardı:
  * biri diğerinin sermayesini tüketir, ölçülen performans karışırdı;
  * biri pozisyon limitini doldurunca diğeri sinyal üretse de işlem açamazdı
    — yani B'nin sonucu A'nın işlem sıklığına bağlı olurdu;
  * bir stratejinin kötü günü diğerinin risk bütçesini küçültürdü.
Bunların hepsi karşılaştırmayı geçersiz kılar.

── NEDEN AYRI KOD TABANI DEĞİL ──────────────────────────────────────────────
Bugün bulunan iki kusur (bayat fiyatla stop, TP1 sonrası ölü bölge) ORTAK
kodtaydı. Beş ayrı bot olsaydı aynı hatayı beş kere ayrı ayrı bulmak
gerekirdi — ve büyük olasılıkla bulunmazdı. Ortak altyapı, hatanın bir kez
düzeltilmesini sağlar; ayrı portföy ise ölçümü ayırır. İkisi çelişmez.

── SESSİZ VERİ KAYBI TUZAĞI (kapatıldı) ─────────────────────────────────────
`open_positions` tablosunun birincil anahtarı yalnız `symbol`di ve kaydetme
işlemi tabloyu KOMPLE siliyordu. Çoklu stratejide bu:
  * aynı sembolü tutan iki stratejiden birinin sessizce ezilmesi,
  * bir stratejinin kaydının DİĞERLERİNİN pozisyonlarını silmesi
demekti. Anahtar (strategy, symbol) yapıldı; silme yalnız kendi stratejisini
kapsıyor. Bu modül o sözleşmeye dayanır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from database import load_open_positions, save_open_positions, state_get, state_set_many
from trader import PaperPortfolio

logger = logging.getLogger("portfolios")

# State anahtarları strateji adıyla ön eklenir. TREND için ESKİ düz anahtarlar
# korunur: mevcut botun geçmişi ve bakiyesi taşınmadan çalışmaya devam etmeli.
_LEGACY_STRATEGY = "TREND"
_FIELDS = ("paper_balance", "total_pnl", "total_commission", "wins", "losses")


def state_key(strategy: str, field: str) -> str:
    """`TREND` için eski düz anahtar, diğerleri için ön ekli.

    Geriye dönük uyumluluk kasıtlı: mevcut bot 1000$ bakiyesiyle çalışıyor ve
    onun state'ini yeni bir anahtara taşımak, taşıma sırasında bir hata olursa
    bakiyenin sessizce sıfırlanması demektir.
    """
    return field if strategy == _LEGACY_STRATEGY else f"{strategy}:{field}"


@dataclass
class StrategyBook:
    """Bir stratejinin sanal defteri: portföy + kimlik."""

    name: str
    portfolio: PaperPortfolio

    # ── Kalıcılık ────────────────────────────────────────────────────────────
    def save(self) -> None:
        p = self.portfolio
        state_set_many({
            state_key(self.name, "paper_balance"):    p.balance,
            state_key(self.name, "total_pnl"):        p.total_pnl,
            state_key(self.name, "total_commission"): getattr(p, "total_commission", 0.0),
            state_key(self.name, "wins"):             p.wins,
            state_key(self.name, "losses"):           p.losses,
        })
        save_open_positions(p.positions, strategy=self.name)

    def restore(self) -> bool:
        """Diskteki state'i yükle. Dönen: daha önce çalışmış mı."""
        kayitli = state_get(state_key(self.name, "paper_balance"))
        p = self.portfolio
        varmi = kayitli is not None
        if varmi:
            p.balance          = float(kayitli)
            p.total_pnl        = float(state_get(state_key(self.name, "total_pnl"), 0.0))
            p.total_commission = float(state_get(state_key(self.name, "total_commission"), 0.0))
            p.wins             = int(state_get(state_key(self.name, "wins"), 0))
            p.losses           = int(state_get(state_key(self.name, "losses"), 0))
        p.restore_positions(load_open_positions(strategy=self.name))
        return varmi

    # ── Ölçüm ────────────────────────────────────────────────────────────────
    @property
    def equity(self) -> float:
        """Nakit + açık pozisyonlarda kilitli marj.

        Karşılaştırma equity üzerinden yapılmalı: yalnız nakde bakmak, açık
        pozisyonu olan stratejiyi olduğundan fakir gösterir.
        """
        return self.portfolio.balance + sum(
            float(pos.get("margin", 0.0)) for pos in self.portfolio.positions.values()
        )


class PortfolioSet:
    """Stratejilerin sanal defterleri. Sözlük gibi kullanılır."""

    def __init__(self, strategies: dict[str, float]):
        """`strategies`: {strateji_adı: başlangıç_sermayesi}"""
        if not strategies:
            raise ValueError("en az bir strateji gerekir")
        self.books: dict[str, StrategyBook] = {
            ad: StrategyBook(ad, PaperPortfolio(sermaye))
            for ad, sermaye in strategies.items()
        }

    def __contains__(self, ad: str) -> bool:
        return ad in self.books

    def __getitem__(self, ad: str) -> StrategyBook:
        return self.books[ad]

    def __iter__(self):
        return iter(self.books.values())

    def __len__(self) -> int:
        return len(self.books)

    @property
    def names(self) -> list[str]:
        return list(self.books)

    def restore_all(self) -> dict[str, bool]:
        return {ad: kitap.restore() for ad, kitap in self.books.items()}

    def save_all(self) -> None:
        """Hepsini kaydet. Biri patlarsa DİĞERLERİ YİNE KAYDEDİLİR.

        Tek bir stratejinin bozuk verisi yüzünden tüm portföylerin diske
        yazılamaması, sessiz ve toplu veri kaybı olurdu.
        """
        for kitap in self.books.values():
            try:
                kitap.save()
            except Exception:
                logger.exception(f"{kitap.name}: portföy kaydedilemedi")

    def held_symbols(self, strategy: str) -> list[str]:
        return list(self.books[strategy].portfolio.positions)

    def all_held_symbols(self) -> set[str]:
        """Fiyat tazeleme için: hangi sembollerin fiyatı gerekiyor.

        Stratejiler AYNI sembolü tutabilir; fiyat bir kez çekilir, karar ayrı
        ayrı verilir. Portföyler bağımsızdır, piyasa verisi ortaktır.
        """
        return {
            sym
            for kitap in self.books.values()
            for sym in kitap.portfolio.positions
        }

    def total_equity(self) -> float:
        return sum(k.equity for k in self.books.values())
