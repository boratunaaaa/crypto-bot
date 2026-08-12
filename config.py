"""
Maksimum kazanç için optimize edilmiş ayarlar.
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
PAPER_TRADING = True   # ← 1 hafta paper test — gerçek para yok
SPOT_MODE     = False  # Futures modu — kaldıraç + long/short

# ─── Timeframe (multi-timeframe 4 katman) ────────────────────────────────────
TF_ENTRY   = "5m"   # giriş sinyali (1m'den daha az gürültülü)
TF_TREND   = "15m"  # kısa vadeli trend
TF_MACRO   = "1h"   # orta vadeli yön
TF_CONTEXT = "4h"   # büyük resim — en ağırlıklı filtre
CANDLE_LIMIT = 120

# ─── Elit Coin Listesi ────────────────────────────────────────────────────────
# False yapıldı (2026-07-26): dar bir listeye bakan bot, listede olmayan
# hareketleri göremez. EUL/SHIB gibi sert yükselişler tam olarak bu yüzden
# kaçırıldı — bot onları hiç izlemiyordu. Artık hacme göre en likit TOP_N_COINS
# taranıyor; likidite filtresi, kimsenin alıp satamadığı çöp coinleri eler.
USE_ELITE_COINS = False  # True → sadece aşağıdaki coinleri izle
ELITE_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "NEARUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "SUIUSDT",
    "TRXUSDT", "OPUSDT", "ARBUSDT", "PEPEUSDT", "APTUSDT",
]
TOP_N_COINS              = 100    # USE_ELITE_COINS=False ise kullanılır
COIN_REFRESH_SEC         = 3600
OHLCV_REFRESH_SEC        = 60
OHLCV_REFRESH_SEC_CTX    = 900
SIGNAL_INTERVAL          = 30

# ─── Piyasa Rejimi (ADX) ──────────────────────────────────────────────────────
ADX_PERIOD           = 14
ADX_TREND_THRESHOLD  = 20   # ADX > 20 → trend market → breakout sinyaller güvenilir
ADX_RANGE_THRESHOLD  = 15   # ADX 15-25 → range → RSI/BB sinyaller
# ADX < 15 → kaotik → işlem açma

# ─── Dinamik Risk ─────────────────────────────────────────────────────────────
DYNAMIC_RISK_ENABLED   = True
RISK_MIN_PCT           = 0.01   # minimum %1 risk
RISK_MAX_PCT           = 0.03   # maximum %3 risk
DYNAMIC_RISK_LOOKBACK  = 10     # son N işlem baz alınır

# ─── Risk / Pozisyon ──────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT     = 0.02   # işlem başına bakiyenin max %2'si risk (kural #1)
MAX_POSITION_PCT       = 0.15   # tek işlem bakiyenin max %15'i (üst tavan)
# %30 idi. Slot sayısı 2→4 çıkarıldığı için tek pozisyon payı yarıya indirildi:
# toplam piyasa maruziyeti aynı kalıyor, sadece daha çok parçaya bölünüyor.
MAX_CAPITAL_RISK_PCT   = 0.50   # toplam açık risk tavanı
# TP sonrası trailing eklendiğinde stratejinin doğası değişti: artık "çok sayıda
# küçük zarar, az sayıda çok büyük kazanç" modeli. Bu modelde getiriyi birkaç
# büyük kazanan taşır, dolayısıyla **kaç deneme yapabildiğin** belirleyicidir.
# 1 long slotuyla aynı anda tek bilet alınıyordu: EUL koşarken slot vasat bir
# işlemle doluysa hareket tamamen kaçırılıyordu — 100 coin taramanın anlamı da
# kalmıyordu. Slot sayısı artırıldı, tek pozisyon payı (MAX_POSITION_PCT)
# yarıya indirilerek toplam maruziyet sabit tutuldu.
MAX_OPEN_POSITIONS        = 4
MAX_CORRELATED_POSITIONS  = 3   # aynı yönde en fazla 3 (kriptoda her şey BTC ile korele)
MIN_MARGIN_USDT           = 5.0    # minimum 5 USDT işlem

# ─── Kill Switch — günlük/haftalık zarar limitleri ───────────────────────────
DAILY_LOSS_LIMIT_PCT   = 0.03   # günde max %3 zarar → bot o gün durur
WEEKLY_LOSS_LIMIT_PCT  = 0.07   # haftada max %7 zarar → bot o hafta durur
MAX_CONSECUTIVE_LOSSES = 3      # üst üste 3 zarar → bot durur

# Kill switch KALICI OLMAMALI. Sayaç yalnızca kârlı bir işlemle sıfırlanıyordu,
# ama bot durunca yeni işlem açamıyor → kârlı işlem hiç olamıyor → bot bir daha
# asla açılmıyordu. 2026-07-26'da gerçekten böyle oldu: 15:14'te durdu ve saatlerce
# "durdum" yazmaktan başka bir şey yapmadı. Soğuma süresi bu kilidi kırar.
KILL_SWITCH_COOLDOWN_HOURS = 4
MAX_DAILY_TRADES       = 10     # günde en fazla 10 işlem

# ─── Komisyon simülasyonu (paper için gerçekçilik) ───────────────────────────
COMMISSION_RATE = 0.0005   # %0.05 taker fee (her açma/kapamada)

LEV_WEAK    = 2
LEV_MEDIUM  = 3
LEV_STRONG  = 5
MARGIN_MODE = "ISOLATED"

LIQ_SAFETY_BUFFER = 0.50

# ─── ATR tabanlı dinamik SL/TP ────────────────────────────────────────────────
ATR_PERIOD      = 14
SL_ATR_MULT     = 2.0   # SL = giriş ± 2.0×ATR
# 1.5 idi: gözlenen ilk 4 işlemin DÖRDÜ de stop-loss ile kapandı. Stop, normal
# piyasa gürültüsünün içinde kalıyordu. Pozisyon boyutu riske göre hesaplandığı
# için daha geniş stop = daha küçük pozisyon; işlem başına $ riski değişmez,
# sadece gürültüye erken kurban gitme ihtimali düşer.
TP_ATR_MULT     = 3.5   # TP = giriş ± 3.5×ATR  → 2.3:1 risk/ödül
MAX_SL_PCT      = 0.04  # SL hiçbir zaman %4'ü geçmesin
MAX_TP_PCT      = 0.12  # TP hiçbir zaman %12'yi geçmesin

# ─── Kazananı koştur ──────────────────────────────────────────────────────────
# Eski davranış: fiyat TP'ye değince pozisyon kapanıyordu. Yani %66'lık bir
# hareket +%1.2'de kesiliyordu (2026-07-26 AVAX işlemi tam olarak böyleydi).
# Yeni davranış: TP'ye ulaşınca kapatma; stop'u fiyatın arkasına takıp bırak.
# Böylece büyük hareketler sonuna kadar taşınır, dönüş başlayınca çıkılır.
# Bunun bedeli var ve bilinçli: TP'de kesin kâr almak yerine, geri dönüşte
# kârın bir kısmı geri verilir. Küçük hareketlerde eski davranış daha iyiydi;
# büyük hareketleri yakalamanın karşılığı bu.
TRAIL_AFTER_TP        = True
TRAIL_ATR_MULT        = 2.5   # TP sonrası stop, en yüksek fiyatın 2.5×ATR gerisi
TRAIL_MIN_LOCK_PCT    = 0.01  # TP'ye ulaşıldığında en az %1 kâr kilitlenir

# ─── Volatilite filtresi ──────────────────────────────────────────────────────
MIN_ATR_PCT     = 0.003   # %0.3 — çok durgun coinleri atla
MAX_ATR_PCT     = 0.06    # %6  — aşırı volatil coinleri atla

# ─── Sinyal eşikleri — 0-100 puan sistemi ────────────────────────────────────
MIN_SIGNAL_SCORE = 60     # 100 üzerinden en az 60 puan
MIN_CONFIDENCE   = 0.60   # = 60/100

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
PAPER_BALANCE   = 1000.0
