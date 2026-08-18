"""
Çoklu strateji deneyi — ölçüm ve karar katmanı testleri.

── BU DOSYA NEDEN VAR ───────────────────────────────────────────────────────
Buradaki asıl risk kodun patlaması değil, kapının FAZLA KOLAY AÇILMASI.
"En çok kazananı seç" kuralı, hiçbiri gerçekte avantaj taşımasa bile bir
kazanan üretir. Bu yüzden testlerin çoğu "doğru hesaplıyor mu" değil,
"yeterince veri yokken HAYIR diyebiliyor mu" sorusunu ölçer.

Sıfır avantajlı rastgele stratejilerden birinin kapıyı geçmesi, bu deneyin
tamamını anlamsızlaştırır: gerçek parayı en iyiye değil en şanslıya veririz.
"""

import random
import sqlite3
from datetime import datetime, timedelta

import pytest

from strategy_lab import (
    GateCriteria,
    build_scorecard,
    effective_sample_size,
    effective_trials,
    evaluate_gate,
    expected_max_of_n,
    halt_events,
    init_ledger,
    record_risk_event,
    record_trial,
    risk_action,
    stalled_strategies,
    RiskBreach,
    RISK_ACTION_HALT,
    RISK_ACTION_RECORD_ONLY,
)

BAS = datetime(2026, 8, 1, 9, 0)


def _islem(r, dk_sonra, pnl=None, komisyon=0.3):
    acilis = BAS + timedelta(minutes=dk_sonra)
    return {
        "r_multiple": r,
        "net_pnl": r * 10 if pnl is None else pnl,
        "commission": komisyon,
        "open_time": acilis.isoformat(),
        "close_time": (acilis + timedelta(minutes=30)).isoformat(),
    }


# ── Etkin örneklem ────────────────────────────────────────────────────────────

def test_ayni_dalgadaki_islemler_tek_gozlem_sayilir():
    """20 altcoinde aynı anda açılan 20 işlem, 20 bağımsız gözlem DEĞİLDİR.

    Bu ayrım kapının tamamını taşır: ham sayı kullanılsaydı örneklem 20 kat
    büyük görünür ve kapı çok erken açılırdı.
    """
    ayni_an = [_islem(0.5, dk) for dk in range(0, 20)]     # 20 dk içinde 20 işlem
    assert effective_sample_size(ayni_an) == 1

    ayri_gunler = [_islem(0.5, gun * 24 * 60) for gun in range(20)]
    assert effective_sample_size(ayri_gunler) == 20


def test_bos_liste_sifir_dondurur():
    assert effective_sample_size([]) == 0


# ── Seçim yanlılığı payı ──────────────────────────────────────────────────────

def test_secim_payi_deneme_sayisiyla_buyur():
    assert expected_max_of_n(1) == 0.0
    assert expected_max_of_n(5) > expected_max_of_n(3) > expected_max_of_n(2)
    assert expected_max_of_n(20) > expected_max_of_n(10)


# ── Kapı: yeterli veri yokken HAYIR ───────────────────────────────────────────

def test_az_veriyle_kapi_acilmaz():
    """Çok iyi görünen ama küçük örneklemli strateji terfi ETMEMELİ."""
    harika = [_islem(2.0, dk) for dk in range(0, 300, 30)]   # 10 işlem, hepsi +2R
    sc = build_scorecard("HARIKA", harika)
    sonuc = evaluate_gate([sc], trials=1)
    assert sonuc.status == "NOT_ENOUGH_DATA"
    assert not sonuc.promoted
    assert any("etkin örneklem" in r for r in sonuc.reasons)


def test_sifir_avantajli_stratejilerin_YANLIS_TERFI_ORANI_dusuk():
    """Deneyin bütün anlamı bu testte.

    Her turda beş stratejinin beşi de GERÇEKTE sıfır avantajlı. Sırf şansla
    biri en iyi görünür. Kapı onu terfi ettirirse gerçek parayı en iyiye değil
    en şanslıya vermiş oluruz.

    Tek tohumla "hiç terfi etmesin" demek yanlış olurdu: istatistiksel bir kapı
    zaman zaman yanılır, mesele YANILMA ORANIDIR. Bu yüzden çok tur koşulup
    oran ölçülüyor. (İlk hâlde eşik olarak beklenen maksimum kullanılıyordu ve
    oran ~%50'ydi; test bunu yakaladı.)
    """
    turlar = 40
    terfi = 0
    for tohum in range(turlar):
        rng = random.Random(1000 + tohum)
        kartlar = []
        for i in range(5):
            islemler = [
                _islem(rng.gauss(0.0, 1.31), gun * 24 * 60 + i)
                for gun in range(150)
            ]
            kartlar.append(build_scorecard(f"S{i}", islemler))
        if evaluate_gate(kartlar, trials=5).promoted:
            terfi += 1

    oran = terfi / turlar
    assert oran <= 0.15, (
        f"sıfır avantajlı stratejiler {terfi}/{turlar} turda terfi etti "
        f"(%{oran*100:.0f}) — kapı kurayı kenar sanıyor"
    )


