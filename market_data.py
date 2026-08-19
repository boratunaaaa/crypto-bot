"""
Market data manager — multi-timeframe cache + WebSocket fiyat akışı.
"""

import logging
import time
import threading
import socket
from collections import defaultdict

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance import ThreadedWebsocketManager


def _patch_ws_fail_connection() -> bool:
    """python-binance ↔ websockets sürüm uyumsuzluğunu kapat.

    DENETİM BULGUSU (2026-08-16, canlı): python-binance 1.0.19 soketi
    kapatırken `self.ws.fail_connection()` çağırıyor. websockets ≥14'te bu
    metot `ClientConnection`'dan kaldırıldı, dolayısıyla her kapanışta
    `AttributeError` fırlıyor. Bu hata `__aexit__`'in ortasında oluştuğu için
    kütüphanenin KENDİ yeniden bağlanma mantığı hiç çalışmıyor: dinleyici
    görev ölüyor ve soket ancak bizim watchdog'umuz 60+ saniye sonra fark
    edince elle ayağa kalkıyordu. Canlıda soket ~2 dakikada bir ölüyor, aradaki
    boşlukta fiyatlar bayatlıyor ve hiçbir giriş yapılamıyordu.

    Kalıcı çözüm sürüm yükseltmesidir; bu yama ortama dokunmadan aynı
    davranışı geri kazandırır. Metot zaten varsa HİÇBİR ŞEY yapmaz, yani
    yükseltmeden sonra kendiliğinden devre dışı kalır.
    """
    try:
        from websockets.asyncio.client import ClientConnection
    except Exception:
        return False
    if hasattr(ClientConnection, "fail_connection"):
        return False

    def fail_connection(self, *_args, **_kwargs):
        # Senkron olmalı: python-binance bunu await ETMEDEN çağırıyor.
        transport = getattr(self, "transport", None)
        if transport is not None:
            try:
                transport.abort()
            except Exception:
                pass

    ClientConnection.fail_connection = fail_connection
    return True


_WS_PATCHED = _patch_ws_fail_connection()

from config import (
    API_KEY, API_SECRET, TESTNET,
    TF_ENTRY, TF_TREND, TF_MACRO, TF_CONTEXT, CANDLE_LIMIT,
    TOP_N_COINS, COIN_REFRESH_SEC, OHLCV_REFRESH_SEC, OHLCV_REFRESH_SEC_CTX,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    USE_ELITE_COINS, ELITE_COINS,
    MIN_QUOTE_VOLUME_24H, MAX_SPREAD_PCT,
    SPOT_MODE, MAX_PRICE_AGE_SEC,
)

logger = logging.getLogger("market_data")

# Momentum tarayıcısının kullandığı hızlı timeframe.
# Tüm evren için çekilmez — yalnızca hareket eden kısa liste için (bkz.
# refresh_fast_ohlcv). 250 sembol × 1m çekmek API kotasını gereksiz tüketirdi.
TF_FAST = "1m"


