"""
Çoklu strateji deneyi — ÖLÇÜM ve KARAR katmanı.

BU MODÜL İŞLEM AÇMAZ. Tek işi şu soruyu dürüstçe cevaplamak:
"Bu stratejilerden birinin gerçekten tekrarlanabilir bir avantajı var mı?"

── NEDEN ÖNCE BU YAZILDI ────────────────────────────────────────────────────
Çünkü asıl tehlike stratejilerde değil, KARAR KURALINDA. Birkaç strateji
çalıştırıp "en çok kazananı seç" demek, hiçbiri gerçekte avantaj taşımasa bile
bir kazanan üretir — seçmenin kendisi sahte bir üstünlük yaratır.

Ölçüldü (2026-08-18, bu botun kendi verisi): işlem başına R'nin standart
sapması 1.31. Buna göre, hepsi GERÇEKTE sıfır avantajlı olsa bile en iyi
görünenin sahte avantajı:

    strateji başına 50 işlem,  5 strateji  -> +0.216R/işlem
    strateji başına 300 işlem, 5 strateji  -> +0.088R/işlem

Değerli bir gerçek avantaj ~0.1-0.2R/işlem mertebesindedir. Yani 50 işlemde
seçim yapmak, gerçek bir avantajdan BÜYÜK bir yanılsamayı gerçek sanmaktır.

── BAĞIMSIZLIK YANILGISI ────────────────────────────────────────────────────
İlk hesabımız işlemleri bağımsız sayıyordu; bu YANLIŞ. Bot aynı momentum
dalgasında 20 altcoinde 20 pozisyon açıyorsa elimizde 20 bağımsız gözlem
değil, kabaca 1 gözlem vardır. Bu yüzden burada ham işlem sayısı değil ETKİN
örneklem (`effective_sample_size`) kullanılır: zaman içinde çakışan işlemler
tek kümeye indirilir ve karar ona göre verilir.

Bu, eşiği geçmeyi zorlaştırır. Kasıtlı.

── KAYIT DEFTERİ ────────────────────────────────────────────────────────────
Kaç deneme yapıldığı, sonucun ne kadar düzeltileceğini belirleyen girdidir.
50 parametre kombinasyonu deneyip yalnız kazananı "bir strateji" diye sunmak,
istatistiği sessizce bozar. `record_trial` her denemeyi — eleneni de — yazar
ve `effective_trials` düzeltmeyi o sayıya göre yapar.

── ÖLÇÜLEN KALİBRASYON ──────────────────────────────────────────────────────
Kapı simülasyonla sınandı (5 strateji, yalnız birinde gerçek kenar, R std 1.31):

    hiçbirinde kenar yok  ->  yanlış terfi %3-7   (hedef ~%5)
    kenar 0.15R, 400 işlem ->  doğru terfi %49
    kenar 0.25R, 400 işlem ->  doğru terfi %89
    kenar 0.40R, 400 işlem ->  doğru terfi %100

Yani kapı zayıf bir kenarı (0.15R) yarı yarıya kaçırır. Bu bilinçli: gerçek
parayı yanlış stratejiye vermek, iyi bir stratejiyi bir süre daha kâğıtta
tutmaktan pahalıdır. Kaçırılan kenar veri biriktikçe yakalanır.

Referans fikirler: Probability of Backtest Overfitting ve Deflated Sharpe
Ratio (Bailey & López de Prado); çoklu hipotez altında klasik eşiklerin
yetersizliği (Harvey, Liu & Zhu). Buradaki uygulama basitleştirilmiş bir
uyarlamadır, o makalelerin birebir yöntemi değildir — amaç kesin bir p-değeri
üretmek değil, "yeterince veri yok" diyebilen bir kapı kurmak.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Iterable, Sequence

# Beklenen maksimum: N bağımsız standart normalin en büyüğünün ortalaması.
# Seçim yanlılığı payı buradan gelir. (Simülasyonla doğrulanmış değerler.)
_E_MAX_N = {
    1: 0.000, 2: 0.564, 3: 0.846, 4: 1.029, 5: 1.163,
    6: 1.267, 7: 1.352, 8: 1.423, 9: 1.485, 10: 1.539,
}


def selection_threshold_z(trials: int, alpha: float = 0.05) -> float:
    """Çoklu deneme altında eşik — standart hata birimiyle.

    İLK HÂLİ YANLIŞTI: eşik olarak N denemenin BEKLENEN maksimumu
    kullanılıyordu. Beklenen değer dağılımın ORTASIDIR; hepsi sıfır avantajlı
    olsa bile kazanan zamanın yaklaşık yarısında onu aşar. Yani kapı, kurayı
    kenar sanma olasılığını ~%50'de bırakıyordu. Test bunu yakaladı: 5 sıfır
    avantajlı stratejiden biri +0.240R ile terfi etti.

    Doğrusu üst kuyruk: en iyi görünenin, hepsi sıfır avantajlıyken bu eşiği
    aşma olasılığı en fazla `alpha` olmalı. P(max < z) = Phi(z)^trials = 1-alpha
    olacak şekilde z seçilir.
    """
    n = max(1, trials)
    p = (1.0 - alpha) ** (1.0 / n)
    p = min(max(p, 1e-12), 1 - 1e-12)
    return NormalDist().inv_cdf(p)


def expected_max_of_n(n: int) -> float:
    """N denemenin en iyisinin, hepsi sıfır avantajlıyken beklenen üstünlüğü
    (standart hata birimiyle). 10'un ötesinde asimptotik yaklaşım."""
    if n <= 1:
        return 0.0
    if n in _E_MAX_N:
        return _E_MAX_N[n]
    # Blom yaklaşımı — büyük N için yeterli.
    return math.sqrt(2.0 * math.log(n)) - (
        math.log(math.log(n)) + math.log(4 * math.pi)
    ) / (2 * math.sqrt(2.0 * math.log(n)))