def test_kar_tek_islemden_geliyorsa_reddedilir():
    """Yoğunlaşma: tekrarlanabilir avantaj tek bir vuruştan gelmez."""
    islemler = [_islem(-0.1, gun * 24 * 60, pnl=-1.0) for gun in range(120)]
    islemler.append(_islem(50.0, 121 * 24 * 60, pnl=500.0))
    sc = build_scorecard("TEK_VURUS", islemler)
    sonuc = evaluate_gate([sc], trials=1, criteria=GateCriteria(min_trades=50))
    assert not sonuc.promoted
    assert any("tek işlemden" in r or "beklenti" in r for r in sonuc.reasons)


def test_tek_donemde_kazanan_reddedilir():
    """Avantaj tek bir haftadan ibaretse tekrarlanabilir değildir."""
    iyi_hafta = [_islem(3.0, dk * 60) for dk in range(60)]
    kotu = [_islem(-0.2, (30 + gun) * 24 * 60) for gun in range(80)]
    sc = build_scorecard("TEK_HAFTA", iyi_hafta + kotu)
    sonuc = evaluate_gate(
        [sc], trials=1,
        criteria=GateCriteria(min_trades=50, min_effective_n=20, min_stable_periods=5),
    )
    assert not sonuc.promoted


def test_gercekten_iyi_strateji_yeterli_veriyle_terfi_EDEBILIR():
    """Kapı her şeyi reddetseydi işe yaramazdı — geçilebilir olmalı."""
    rng = random.Random(7)
    islemler = [
        _islem(rng.gauss(0.45, 0.8), gun * 12 * 60)
        for gun in range(400)
    ]
    sc = build_scorecard("GERCEK", islemler)
    sonuc = evaluate_gate(
        [sc], trials=1,
        criteria=GateCriteria(max_drawdown_r=50.0, min_stable_periods=3),
    )
    assert sonuc.promoted, f"gerçek avantaj reddedildi: {sonuc.reasons}"
    assert sonuc.winner == "GERCEK"
    assert sonuc.status == "LIVE_CANDIDATE"


def test_daha_cok_deneme_kapiyi_zorlastirir():
    """Aynı sonuç, daha çok denemeden sonra daha az inandırıcıdır."""
    rng = random.Random(11)
    islemler = [_islem(rng.gauss(0.30, 1.0), gun * 12 * 60) for gun in range(300)]
    sc = build_scorecard("A", islemler)
    kriter = GateCriteria(max_drawdown_r=60.0, min_stable_periods=2)
    az = evaluate_gate([sc], trials=1, criteria=kriter)
    cok = evaluate_gate([sc], trials=200, criteria=kriter)
    assert cok.selection_margin_r > az.selection_margin_r
    assert not (az.promoted is False and cok.promoted is True), \
        "daha çok deneme kapıyı KOLAYLAŞTIRDI — düzeltme ters çalışıyor"


def test_sistem_kendi_kendine_gercek_paraya_gecemez():
    """En iyi ihtimalle ADAY der; 'gerçek paraya geçtim' diyemez."""
    rng = random.Random(3)
    islemler = [_islem(rng.gauss(0.5, 0.7), gun * 12 * 60) for gun in range(400)]
    sc = build_scorecard("X", islemler)
    sonuc = evaluate_gate([sc], trials=1,
                          criteria=GateCriteria(max_drawdown_r=50.0))
    assert sonuc.status in ("LIVE_CANDIDATE", "REJECTED", "NOT_ENOUGH_DATA")
    assert sonuc.status != "LIVE"


# ── Deneme defteri ────────────────────────────────────────────────────────────

def test_elenen_denemeler_de_sayilir():
    """Yalnız kazananı saymak, düzeltmeyi olduğundan küçük gösterir."""
    con = sqlite3.connect(":memory:")
    init_ledger(con)
    for i in range(7):
        record_trial(con, "A", "v1", {"esik": i},
                     outcome="REJECTED", reason="beklenti düşük")
    record_trial(con, "A", "v1", {"esik": 3}, outcome="RUNNING")   # tekrar
    assert effective_trials(con) == 7, "aynı parametre iki kez sayıldı"

    record_trial(con, "B", "v1", {"esik": 0}, outcome="RUNNING")
    assert effective_trials(con) == 8


# ── Ölmesin ───────────────────────────────────────────────────────────────────

def test_kagit_modda_risk_ihlali_DURDURMAZ():
    """Deneyde durdurmak ölçümü sansürler: kötü kuyruk kesilir, strateji
    olduğundan iyi görünür. Üstelik durmuş strateji veri üretmez."""
    assert risk_action("paper") == RISK_ACTION_RECORD_ONLY


def test_gercek_parada_risk_ihlali_DURDURUR():
    assert risk_action("live") == RISK_ACTION_HALT