class MarketDataManager:

    def __init__(self):
        self._client = Client(API_KEY, API_SECRET, testnet=TESTNET)
        self._client_lock = threading.Lock()
        self._lock   = threading.RLock()

        self._prices:  dict[str, float] = {}
        self._price_ts: dict[str, float] = {}   # sembol başına son WS güncelleme zamanı
        # ohlcv[timeframe][symbol] = DataFrame
        self._ohlcv:   dict[str, dict[str, pd.DataFrame]] = {
            TF_ENTRY: {}, TF_TREND: {}, TF_MACRO: {}, TF_CONTEXT: {},
        }
        self._symbols:       list[str] = []
        self._funding:       dict[str, float] = {}
        self._open_interest: dict[str, float] = {}   # OI değeri (anlık)
        self._oi_prev:       dict[str, float] = {}   # önceki OI (değişim için)

        # Momentum tarayıcısı için: tüm piyasanın 24s istatistikleri tek çağrıda
        self._ticker:    dict[str, dict] = {}
        self._ticker_ts: float = 0.0
        self._fast_ohlcv: dict[str, pd.DataFrame] = {}   # 1m, yalnız kısa liste
        self._fast_ts:    dict[str, float] = {}
        self._spread:     dict[str, float] = {}

        self._twm:        ThreadedWebsocketManager | None = None
        # Açık pozisyonlara adanmış akışlar: sembol -> soket anahtarı.
        self._prio_streams: dict[str, str] = {}
        self._prio_lock = threading.Lock()
        self._running     = False
        self._last_ws_msg = time.time()
        # Mesaj akıyor ama hiçbiri fiyata dönüşmüyorsa bu bir FORMAT hatasıdır,
        # ağ hatası değil. İkisi ayrı ölçülmezse arıza sessiz kalır.
        self._last_price_update = time.time()
        self._fmt_alerted = False

        self._last_coin_refresh   = 0.0
        self._last_funding_refresh= 0.0
        self._last_ohlcv_refresh: dict[str, dict[str, float]] = {
            TF_ENTRY:   defaultdict(float),
            TF_TREND:   defaultdict(float),
            TF_MACRO:   defaultdict(float),
            TF_CONTEXT: defaultdict(float),
        }

    def _reconnect_client(self):
        """REST client'ı yeniden oluştur (IP değişiminden sonra)."""
        with self._client_lock:
            try:
                self._client = Client(API_KEY, API_SECRET, testnet=TESTNET)
                logger.info("Binance REST client yeniden bağlandı")
            except Exception as e:
                logger.error(f"REST client yeniden bağlantı hatası: {e}")

    # ── Coin listesi ─────────────────────────────────────────────────────────

    def refresh_symbols(self) -> list[str]:
        try:
            if USE_ELITE_COINS:
                # Elit listedeki coinlerin Futures'ta var olduğunu doğrula
                tickers = self._client.futures_ticker()
                available = {t["symbol"] for t in tickers}
                syms = [s for s in ELITE_COINS if s in available]
                with self._lock:
                    self._symbols = syms
                logger.info(f"Elit coin listesi: {len(syms)} sembol")
                return syms
            # Normal mod — likidite filtresi + hacme göre top N
            tickers = self._client.futures_ticker()
            usdt = [
                t for t in tickers
                if t["symbol"].endswith("USDT")
                and "_" not in t["symbol"]
                and not any(x in t["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR"])
            ]
            with self._lock:
                self._ticker = {t["symbol"]: t for t in usdt}
                self._ticker_ts = time.time()

            # Likidite filtresi: sıralama tavanı yerine gerçek hacim eşiği.
            # Amaç, henüz top-100 hacme girmemiş ama işlem yapılabilir coinleri
            # de görebilmek — pump'lar tam olarak orada başlıyor.
            liquid = [
                t for t in usdt
                if float(t.get("quoteVolume", 0)) >= MIN_QUOTE_VOLUME_24H
            ]
            liquid.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
            syms = [t["symbol"] for t in liquid[:TOP_N_COINS]]
            with self._lock:
                self._symbols = syms
            logger.info(
                f"Coin listesi güncellendi: {len(syms)} sembol "
                f"({len(usdt)} USDT perp, {len(liquid)} likidite filtresini geçti)"
            )
            return syms
        except Exception as e:
            logger.error(f"Coin listesi hatası: {e}")
            return self._symbols

    # ── Open Interest ─────────────────────────────────────────────────────────

    def refresh_open_interest(self):
        """Tüm sembollerin anlık Open Interest değerini güncelle."""
        try:
            with self._lock:
                symbols = list(self._symbols)
            for sym in symbols:
                try:
                    oi = self._client.futures_open_interest(symbol=sym)
                    val = float(oi.get("openInterest", 0))
                    with self._lock:
                        self._oi_prev[sym] = self._open_interest.get(sym, val)
                        self._open_interest[sym] = val
                    time.sleep(0.05)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"OI güncelleme hatası: {e}")

    def get_oi_trend(self, symbol: str) -> str:
        """OI değişim yönü: UP / DOWN / FLAT."""
        with self._lock:
            cur  = self._open_interest.get(symbol, 0)
            prev = self._oi_prev.get(symbol, cur)
        if prev <= 0:
            return "FLAT"
        change = (cur - prev) / prev
        if change > 0.02:
            return "UP"
        if change < -0.02:
            return "DOWN"
        return "FLAT"

    # ── Funding rate ─────────────────────────────────────────────────────────

    def refresh_funding(self):
        """Futures funding rate — negatifse short lehine, pozitifse long lehine."""
        try:
            rates = self._client.futures_funding_rate()  # type: ignore
            mapping = {}
            for r in rates:
                sym = r.get("symbol","")
                if sym.endswith("USDT"):
                    mapping[sym] = float(r.get("fundingRate", 0))
            with self._lock:
                self._funding = mapping
        except Exception:
            pass   # spot hesapta funding olmaz, hata normal

    # ── OHLCV ────────────────────────────────────────────────────────────────

    def _fetch_df(self, symbol: str, interval: str) -> pd.DataFrame | None:
        for attempt in range(2):
            try:
                klines = self._client.futures_klines(
                    symbol=symbol, interval=interval, limit=CANDLE_LIMIT
                )
                df = pd.DataFrame(klines, columns=[
                    "open_time","open","high","low","close","volume",
                    "close_time","quote_volume","trades",
                    "taker_buy_base","taker_buy_quote","ignore",
                ])
                for col in ["open","high","low","close","volume"]:
                    df[col] = df[col].astype(float)
                df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
                df.set_index("open_time", inplace=True)
                return df[["open","high","low","close","volume"]]
            except Exception as e:
                err = str(e)
                if attempt == 0 and any(x in err for x in [
                    "Invalid API-key", "-2015", "nodename", "NameResolution",
                    "ConnectionReset", "Connection aborted", "timed out",
                ]):
                    logger.warning(f"Bağlantı hatası, client yeniden oluşturuluyor: {e}")
                    self._reconnect_client()
                    time.sleep(2)
                else:
                    logger.debug(f"OHLCV hatası {symbol} {interval}: {e}")
                    return None
        return None

    def refresh_ohlcv(self, symbol: str, interval: str) -> bool:
        df = self._fetch_df(symbol, interval)
        if df is None:
            return False
        with self._lock:
            self._ohlcv[interval][symbol] = df
            self._last_ohlcv_refresh[interval][symbol] = time.time()
        return True

    def _ohlcv_loop(self):
        """Tüm timeframe'leri döngüsel yenile."""
        while self._running:
            with self._lock:
                symbols = list(self._symbols)
            now = time.time()
            for sym in symbols:
                if not self._running:
                    break
                # 5m/15m/1h — kısa periyotlu yenileme
                for tf in [TF_ENTRY, TF_TREND, TF_MACRO]:
                    age = now - self._last_ohlcv_refresh[tf].get(sym, 0)
                    if age >= OHLCV_REFRESH_SEC:
                        self.refresh_ohlcv(sym, tf)
                        time.sleep(0.04)
                # 4h — uzun periyotlu yenileme (daha az API isteği)
                age_ctx = now - self._last_ohlcv_refresh[TF_CONTEXT].get(sym, 0)
                if age_ctx >= OHLCV_REFRESH_SEC_CTX:
                    self.refresh_ohlcv(sym, TF_CONTEXT)
                    time.sleep(0.04)
            time.sleep(1)

    def _coin_loop(self):
        _last_oi_refresh = 0.0
        while self._running:
            now = time.time()
            if now - self._last_coin_refresh >= COIN_REFRESH_SEC:
                self.refresh_symbols()
                self._last_coin_refresh = now
            if now - self._last_funding_refresh >= 300:
                self.refresh_funding()
                self._last_funding_refresh = now
            if now - _last_oi_refresh >= 300:   # 5 dakikada bir OI güncelle
                self.refresh_open_interest()
                _last_oi_refresh = now
            time.sleep(30)

    # ── WebSocket ────────────────────────────────────────────────────────────

    @staticmethod
    def _internet_ok(host="api.binance.com", port=443, timeout=5) -> bool:
        """Binance'e TCP bağlantısı mümkün mü kontrol et."""
        try:
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except Exception:
            return False

    def _stop_twm(self):
        # Soket anahtarları eski yöneticiye aitti; yenisinde geçersizdir.
        with self._prio_lock:
            self._prio_streams.clear()
        """Mevcut TWM'i güvenle kapat."""
        twm = self._twm
        self._twm = None
        if twm is None:
            return
        try:
            twm.stop()
        except Exception:
            pass
        time.sleep(1)

    def _ingest_ticker(self, item, now: float) -> int:
        """Tek bir ticker kaydını işle. Dönen değer: güncellenen sembol sayısı.

        İki format da desteklenir:
          • miniTicker / 24h ticker → `c` (son fiyat)
          • bookTicker              → `b`/`a` (alış/satış), orta fiyat alınır
        Orta fiyat giriş kararı için son işlem fiyatından daha dürüsttür:
        gerçekte alınabilecek fiyata daha yakındır.

        Çağıran `self._lock`'u tutuyor olmalı.
        """
        if not isinstance(item, dict):
            return 0
        sym = item.get("s")
        if not sym:
            return 0

        price = item.get("c")
        if price is None:
            bid, ask = item.get("b"), item.get("a")
            if bid is not None and ask is not None:
                try:
                    b, a = float(bid), float(ask)
                except (TypeError, ValueError):
                    return 0
                if b <= 0 or a <= 0:
                    return 0
                price = (b + a) / 2.0
                self._spread[sym] = (a - b) / price if price else 0.0
        if price is None:
            return 0

        try:
            px = float(price)
        except (TypeError, ValueError):
            return 0
        if px <= 0:
            return 0

        self._prices[sym] = px
        self._price_ts[sym] = now
        return 1

    def _start_ws(self):
        self._stop_twm()
        try:
            twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
            twm.start()
            time.sleep(1)

            def on_ticker(msg):
                # DENETİM BULGUSU (2026-08-16, canlı): burada yalnız
                # `isinstance(msg, list)` işleniyordu ve yalnız `c` alanı
                # okunuyordu. Futures akışında ÜÇ varsayım da yanlış:
                #   1. Mesaj liste değil, {'stream':..., 'data':...} sarmalı,
                #   2. `data` bir ticker DİZİSİ değil, TEK bir kayıt,
                #   3. python-binance'in `start_all_ticker_futures_socket`'i
                #      aslında `!bookTicker`'a abone olur — orada `c` (son
                #      fiyat) YOKTUR, `b`/`a` (alış/satış) vardır.
                # Sonuç: futures modunda `_prices` HİÇ dolmadı. Yeni eklenen
                # tazelik kontrolüyle birleşince her giriş "Fiyat bayat"
                # gerekçesiyle reddedildi ve bot hiç işlem açamadı.
                now = time.time()
                # Bağlantı sağlığı: mesaj GELDİYSE hat ayakta. Bunu ayrıştırma
                # başarısına bağlamak, format değişiminde watchdog'u sonsuz ve
                # faydasız bir yeniden bağlanma döngüsüne sokuyordu.
                self._last_ws_msg = now

                payload = msg
                if isinstance(payload, dict) and "data" in payload:
                    payload = payload["data"]
                items = payload if isinstance(payload, list) else [payload]

                updated = 0
                with self._lock:
                    for item in items:
                        updated += self._ingest_ticker(item, now)
                if updated:
                    self._last_price_update = now

            # DENETİM BULGUSU (kritik): burada `start_miniticker_socket`
            # kullanılıyordu — o python-binance'te SPOT akışıdır. Futures botu
            # göstergelerini Futures mumlarından, anlık giriş/stop kararlarını
            # ise Spot fiyatından üretiyordu. Futures-only coinlerde fiyat hiç
            # gelmiyor, gelenlerde basis farkı yanlış giriş/çıkış üretiyordu.
            # Paper/canlı pariteyi de doğrudan bozuyordu.
            if SPOT_MODE:
                twm.start_miniticker_socket(callback=on_ticker)
                stream = "spot miniTicker"
            else:
                twm.start_all_ticker_futures_socket(callback=on_ticker)
                stream = "futures ticker"
            self._twm = twm
            # WS yeniden başladığında adanmış akışlar da yeniden kurulmalı;
            # yoksa restart sonrası açık pozisyonlar sessizce toplu akışa
            # (yani sorunun kaynağına) geri düşer.
            with self._prio_lock:
                onceki = list(self._prio_streams)
                self._prio_streams.clear()
            if onceki:
                self.set_priority_symbols(onceki)
            self._last_ws_msg = time.time()
            self._last_price_update = time.time()
            self._fmt_alerted = False
            patched = " [ws uyumluluk yaması etkin]" if _WS_PATCHED else ""
            logger.info(f"WebSocket başlatıldı ({stream}){patched}")
        except Exception as e:
            logger.error(f"WebSocket başlatma hatası: {e}")
            self._twm = None

    # ── Öncelikli semboller: açık pozisyonlara ADANMIŞ akış ──────────────────

    def set_priority_symbols(self, symbols) -> None:
        """Açık pozisyonların sembollerine adanmış WS akışı aç/kapat.

        NEDEN: toplu akış (`!bookTicker`) bazı sembolleri sessizce düşürüyor.
        Ölçüldü: bir sembolün fiyatı 120 saniye boyunca güncellenmedi ve stop
        kontrolü o bayat fiyatla çalıştı. REST yedeği eklenince en kötü hâl
        120s'den 20s'ye indi ama SORUN BİTMEDİ: `INITIAL_STOP` ortalaması
        sıfırlamadan bu yana hâlâ **-1.28R** (hedef -1.0R). Yani her stop
        planlanandan ~%28 pahalıya kapanıyor.

        Sembole adanmış akış bu boşluğu kapatır: tek sembollük abonelik toplu
        akışın seçiciliğine tabi değildir ve saniyenin altında güncellenir.
        Yalnız AÇIK POZİSYONLAR için açılır — evrenin tamamına adanmış akış
        açmak ne gerekli ne mümkün.

        REST yedeği KALDIRILMADI: adanmış akış da düşebilir, ve düşerse yine
        20 saniyede REST devreye girer. Katmanlar birbirinin yedeği.
        """
        istenen = {s.upper() for s in (symbols or [])}
        with self._prio_lock:
            mevcut = set(self._prio_streams)
            if istenen == mevcut:
                return
            twm = self._twm
            # Fazlalıkları kapat
            for sym in mevcut - istenen:
                anahtar = self._prio_streams.pop(sym, None)
                if twm and anahtar:
                    try:
                        twm.stop_socket(anahtar)
                    except Exception:
                        # Kapatılamayan soket sızıntıdır ama pozisyonu
                        # yönetimsiz bırakmaktan iyidir; sessizce geçme, logla.
                        logger.debug(f"{sym}: adanmış akış kapatılamadı")
            # Eksikleri aç
            if twm is None:
                return
            for sym in istenen - set(self._prio_streams):
                try:
                    anahtar = twm.start_symbol_ticker_futures_socket(
                        callback=self._on_priority_msg, symbol=sym
                    )
                    self._prio_streams[sym] = anahtar
                    logger.info(f"{sym}: adanmış fiyat akışı açıldı")
                except Exception as exc:
                    # Açılamazsa sessizce vazgeçme: bu sembol yine toplu akış
                    # ve REST yedeğiyle yürür, ama bunu bilmek gerekir.
                    logger.warning(f"{sym}: adanmış akış açılamadı ({exc})")

    def _on_priority_msg(self, msg) -> None:
        """Adanmış akıştan gelen fiyat. Toplu akışla AYNI ayrıştırıcı."""
        now = time.time()
        self._last_ws_msg = now
        payload = msg
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        items = payload if isinstance(payload, list) else [payload]
        updated = 0
        with self._lock:
            for item in items:
                updated += self._ingest_ticker(item, now)
        if updated:
            self._last_price_update = now

    def _ws_watchdog(self):
        """Ağ kesintilerinde WebSocket'i otomatik yeniden başlat."""
        while self._running:
            time.sleep(15)
            if not self._running:
                break
            age = time.time() - self._last_ws_msg

            # Hat ayakta ama fiyat üretmiyorsa sorun ağ DEĞİL, ayrıştırmadır.
            # Yeniden bağlanmak bunu çözmez; tam olarak bu ayrımı yapmadığımız
            # için bot saatlerce boşuna reconnect döngüsünde kaldı ve tek işlem
            # açmadı. Bu durum ayrı ve YÜKSEK sesle raporlanır.
            # 60s'ti. Kütüphane sürüm uyumsuzluğu yüzünden akış dakikalar
            # içinde kesilebiliyor; eşiği düşürmek ölü pencereyi kısaltır.
            # Yanlış pozitif maliyeti düşük (yeniden bağlanmak ucuz), kaçırılan
            # kesintinin maliyeti yüksek (giriş yapılamıyor).
            if age <= 30:
                stale = time.time() - self._last_price_update
                if stale > 120 and not self._fmt_alerted:
                    logger.error(
                        f"WebSocket mesaj gönderiyor ama {stale:.0f}s'dir hiçbiri "
                        f"fiyata dönüşmedi — akış formatı değişmiş olabilir. "
                        f"Giriş kararları bayat fiyat yüzünden durur."
                    )
                    self._fmt_alerted = True
                elif stale <= 120:
                    self._fmt_alerted = False
                continue

            logger.warning(f"WebSocket {age:.0f}s sessiz — ağ kontrol ediliyor...")

            # Ağ geri gelene kadar bekle (maks 5 dakika)
            waited = 0
            while self._running and not self._internet_ok():
                if waited == 0:
                    logger.warning("İnternet bağlantısı yok, bekleniyor...")
                time.sleep(10)
                waited += 10
                if waited >= 300:
                    logger.error("5 dakika boyunca internet yok, yeniden denenecek...")
                    break

            if not self._running:
                break

            logger.info("Ağ hazır — WebSocket yeniden başlatılıyor")
            self._start_ws()

    # ── Başlat / Durdur ───────────────────────────────────────────────────────

    def get_btc_trend(self) -> str:
        """BTC 1h trendini döndür — altcoin filtresi için."""
        try:
            import pandas_ta as ta
            df = self.get_ohlcv("BTCUSDT", TF_MACRO)
            if df is None or len(df) < EMA_TREND + 5:
                return "SIDEWAYS"
            c  = df["close"]
            ef = ta.ema(c, length=EMA_FAST)
            es = ta.ema(c, length=EMA_SLOW)
            et = ta.ema(c, length=EMA_TREND)
            if ef is None or es is None or et is None:
                return "SIDEWAYS"
            if float(ef.iloc[-1]) > float(es.iloc[-1]) > float(et.iloc[-1]):
                return "UP"
            if float(ef.iloc[-1]) < float(es.iloc[-1]) < float(et.iloc[-1]):
                return "DOWN"
        except Exception:
            pass
        return "SIDEWAYS"

    def start(self):
        self._running = True
        self.refresh_symbols()
        self.refresh_funding()

        # İlk yükleme: ilk 30 coini anında yükle (4h dahil ilk 10)
        logger.info("İlk veri yükleniyor (30 coin × 4 timeframe)...")
        for sym in self._symbols[:10]:
            for tf in [TF_ENTRY, TF_TREND, TF_MACRO, TF_CONTEXT]:
                self.refresh_ohlcv(sym, tf)
                time.sleep(0.04)
        for sym in self._symbols[10:30]:
            for tf in [TF_ENTRY, TF_TREND, TF_MACRO]:
                self.refresh_ohlcv(sym, tf)
                time.sleep(0.04)

        threading.Thread(target=self._ohlcv_loop,  daemon=True).start()
        threading.Thread(target=self._coin_loop,   daemon=True).start()
        threading.Thread(target=self._ws_watchdog, daemon=True).start()
        self._start_ws()
        logger.info("MarketDataManager hazır")

    def stop(self):
        self._running = False
        if self._twm:
            try:
                self._twm.stop()
            except Exception:
                pass

    # ── Momentum tarayıcısı verisi ───────────────────────────────────────────
    #
    # Tasarım: tüm evren için 1m mum çekmek API kotasını tüketir (250 sembol ×
    # 2 ağırlık = her dakika 500 ağırlık, sadece bu iş için). Bunun yerine iki
    # aşama: (1) `futures_ticker()` TEK çağrıyla tüm piyasanın 24s hareketini
    # verir, buradan hareket eden kısa liste çıkarılır; (2) 1m mumlar yalnız o
    # kısa liste için çekilir. Böylece hem tüm piyasa görülür hem kota korunur.

    def refresh_ticker(self) -> None:
        try:
            tickers = self._client.futures_ticker()
            with self._lock:
                self._ticker = {
                    t["symbol"]: t for t in tickers
                    if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                }
                self._ticker_ts = time.time()
        except Exception as e:
            logger.debug(f"Ticker güncelleme hatası: {e}")

    def get_movers(self, n: int = 25, min_change_pct: float = 0.02) -> list[str]:
        """
        24 saatlik değişime göre en çok hareket eden likit semboller.

        Bu, momentum tarayıcısının kısa listesidir — pump başlamadan önce
        hacim sıralamasında henüz üste çıkmamış coinleri de kapsar.
        """
        with self._lock:
            snapshot = list(self._ticker.values())
        if not snapshot:
            return []
        out = []
        for t in snapshot:
            try:
                qv = float(t.get("quoteVolume", 0))
                if qv < MIN_QUOTE_VOLUME_24H:
                    continue
                chg = abs(float(t.get("priceChangePercent", 0))) / 100.0
                if chg < min_change_pct:
                    continue
                out.append((t["symbol"], chg))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in out[:n]]

    def _fetch_fast_df(self, symbol: str) -> pd.DataFrame | None:
        """1m mumlar — taker alım hacmi ve işlem sayısı dahil."""
        try:
            klines = self._client.futures_klines(symbol=symbol, interval=TF_FAST, limit=60)
            df = pd.DataFrame(klines, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","quote_volume","trades",
                "taker_buy_base","taker_buy_quote","ignore",
            ])
            for col in ["open","high","low","close","volume",
                        "quote_volume","taker_buy_base","taker_buy_quote"]:
                df[col] = df[col].astype(float)
            df["trades"] = df["trades"].astype(int)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df[["open","high","low","close","volume",
                       "quote_volume","trades","taker_buy_base","taker_buy_quote"]]
        except Exception as e:
            logger.debug(f"Fast OHLCV hatası {symbol}: {e}")
            return None

    def refresh_fast_ohlcv(self, symbols: list[str], max_age: float = 45.0) -> int:
        """Kısa liste için 1m mumları tazele. Dönüş: güncellenen sembol sayısı."""
        now = time.time()
        updated = 0
        for sym in symbols:
            if now - self._fast_ts.get(sym, 0) < max_age:
                continue
            df = self._fetch_fast_df(sym)
            if df is not None and not df.empty:
                with self._lock:
                    self._fast_ohlcv[sym] = df
                    self._fast_ts[sym] = now
                updated += 1
            time.sleep(0.05)
        return updated

    def get_fast_df(self, symbol: str, max_age: float = 180.0) -> pd.DataFrame | None:
        """1m veri — bayatsa None döner (staleness kontrolü zorunlu)."""
        with self._lock:
            ts = self._fast_ts.get(symbol, 0)
            if time.time() - ts > max_age:
                return None
            df = self._fast_ohlcv.get(symbol)
            return df.copy() if df is not None else None

    def get_spread_pct(self, symbol: str) -> float | None:
        """Bid/ask spread yüzdesi. Bilinmiyorsa None (filtre 'bilinmiyor'u geçirir)."""
        with self._lock:
            cached = self._spread.get(symbol)
        if cached is not None:
            return cached
        try:
            ob = self._client.futures_orderbook_ticker(symbol=symbol)
            bid = float(ob["bidPrice"]); ask = float(ob["askPrice"])
            if bid <= 0 or ask <= 0:
                return None
            spread = (ask - bid) / ((ask + bid) / 2)
            with self._lock:
                self._spread[symbol] = spread
            return spread
        except Exception:
            return None

    def clear_spread_cache(self):
        with self._lock:
            self._spread.clear()

    def get_oi_change_pct(self, symbol: str) -> float:
        """Open interest değişim yüzdesi (son iki ölçüm arası)."""
        with self._lock:
            cur  = self._open_interest.get(symbol, 0.0)
            prev = self._oi_prev.get(symbol, 0.0)
        if prev <= 0:
            return 0.0
        return (cur - prev) / prev

    def get_btc_change_15m(self) -> float:
        """BTC'nin son 15 dakikalık değişimi — momentum rejim vetosu için."""
        df = self.get_ohlcv("BTCUSDT", TF_TREND)
        if df is None or len(df) < 2:
            return 0.0
        try:
            return (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]
        except Exception:
            return 0.0

    # ── Erişim (thread-safe) ─────────────────────────────────────────────────

    def get_price(self, symbol: str, max_age: float | None = None) -> float | None:
        """
        Son fiyat. Bayatsa None döner.

        DENETİM BULGUSU: tazelik hiç kontrol edilmiyordu. WebSocket sessizce
        düşerse `_prices` sözlüğünde saatler önceki fiyat durur ve yeni pozisyon
        o fiyatla açılabilirdi. Açık pozisyonlar için main.py zaten taze fiyat
        çekiyordu (2026-08-07 WS olayı), ama GİRİŞ tarafında böyle bir koruma
        yoktu — yani en pahalı yönde açıktı.
        """
        limit = MAX_PRICE_AGE_SEC if max_age is None else max_age
        with self._lock:
            px = self._prices.get(symbol)
            if px is None:
                return None
            ts = self._price_ts.get(symbol, 0.0)
            if limit and (time.time() - ts) > limit:
                return None
            return px

    def price_age(self, symbol: str) -> float:
        with self._lock:
            ts = self._price_ts.get(symbol)
        return float("inf") if not ts else time.time() - ts

    def get_ohlcv(self, symbol: str, tf: str = TF_ENTRY) -> pd.DataFrame | None:
        with self._lock:
            df = self._ohlcv.get(tf, {}).get(symbol)
            return df.copy() if df is not None else None

    def get_funding(self, symbol: str) -> float:
        with self._lock:
            return self._funding.get(symbol, 0.0)

    def get_open_interest(self, symbol: str) -> float:
        with self._lock:
            return self._open_interest.get(symbol, 0.0)

    def get_symbols(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_prices_for(
        self, symbols: list[str], max_age_seconds: float = MAX_PRICE_AGE_SEC
    ) -> dict[str, float]:
        """Fiyatlar, açık pozisyon SL/TP kontrolü gibi güvenlik-kritik kullanım
        için: ``symbols`` içindeki her sembol için WS mini-ticker'dan gelen
        fiyat ``max_age_seconds``'tan taze değilse (veya hiç yoksa), REST
        üzerinden doğrudan tazelenir.

        2026-08-07 gerçek olay: `!miniTicker@arr` akışı genel olarak canlı
        kalırken (watchdog yalnızca akışın TAMAMEN sessiz kaldığı durumu
        yakalıyor), iki farklı sembol için fiyat saatlerce güncellenmeden
        kaldı — o pozisyonlar gerçek fiyat çoktan stop-loss'u geçmişken hiç
        kapanmadı, bot 4 hayalet pozisyonla "dolu" kalıp yeni işlem açmayı
        durdurdu. Kök neden kesin olarak WS kütüphanesinin belirli sembolleri
        sessizce düşürmesiyle açıklanabiliyor; bu yöntem nedeni ne olursa
        olsun aynı sonucu (sessiz, kalıcı fiyat donması) engeller.

        2026-08-18: varsayılan eşik 90 saniyeydi ve stop kontrolü tam da bu
        varsayılanı kullanıyordu — yani 89 saniyelik bir fiyat "taze" sayılıp
        stop kararına giriyordu. Ölçüm: `INITIAL_STOP` ile kapanan **32
        işlemin 32'si de** planlanan -1R'yi aştı; plan dışı fazla zarar
        119.50$. Giriş yolu ise zaten sıkı olan `MAX_PRICE_AGE_SEC`'i (20s)
        açıkça geçiriyordu: güvenlik-kritik olan taraf gevşek, olmayan taraf
        sıkıydı. Varsayılan artık aynı sabit; gevşetmek isteyen çağıranın
        bunu BİLEREK yazması gerekir."""
        now = time.time()
        with self._lock:
            fresh = {
                sym: self._prices[sym]
                for sym in symbols
                if sym in self._prices and (now - self._price_ts.get(sym, 0)) <= max_age_seconds
            }
        stale = [s for s in symbols if s not in fresh]
        for sym in stale:
            price = self._fetch_price_rest(sym)
            if price is None:
                continue
            age = now - self._price_ts.get(sym, 0) if sym in self._price_ts else None
            logger.warning(
                f"Fiyat REST fallback: {sym} WS'ten bayat/eksikti "
                f"(yaş={'yok' if age is None else f'{age:.0f}s'}), REST'ten tazelendi ({price})"
            )
            with self._lock:
                self._prices[sym] = price
                self._price_ts[sym] = time.time()
            fresh[sym] = price
        return fresh

    def _fetch_price_rest(self, symbol: str) -> float | None:
        try:
            with self._client_lock:
                data = self._client.futures_symbol_ticker(symbol=symbol)
            return float(data["price"])
        except Exception as e:
            logger.error(f"REST fiyat çekme hatası {symbol}: {e}")
            return None

    def ready_symbols(self) -> list[str]:
        """En az entry timeframe'i hazır olan semboller."""
        with self._lock:
            return [s for s in self._symbols if s in self._ohlcv[TF_ENTRY]]