# ── Etkin örneklem ────────────────────────────────────────────────────────────

def effective_sample_size(
    trades: Sequence[dict], cluster_minutes: int = 60
) -> int:
    """Zaman içinde çakışan/kümelenen işlemleri TEK gözleme indirger.

    Neden: aynı piyasa hareketinde açılan işlemler aynı bahsin kopyalarıdır.
    Ham sayıyı kullanmak, örneklemi olduğundan büyük gösterip kapıyı erken
    açar — tam olarak kaçınmak istediğimiz hata budur.

    Kural: bir işlem, önceki kümenin son açılışından `cluster_minutes` içinde
    açıldıysa aynı kümeye girer. Küme sayısı etkin örneklemdir.
    """
    zamanlar = []
    for t in trades:
        ot = t.get("open_time")
        if not ot:
            continue
        if isinstance(ot, str):
            try:
                ot = datetime.fromisoformat(ot)
            except ValueError:
                continue
        zamanlar.append(ot)
    if not zamanlar:
        return 0

    zamanlar.sort()
    pencere = timedelta(minutes=cluster_minutes)
    kume = 1
    son = zamanlar[0]
    for z in zamanlar[1:]:
        if z - son > pencere:
            kume += 1
        son = z
    return kume


# ── Skor kartı ────────────────────────────────────────────────────────────────

@dataclass
class Scorecard:
    """Bir stratejinin ölçülen hâli. Dolar kârı BİLEREK ikincil metriktir:
    daha çok risk alan strateji doğal olarak daha çok dolar kazanır."""

    strategy: str
    trades: int = 0
    effective_n: int = 0
    expectancy_r: float = 0.0        # işlem başına ortalama R
    stdev_r: float = 0.0
    std_error: float = 0.0           # ETKİN örnekleme göre
    win_rate: float = 0.0
    max_drawdown_r: float = 0.0
    concentration: float = 0.0       # en iyi tek işlemin toplam kârdaki payı
    net_pnl: float = 0.0             # ikincil
    commission: float = 0.0
    periods: dict = field(default_factory=dict)   # dönem -> expectancy_r
    stable_periods: int = 0          # kaç dönemde pozitif

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def _drawdown_r(rs: Sequence[float]) -> float:
    tepe = 0.0
    kumulatif = 0.0
    en_kotu = 0.0
    for r in rs:
        kumulatif += r
        tepe = max(tepe, kumulatif)
        en_kotu = min(en_kotu, kumulatif - tepe)
    return abs(en_kotu)