def test_bilinmeyen_mod_guvenli_tarafa_duser():
    """Yanlış yapılandırma sessizce riskli tarafa düşmemeli."""
    assert risk_action("") == RISK_ACTION_HALT
    assert risk_action("deneme") == RISK_ACTION_HALT


def test_ihlal_kaydediliyor_bilgi_kaybolmuyor():
    """İşlem durmadığı için olayın TEK izi bu kayıttır."""
    con = sqlite3.connect(":memory:")
    b = RiskBreach("A", "DAILY_LOSS", "-4%", datetime(2026, 8, 18, 12))
    assert record_risk_event(con, b, "paper") == RISK_ACTION_RECORD_ONLY
    assert halt_events(con, "A") == 1
    assert halt_events(con, "B") == 0


def test_sessiz_olum_yakalanir():
    """'Yaşıyorum ama iş yapmıyorum' bu sistemde bir kez yaşandı."""
    simdi = datetime(2026, 8, 18, 12)
    durum = {
        "A": simdi - timedelta(hours=1),
        "B": simdi - timedelta(hours=40),   # 40 saattir işlem yok
        "C": None,                          # hiç işlem açmamış
    }
    assert stalled_strategies(durum, max_silence_hours=24, now=simdi) == ["B", "C"]


# ── Birden fazla gerçek kazanan ───────────────────────────────────────────────

def test_iki_strateji_de_iyiyse_IKISI_DE_nitelikli_sayilir():
    """Tek kazanan seçmeye zorlamak patolojikti.

    İki strateji de gerçekten iyiyse, kazanan ikinciyi seçim payı kadar
    geçemediği için ESKİ kural ikisini birden eliyordu. Oysa doğru cevap
    "hangisi daha iyi bilinemez" — ikisi de nitelikli.
    """
    rng = random.Random(42)
    kartlar = []
    for i in range(2):
        islemler = [_islem(rng.gauss(0.5, 0.8), gun * 12 * 60 + i) for gun in range(400)]
        kartlar.append(build_scorecard(f"IYI{i}", islemler))

    sonuc = evaluate_gate(kartlar, trials=2,
                          criteria=GateCriteria(max_drawdown_r=50.0))
    assert len(sonuc.qualified) == 2, (
        f"iki gerçek kenar varken nitelikli sayısı {sonuc.qualified} — "
        "tek kazanan zorlaması ikisini birden eliyor"
    )
    assert sonuc.status == "LIVE_CANDIDATE"
    # Kazanan ilan edilmiş olabilir (biri ikinciyi payı aşarak geçtiyse) ama
    # ASIL mesele ikincinin listeden düşmemesi: gerçek bir kenar, birinciyle
    # kıyaslandığı için kaybolmamalı.
    assert set(sonuc.qualified) == {"IYI0", "IYI1"}


def test_hicbiri_iyi_degilse_nitelikli_bos():
    rng = random.Random(5)
    kartlar = []
    for i in range(3):
        islemler = [_islem(rng.gauss(0.0, 1.31), gun * 24 * 60 + i) for gun in range(200)]
        kartlar.append(build_scorecard(f"S{i}", islemler))
    sonuc = evaluate_gate(kartlar, trials=3)
    assert sonuc.qualified == []
    assert not sonuc.promoted or sonuc.winner is None


def test_kumelenmis_islemler_standart_hatayi_KUCULTMEZ():
    """Etkin örneklem yalnız raporda değil, KARARDA da kullanılmalı.

    Standart hata ham işlem sayısına bölünürse, aynı dalgada açılmış 200
    işlem 200 bağımsız gözlem sanılır; belirsizlik olduğundan küçük görünür
    ve kapı erken açılır. Bu test ikisini ayırt edebilen tek senaryoyu kurar:
    işlemler 10 kümede toplanmış (etkin 10, ham 200).
    """
    rng = random.Random(99)
    islemler = []
    for kume in range(10):                      # 10 ayrı gün
        taban = kume * 24 * 60
        for k in range(20):                     # her gün 20 işlem, 1 saat içinde
            islemler.append(_islem(rng.gauss(0.35, 1.31), taban + k * 2))

    sc = build_scorecard("KUMELI", islemler)
    assert sc.trades == 200
    assert sc.effective_n == 10, "kümeleme çalışmıyor, test anlamsız"

    ham_se = sc.stdev_r / (200 ** 0.5)
    assert sc.std_error > ham_se * 3, (
        f"standart hata {sc.std_error:.3f}, ham sayıya göre {ham_se:.3f} — "
        "etkin örneklem karara girmiyor, belirsizlik olduğundan küçük görünüyor"
    )

    # Ve kapı bu belirsizlikle terfi ETMEMELİ.
    sonuc = evaluate_gate([sc], trials=5,
                          criteria=GateCriteria(min_effective_n=5, min_trades=50))
    assert not sonuc.promoted, (
        "10 bağımsız gözlemle terfi etti — kümelenmiş veri bağımsız sanıldı"
    )
