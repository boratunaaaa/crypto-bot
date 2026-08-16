"""
Bot ayarları.

Tasarım ilkeleri (2026-08-12 refactor):
1. Risk dolar cinsinden sabittir; kaldıraç riski DEĞİŞTİRMEZ (bkz. RISK_PER_TRADE_PCT).
2. Kazananlar kısmi çıkışla korunur ama üst limitsiz koşar (bkz. PARTIAL_* ve CHANDELIER_*).
3. Günlük durdurma işlem SAYISINA değil, harcanan RİSKE bakar (bkz. DAILY_RISK_BUDGET_R).
4. Komisyon her hesaba dahildir — breakeven stop bile komisyonun üstüne konur.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ─── API ───────────────────────────────────────────────────────────────────────
API_KEY    = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]
TESTNET    = False

# ─── Mod ──────────────────────────────────────────────────────────────────────
PAPER_TRADING = True   # ← GERÇEK PARA KAPALI. Değiştirmeden önce go_live_check.py çalıştır.
SPOT_MODE     = False  # Futures modu — kaldıraç + long/short

# ─── Timeframe (multi-timeframe 4 katman) ────────────────────────────────────
TF_ENTRY   = "5m"   # giriş sinyali (1m'den daha az gürültülü)
TF_TREND   = "15m"  # kısa vadeli trend
TF_MACRO   = "1h"   # orta vadeli yön
TF_CONTEXT = "4h"   # büyük resim — en ağırlıklı filtre
CANDLE_LIMIT = 120

# ─── Coin evreni ──────────────────────────────────────────────────────────────
# 2026-08-12: TOP_N 100→250. missed_signals analizi, kaçan yüksek skorlu
# sinyallerin PENGU/TST/BICO/TUT/PUMP/BOME gibi düşük-cap pump coinlerinde
# yoğunlaştığını gösterdi — bunlar hacim sıralamasında dalgalanıyor, 100'lük
# pencerede bazen içeride bazen dışarıda kalıyorlar. Likidite filtresi
# (MIN_QUOTE_VOLUME_24H) çöp coinleri zaten eliyor, sıralama tavanı değil.
USE_ELITE_COINS = False
ELITE_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "NEARUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "SUIUSDT",
    "TRXUSDT", "OPUSDT", "ARBUSDT", "PEPEUSDT", "APTUSDT",
]
TOP_N_COINS              = 250
COIN_REFRESH_SEC         = 3600
OHLCV_REFRESH_SEC        = 60
OHLCV_REFRESH_SEC_CTX    = 900
SIGNAL_INTERVAL          = 30

# Likidite / execution kalitesi filtreleri
MIN_QUOTE_VOLUME_24H     = 3_000_000.0   # 24s hacim (USDT) — altı taranmaz
MAX_SPREAD_PCT           = 0.0035        # %0.35'ten geniş spread → işlem açma

# ─── Piyasa Rejimi (ADX) ──────────────────────────────────────────────────────
ADX_PERIOD           = 14
ADX_TREND_THRESHOLD  = 20
ADX_RANGE_THRESHOLD  = 15

# ─── Risk / Pozisyon ──────────────────────────────────────────────────────────
# İşlem başına risk, EQUITY'nin yüzdesi olarak. Bu, stop'a çarpıldığında
# kaybedilecek dolar miktarıdır ve kaldıraçtan BAĞIMSIZDIR.
RISK_PER_TRADE_PCT       = 0.0075   # normal işlem: %0.75
RISK_PER_TRADE_STRONG_PCT= 0.01     # çok güçlü sinyal (skor >= STRONG_SCORE): %1.0
STRONG_SIGNAL_SCORE      = 82

# Dinamik risk — son işlemlere göre ölçekle
DYNAMIC_RISK_ENABLED   = True
RISK_MIN_PCT           = 0.004   # taban %0.4
RISK_MAX_PCT           = 0.012   # tavan %1.2
DYNAMIC_RISK_LOOKBACK  = 15

# Teminat tavanı — SADECE marjin yeterliliği içindir, riski belirlemez.
# ESKİ BUG: bu tavan risk hesabını eziyordu, dolayısıyla gerçek dolar riski
# kaldıraçla birlikte büyüyordu (5x işlemler 2x'in 2-3 katı risk taşıyordu).
# Artık tavana takılan pozisyon KÜÇÜLTÜLMEZ, REDDEDİLİR — böylece her işlem
# ya hedeflenen riski taşır ya da hiç açılmaz.
MAX_MARGIN_PCT            = 0.35   # tek pozisyon teminatı equity'nin max %35'i
MAX_TOTAL_MARGIN_PCT      = 0.80   # tüm açık pozisyonların teminat toplamı tavanı

MAX_OPEN_POSITIONS        = 6      # 4 → 6 (kaçan sinyallerin %15.8'i bu limitti)
MAX_CORRELATED_POSITIONS  = 4      # aynı yönde en fazla 4
MIN_MARGIN_USDT           = 5.0
MAX_PORTFOLIO_RISK_R      = 4.0    # aynı anda açık toplam risk: max 4R

# ─── Korelasyon / stres limiti ───────────────────────────────────────────────
# `MAX_CORRELATED_POSITIONS` sayı sayar, birlikte hareketi görmez. Kriptoda
# altcoin'ler BTC ile yüksek korelasyonludur: 4 ayrı alt long, dört bağımsız
# 1R bahsi DEĞİL, tek bir 4R'lik BTC bahsidir. Bu iki tavan onu ölçer.
CORRELATION_GUARD_ENABLED = True
MAX_NET_DIRECTIONAL_R     = 3.0    # beta ağırlıklı net yön maruziyeti
STRESS_SHOCK_PCT          = 0.05   # BTC'de ani %5 hareket senaryosu
MAX_STRESS_LOSS_R         = 3.0    # o şokta izin verilen toplam zarar
BETA_LOOKBACK_BARS        = 120

# ─── Kill Switch ──────────────────────────────────────────────────────────────
# Günlük RİSK BÜTÇESİ — işlem sayısı limitinin yerini alır.
# Her açılan işlem bütçeden riski kadar (1R) düşer. Kapanan kârlı işlem
# bütçeyi geri besler (max kendi riski kadar). Böylece iyi giden günde bot
# daha çok işlem yapar, kötü giden günde kendini durdurur.
DAILY_RISK_BUDGET_R    = 8.0
RISK_BUDGET_REFUND     = True   # kârlı kapanış bütçeyi geri besler

# Runaway/bug koruması — normal işleyişte ASLA bağlamaz.
MAX_DAILY_TRADES_HARD  = 40

DAILY_LOSS_LIMIT_PCT   = 0.04   # günde max %4 zarar
WEEKLY_LOSS_LIMIT_PCT  = 0.10   # haftada max %10 zarar
MAX_DRAWDOWN_PCT       = 0.20   # tepe bakiyeden %20 düşüş → tam durdurma
MAX_CONSECUTIVE_LOSSES = 4
KILL_SWITCH_COOLDOWN_HOURS = 2

# ─── Drawdown kapısının çıkışı ────────────────────────────────────────────────
# DENETİM BULGUSU (2026-08-16): drawdown kapısında soğuma YOKTU ve
# `high_water_mark` yalnız artıyordu. Bakiye ancak işlemle yükselir, işlem de bu
# kapıyla kapalı — yani kapı bir kez bağlayınca bot KALICI olarak ölüyordu ve
# (nabız mesajı düzeltilene kadar) bunu kimseye söylemiyordu. Bu, aynı hafta
# haftalık zarar limitinde yaşanan sessiz kilidin birebir aynısı.
#
# Kapının DURDURMASI doğru; kalıcı ve sessiz olması yanlış. Bu yüzden:
#   • kâğıt modda soğuma dolunca taban bir kez yenilenir ve ölçüm sürer
#     (durmuş bir kâğıt bot hiç veri üretmez — en kötü sonuç budur),
#   • GERÇEK modda otomatik kurtarma YOKTUR; insan bakana kadar durur.
DRAWDOWN_COOLDOWN_HOURS    = 12
DRAWDOWN_AUTO_REBASE_PAPER = True

# Aynı sembolde arka arkaya işlem açmadan önce bekleme (dakika)
SYMBOL_COOLDOWN_MIN    = 30

# ─── Komisyon / slippage / funding ────────────────────────────────────────────
COMMISSION_RATE = 0.0005   # %0.05 taker fee (her açma/kapamada, notional üzerinden)
SLIPPAGE_PCT    = 0.0004   # taban kayma — backtest VE paper, ikisi de uygular

# Stop emirleri piyasa emridir ve hareketin sert olduğu anda tetiklenir; normal
# emirden daha kötü dolar. Kâğıt sonucun canlıyı olduğundan iyi göstermemesi
# için stop çıkışlarına ek kayma uygulanır.
SLIPPAGE_STOP_MULT = 2.5

# Perpetual funding — 8 saatte bir, notional üzerinden.
FUNDING_RATE_8H = 0.0001

# ─── Veri tazeliği ────────────────────────────────────────────────────────────
# Bayat fiyatla pozisyon AÇILMAZ. WebSocket sessizce düşerse eski fiyat
# sözlükte kalıyor ve fark edilmiyordu.
MAX_PRICE_AGE_SEC = 20
MAX_OHLCV_AGE_SEC = 600

# ─── Sermaye hazırlama ────────────────────────────────────────────────────────
# DENETİM BULGUSU: bot gerçek modda AÇILIŞTA spot hesaptaki USDT/BNB dışındaki
# TÜM varlıkları satıyordu — bayrak yok, onay yok. systemd `Restart=always`
# altında her çökme-yeniden başlatmada tekrarlanıyordu, yani botla ilgisi
# olmayan kişisel coinler de tasfiye olabilirdi. Artık varsayılan KAPALI ve
# yalnız `--prepare-capital` bayrağıyla elle çalıştırılır.
AUTO_LIQUIDATE_SPOT = False

# ─── Kaldıraç ─────────────────────────────────────────────────────────────────
# Kaldıraç artık KÂRI DEĞİŞTİRMEZ (risk-bazlı boyutlandırmada notional
# kaldıraçtan bağımsızdır). Tek işlevi aynı anda kaç pozisyon taşınabildiğidir.
# Bu yüzden güven skoruyla ilişkisi kaldırıldı ve tavan 3x'e çekildi:
# ölçülen veri 5x diliminin net zararda olduğunu gösteriyordu (8 işlem, -$5.77).
LEVERAGE_DEFAULT = 3
LEVERAGE_MAX     = 3
LEV_WEAK    = 2   # geriye dönük uyumluluk (backtest)
LEV_MEDIUM  = 3
LEV_STRONG  = 3
MARGIN_MODE = "ISOLATED"

LIQ_SAFETY_BUFFER = 0.50

# ─── ATR tabanlı dinamik SL/TP ────────────────────────────────────────────────
ATR_PERIOD      = 14
SL_ATR_MULT     = 2.0   # SL = giriş ± 2.0×ATR
TP_ATR_MULT     = 3.5   # ilk hedef referansı (artık zorunlu çıkış değil)
MAX_SL_PCT      = 0.04
MAX_TP_PCT      = 0.12

# ─── ÇIKIŞ MOTORU — kısmi kâr + limitsiz runner ──────────────────────────────
# Kullanıcı talebi: "kâra geçtikten sonra ani düşüşten etkilenmeyelim" +
# "üst limitsiz maksimum kazanç". Bu ikisi sabit-sıkı stop ile aynı anda
# sağlanamaz (ort. 5dk ATR %1.26 — %1'lik stop gürültünün içinde kalır).
# Çözüm: kârın bir kısmını ERKEN NAKDE ÇEVİR, kalanı ATR ile ölçeklenen
# stop'la SONSUZA KADAR koştur.
PARTIAL_EXITS_ENABLED = True
PARTIAL_TP1_R         = 1.0    # +1R'de
PARTIAL_TP1_PCT       = 0.40   # pozisyonun %40'ı realize edilir
PARTIAL_TP2_R         = 2.0    # +2R'de
PARTIAL_TP2_PCT       = 0.30   # %30 daha realize edilir (kalan %30 runner)

# TP1 sonrası stop breakeven'e çekilir — ama KOMİSYONUN ÜSTÜNE.
# Böylece "kâr etmesek bile zarar etmeyelim" garantisi gerçekten sağlanır.
BREAKEVEN_AFTER_TP1   = True
BREAKEVEN_BUFFER_PCT  = 0.0015  # giriş + %0.15 (round-trip komisyon %0.10'u aşar)

# Runner trailing — Chandelier (en yüksek fiyat - N×ATR).
# SABİT MAKSİMUM TAKE PROFIT YOKTUR. Coin %20, %50, %200 yükselirse
# pozisyon açık kalır; yalnızca trailing stop'a değince çıkılır.
CHANDELIER_ATR_MULT_STRONG = 3.5   # momentum güçlü → geniş bırak, hareketi boğma
CHANDELIER_ATR_MULT_WEAK   = 1.8   # momentum zayıfladı → sıkılaştır, kârı koru
CHANDELIER_MIN_LOCK_R      = 0.30  # trailing devreye girince en az 0.30R kilitli
TRAIL_MOMENTUM_LOOKBACK    = 8     # (eski, döngü tabanlı — yalnız uyumluluk)
TRAIL_MOMENTUM_SECONDS     = 240   # son 4 dakikada yeni zirve varsa "güçlü"

# Geriye dönük uyumluluk (eski kod yolları / backtest)
TRAIL_AFTER_TP        = True
TRAIL_ATR_MULT        = 2.5
TRAIL_MIN_LOCK_PCT    = 0.01

# ─── TIME STOP ────────────────────────────────────────────────────────────────
# Momentum işlemi açıldıktan sonra hareket gelmiyorsa sermayeyi kilitleme.
TIME_STOP_ENABLED     = True
TIME_STOP_MINUTES     = 90     # bu süre içinde
TIME_STOP_MIN_MFE_R   = 0.5    # en az 0.5R lehte hareket olmadıysa → çık
TIME_STOP_MAX_MINUTES = 480    # runner olmayan pozisyon için mutlak üst süre

# ─── Volatilite filtresi ──────────────────────────────────────────────────────
MIN_ATR_PCT     = 0.003
MAX_ATR_PCT     = 0.08    # 0.06 → 0.08: pump coinleri dışlamamak için

# ─── Sinyal eşikleri — TREND stratejisi ──────────────────────────────────────
# 60 → 72. Günlük işlem limiti risk bütçesine çevrildiği için bot artık daha
# çok işlem açabiliyor; kalite çıtası yükseltilmezse komisyon yükü artardı
# (ölçüm: komisyon şu an brüt kârın %8.3'ü, işlem başına $0.31).
MIN_SIGNAL_SCORE = 72
MIN_CONFIDENCE   = 0.72

# ─── STRATEJİ: MOMENTUM_FAST ─────────────────────────────────────────────────
# Ayrı, izole momentum motoru. Kendi risk bütçesi var; kötüyse tek satırla
# kapatılır, ana TREND stratejisini kirletmez.
MOMENTUM_ENABLED       = True
MOMENTUM_SHADOW_MODE   = True   # True → sinyal üretir + loglar ama İŞLEM AÇMAZ
MOMENTUM_RISK_SHARE    = 0.30   # günlük risk bütçesinin en fazla %30'unu kullanır
MOMENTUM_MIN_SCORE     = 70

# Momentum eşikleri — araştırma aralığı, optimum değer DEĞİL.
# Backtest ile karşılaştırılabilir parametreler olarak tutulur.
MOM_RET_1M_MIN         = 0.008   # %0.8
MOM_RET_3M_MIN         = 0.015   # %1.5
MOM_RET_5M_MIN         = 0.020   # %2.0
MOM_REL_VOLUME_MIN     = 3.0     # 3x ortalama hacim
MOM_TAKER_BUY_RATIO_MIN= 0.58    # alıcı baskısı %58
MOM_VOL_BASELINE_BARS  = 20      # relative volume için taban pencere

# Overextension / chase koruması — dev yeşil mumun tepesinden alma
MOM_MAX_EXT_ATR        = 4.0     # fiyat, kısa EMA'dan 4×ATR'den fazla uzaksa reddet
MOM_MAX_IMPULSE_PCT    = 0.18    # son 5 dakikada %18'den fazla çıktıysa pullback bekle
MOM_REQUIRE_PULLBACK   = True

# BTC yalnızca REJİM VETOSU — yön filtresi değil.
# Bağımsız güçlü altcoin momentumu BTC yatay/hafif düşüşteyken reddedilmemeli.
MOM_BTC_CRASH_PCT      = -0.025  # BTC 15dk'da %2.5+ düşerse yeni momentum işlemi yok

# ─── İndikatör parametreleri ─────────────────────────────────────────────────
RSI_PERIOD      = 14
RSI_OVERSOLD    = 33
RSI_OVERBOUGHT  = 67

EMA_FAST        = 8
EMA_SLOW        = 21
EMA_TREND       = 50

MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9

BB_PERIOD       = 20
BB_STD          = 2.0

VWAP_PERIOD     = 20

# ─── Paper Trading ────────────────────────────────────────────────────────────
PAPER_BALANCE   = 1000.0   # SADECE ilk çalıştırmada kullanılır; sonrası DB'den gelir

# ─── İzleme / heartbeat ───────────────────────────────────────────────────────
HEARTBEAT_ENABLED      = True
HEARTBEAT_INTERVAL_MIN = 60     # bu sıklıkta "yaşıyorum" özeti
STALE_ALERT_MINUTES    = 15     # sinyal akışı bu kadar durursa alarm