def build_scorecard(
    strategy: str, trades: Sequence[dict], period_days: int = 7
) -> Scorecard:
    """İşlem listesinden skor kartı üretir. `trades` kapanmış işlemler olmalı."""
    sc = Scorecard(strategy=strategy)
    kapali = [t for t in trades if t.get("r_multiple") is not None]
    sc.trades = len(kapali)
    if not kapali:
        return sc

    rs = [float(t["r_multiple"]) for t in kapali]
    sc.expectancy_r = sum(rs) / len(rs)
    if len(rs) > 1:
        ort = sc.expectancy_r
        sc.stdev_r = math.sqrt(sum((r - ort) ** 2 for r in rs) / len(rs))
    sc.effective_n = effective_sample_size(kapali)
    if sc.effective_n > 0:
        sc.std_error = sc.stdev_r / math.sqrt(sc.effective_n)
    sc.win_rate = 100.0 * sum(1 for r in rs if r > 0) / len(rs)
    sc.max_drawdown_r = _drawdown_r(rs)
    sc.net_pnl = sum(float(t.get("net_pnl") or 0) for t in kapali)
    sc.commission = sum(float(t.get("commission") or 0) for t in kapali)

    # Yoğunlaşma: kâr tek bir işlemden mi geliyor? (0-1; 1 = tamamı)
    kazanclar = [float(t.get("net_pnl") or 0) for t in kapali]
    pozitif = sum(x for x in kazanclar if x > 0)
    if pozitif > 0:
        sc.concentration = max(kazanclar) / pozitif

    # Dönem istikrarı: avantaj tek bir haftadan mı ibaret?
    kovalar: dict[str, list[float]] = {}
    for t in kapali:
        ct = t.get("close_time")
        if isinstance(ct, str):
            try:
                ct = datetime.fromisoformat(ct)
            except ValueError:
                continue
        if not ct:
            continue
        anahtar = (ct - timedelta(days=ct.timetuple().tm_yday % period_days)).date().isoformat()
        kovalar.setdefault(anahtar, []).append(float(t["r_multiple"]))
    sc.periods = {k: sum(v) / len(v) for k, v in sorted(kovalar.items())}
    sc.stable_periods = sum(1 for v in sc.periods.values() if v > 0)
    return sc


# ── Terfi kapısı ──────────────────────────────────────────────────────────────

@dataclass
class GateCriteria:
    """Kriterler ÖNCEDEN yazılır. Sonuçları görüp eşik ayarlamak, seçim
    yanlılığını arka kapıdan geri davet etmektir."""

    min_effective_n: int = 30          # ETKİN gözlem (ham işlem değil)
    min_trades: int = 100              # ham işlem tabanı
    min_expectancy_r: float = 0.10     # maliyet sonrası, işlem başına
    max_drawdown_r: float = 15.0
    max_concentration: float = 0.50    # kârın yarısından fazlası tek işlemse ret
    min_stable_periods: int = 3        # en az 3 ayrı dönemde pozitif
    margin_multiple: float = 1.0       # rakibi seçim payının kaç katı geçmeli


@dataclass
class GateResult:
    status: str                # NOT_ENOUGH_DATA | REJECTED | LIVE_CANDIDATE
    winner: str | None         # tek kazanan seçilebildiyse
    reasons: list[str]
    selection_margin_r: float
    scorecards: list[Scorecard]
    # MUTLAK ölçütleri tek başına geçen stratejiler.
    #
    # Neden ayrı: "tek kazanan seç" kuralı, iki strateji de GERÇEKTEN iyiyken
    # patolojik davranıyordu — kazanan ikinciyi seçim payı kadar geçemediği
    # için ikisi birden elenirdi. Oysa doğru cevap "hangisi daha iyi
    # bilinemez" değil, "ikisi de nitelikli". Farklı piyasa koşullarında
    # kazanıyorlarsa birlikte kullanmak tek birini seçmekten iyidir.
    #
    # UYARI: nitelikli üç strateji aynı hareketlerden kazanıyorsa elimizde üç
    # strateji değil, üç kılığa girmiş TEK risk vardır. Bu liste otomatik
    # dağıtım için değil, korelasyon incelemesine girdi olarak kullanılmalı.
    qualified: list[str] = field(default_factory=list)

    @property
    def promoted(self) -> bool:
        return self.status == "LIVE_CANDIDATE"


def evaluate_gate(
    scorecards: Sequence[Scorecard],
    trials: int,
    criteria: GateCriteria | None = None,
) -> GateResult:
    """Kazananı İLAN ETME kararı. Sistem kendi kendine gerçek paraya geçemez;
    en iyi ihtimalle `LIVE_CANDIDATE` der ve insan onayına bırakır.

    `trials` = şimdiye kadar DENENMİŞ toplam strateji/parametre sayısı.
    Yalnız hayatta kalanları saymak düzeltmeyi olduğundan küçük gösterir.
    """
    c = criteria or GateCriteria()
    kartlar = sorted(scorecards, key=lambda s: s.expectancy_r, reverse=True)
    if not kartlar:
        return GateResult("NOT_ENOUGH_DATA", None, ["hiç strateji yok"], 0.0, [], [])

    en_iyi = kartlar[0]
    sebepler: list[str] = []

    # Seçim payı: en iyi görünenin, hepsi sıfır avantajlıyken beklenen sahte
    # üstünlüğü. Denenen strateji sayısı arttıkça büyür.
    pay = selection_threshold_z(max(trials, len(kartlar))) * en_iyi.std_error

    yeterli = True
    if en_iyi.effective_n < c.min_effective_n:
        sebepler.append(
            f"etkin örneklem {en_iyi.effective_n} < {c.min_effective_n} "
            f"(ham işlem {en_iyi.trades}; çakışan işlemler tek gözleme indirildi)"
        )
        yeterli = False
    if en_iyi.trades < c.min_trades:
        sebepler.append(f"ham işlem {en_iyi.trades} < {c.min_trades}")
        yeterli = False
    if not yeterli:
        return GateResult("NOT_ENOUGH_DATA", None, sebepler, pay, kartlar, [])

    # Mutlak ölçütleri tek başına geçenler (birbirleriyle kıyaslamadan).
    nitelikli = [
        k.strategy for k in kartlar
        if k.effective_n >= c.min_effective_n
        and k.trades >= c.min_trades
        and k.expectancy_r >= c.min_expectancy_r
        and k.expectancy_r > selection_threshold_z(max(trials, len(kartlar))) * k.std_error
        and k.max_drawdown_r <= c.max_drawdown_r
        and k.concentration <= c.max_concentration
        and k.stable_periods >= c.min_stable_periods
    ]

    # Buradan sonrası: veri yeterli, strateji gerçekten iyi mi?
    if en_iyi.expectancy_r < c.min_expectancy_r:
        sebepler.append(
            f"beklenti {en_iyi.expectancy_r:+.3f}R < {c.min_expectancy_r}R"
        )
    if en_iyi.expectancy_r <= pay * c.margin_multiple:
        sebepler.append(
            f"beklenti {en_iyi.expectancy_r:+.3f}R, seçim payı {pay:.3f}R — "
            f"üstünlük kuradan ayırt edilemiyor"
        )
    if len(kartlar) > 1:
        ikinci = kartlar[1]
        fark = en_iyi.expectancy_r - ikinci.expectancy_r
        if fark <= pay * c.margin_multiple:
            sebepler.append(
                f"ikinciyi yalnız {fark:+.3f}R geçiyor, seçim payı {pay:.3f}R — "
                f"sıralama tesadüf olabilir"
            )
    if en_iyi.max_drawdown_r > c.max_drawdown_r:
        sebepler.append(
            f"düşüş {en_iyi.max_drawdown_r:.1f}R > {c.max_drawdown_r}R"
        )
    if en_iyi.concentration > c.max_concentration:
        sebepler.append(
            f"kârın %{en_iyi.concentration*100:.0f}'i tek işlemden — "
            f"tekrarlanabilir değil"
        )
    if en_iyi.stable_periods < c.min_stable_periods:
        sebepler.append(
            f"yalnız {en_iyi.stable_periods} dönemde pozitif "
            f"(gereken {c.min_stable_periods})"
        )

    if sebepler:
        # Tek kazanan seçilemedi. Ama mutlak ölçütleri geçen varsa bu bir
        # "hepsi kötü" sonucu değildir; ikisi birden nitelikli olabilir.
        durum = "LIVE_CANDIDATE" if nitelikli else "REJECTED"
        kazanan = en_iyi.strategy if (nitelikli and len(nitelikli) == 1) else None
        return GateResult(durum, kazanan, sebepler, pay, kartlar, nitelikli)
    return GateResult("LIVE_CANDIDATE", en_iyi.strategy, [], pay, kartlar, nitelikli)


# ── Deneme defteri ────────────────────────────────────────────────────────────

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_trials (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy         TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    params           TEXT NOT NULL,
    data_window      TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    outcome          TEXT,
    reason           TEXT,
    scorecard        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trials_strategy ON strategy_trials(strategy);
"""


def init_ledger(con: sqlite3.Connection) -> None:
    con.executescript(_LEDGER_SCHEMA)


def record_trial(
    con: sqlite3.Connection,
    strategy: str,
    version: str,
    params: dict,
    *,
    data_window: str = "",
    outcome: str = "RUNNING",
    reason: str = "",
    scorecard: Scorecard | None = None,
    now: datetime | None = None,
) -> int:
    """Her denemeyi yaz — ELENENİ DE.

    Elenenleri yazmamak, kaç kere denendiğini gizler ve `effective_trials`
    düzeltmeyi olduğundan küçük gösterir; yani defterin yokluğu istatistiği
    sessizce iyimserleştirir.
    """
    init_ledger(con)
    cur = con.execute(
        "INSERT INTO strategy_trials(strategy, strategy_version, params, "
        "data_window, started_at, outcome, reason, scorecard) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            strategy, version, json.dumps(params, sort_keys=True), data_window,
            (now or datetime.now()).isoformat(), outcome, reason,
            json.dumps(scorecard.as_dict(), default=str) if scorecard else None,
        ),
    )
    con.commit()
    return int(cur.lastrowid or 0)


def effective_trials(con: sqlite3.Connection) -> int:
    """Şimdiye kadar denenmiş AYIRT EDİLEBİLİR konfigürasyon sayısı.

    Aynı strateji+sürüm+parametre üçlüsünün tekrarı tek deneme sayılır;
    farklı parametre ayrı denemedir — çünkü seçim yanlılığını büyüten şey
    denenen ayrı konfigürasyon sayısıdır.
    """
    init_ledger(con)
    row = con.execute(
        "SELECT COUNT(*) c FROM (SELECT DISTINCT strategy, strategy_version, params "
        "FROM strategy_trials)"
    ).fetchone()
    return int(row[0] if row else 0)


# ── Risk olayları: durdurmak ÖLÇMEYİ bozar ────────────────────────────────────
#
# Kullanıcı şartı: "botlar kesinlikle çalışmayı durdurmasın, mutlaka sonuçları
# öğrenelim." Bu şart yalnız pratik değil, İSTATİSTİKSEL olarak da doğru.
#
# Bir stratejiyi zarar ederken durdurmak, ölçümü sansürler: kötü sonuçların
# kuyruğunu keser ve geriye kalan veri stratejiyi olduğundan iyi gösterir.
# Deney sırasında "günlük zarar limiti" uygulamak, tam da ölçmek istediğimiz
# dağılımı bozmak demektir. Üstelik durmuş bir strateji veri üretmez; yedi
# hafta beklenip elde hiçbir şey olmaması bu botta bir kez yaşandı (2,5 gün
# boyunca kârlı bir haftada kendini durdurmuştu).
#
# Ama risk limitini tamamen SİLMEK de yanlış olur: gerçek paraya geçince o
# limitler hayat kurtarır. Ayrım şu:
#
#   KAĞIT/DENEY  -> limit UYGULANMAZ, olay KAYDEDİLİR (ölçüm bozulmasın)
#   GERÇEK PARA  -> limit UYGULANIR, işlem durur (sermaye korunsun)
#
# Böylece skor kartı "bu strateji gerçek parada şu noktada durdurulurdu"
# bilgisini de taşır; bilgi kaybolmaz, yalnız veri akışı kesilmez.

_MODE_PAPER = "paper"
_MODE_LIVE = "live"

RISK_ACTION_HALT = "HALT"
RISK_ACTION_RECORD_ONLY = "RECORD_ONLY"


@dataclass
class RiskBreach:
    strategy: str
    kind: str                    # DAILY_LOSS | WEEKLY_LOSS | DRAWDOWN | ...
    detail: str
    at: datetime


def risk_action(mode: str) -> str:
    """Risk ihlalinde ne yapılacağı. Deneyde ASLA durdurma.

    `mode` tanınmıyorsa GERÇEK PARA gibi davranılır (durdur). Bilinmeyen bir
    moda "devam et" demek, yanlış yapılandırmayı sessizce riskli tarafa
    düşürürdü; kapı fail-safe olmalı.
    """
    return RISK_ACTION_RECORD_ONLY if mode == _MODE_PAPER else RISK_ACTION_HALT


_BREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_risk_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    mode       TEXT NOT NULL,
    action     TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_strategy ON strategy_risk_events(strategy);
"""


def record_risk_event(
    con: sqlite3.Connection, breach: RiskBreach, mode: str
) -> str:
    """İhlali yaz ve ne yapılacağını döndür. Yazmak ZORUNLU: deneyde işlem
    durmadığı için, olayın tek izi bu kayıttır."""
    con.executescript(_BREACH_SCHEMA)
    action = risk_action(mode)
    con.execute(
        "INSERT INTO strategy_risk_events(strategy, kind, detail, mode, action, at) "
        "VALUES(?,?,?,?,?,?)",
        (breach.strategy, breach.kind, breach.detail, mode, action,
         breach.at.isoformat()),
    )
    con.commit()
    return action


def halt_events(con: sqlite3.Connection, strategy: str) -> int:
    """Bu strateji gerçek parada kaç kez durdurulmuş olurdu."""
    con.executescript(_BREACH_SCHEMA)
    row = con.execute(
        "SELECT COUNT(*) c FROM strategy_risk_events WHERE strategy = ?",
        (strategy,),
    ).fetchone()
    return int(row[0] if row else 0)


# ── Sessiz ölüm dedektörü ─────────────────────────────────────────────────────

def stalled_strategies(
    scorecards_last_seen: dict[str, datetime | None],
    max_silence_hours: float = 24.0,
    now: datetime | None = None,
) -> list[str]:
    """Uzun süredir işlem AÇMAYAN stratejiler.

    "Durmasın" şartının denetlenebilir hâli. Bir strateji çökmüş, sinyal
    üretemiyor ya da filtresi her şeyi eliyorsa bu HİÇBİR göstergede kendini
    belli etmez — kuyrukta bekleyen iş ile sessizce ölmüş iş aynı görünür.
    Bu bu sistemde bir kez yaşandı; o yüzden ayrı bir kontrol.
    """
    ref = now or datetime.now()
    sinir = timedelta(hours=max_silence_hours)
    durmus = []
    for ad, son in scorecards_last_seen.items():
        if son is None or (ref - son) > sinir:
            durmus.append(ad)
    return sorted(durmus)
