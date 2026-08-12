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

from config import (
    API_KEY, API_SECRET, TESTNET,
    TF_ENTRY, TF_TREND, TF_MACRO, TF_CONTEXT, CANDLE_LIMIT,
    TOP_N_COINS, COIN_REFRESH_SEC, OHLCV_REFRESH_SEC, OHLCV_REFRESH_SEC_CTX,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    USE_ELITE_COINS, ELITE_COINS,
)

logger = logging.getLogger("market_data")


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

        self._twm:        ThreadedWebsocketManager | None = None
        self._running     = False
        self._last_ws_msg = time.time()

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
            # Normal mod — hacme göre top N
            tickers = self._client.futures_ticker()
            usdt = [
                t for t in tickers
                if t["symbol"].endswith("USDT")
                and "_" not in t["symbol"]
                and not any(x in t["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR"])
            ]
            usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
            syms = [t["symbol"] for t in usdt[:TOP_N_COINS]]
            with self._lock:
                self._symbols = syms
            logger.info(f"Coin listesi güncellendi: {len(syms)} sembol (Futures)")
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

    def _start_ws(self):
        self._stop_twm()
        try:
            twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
            twm.start()
            time.sleep(1)

            def on_ticker(msg):
                if isinstance(msg, list):
                    now = time.time()
                    self._last_ws_msg = now
                    with self._lock:
                        for item in msg:
                            sym = item.get("s")
                            price = item.get("c")
                            if sym and price:
                                self._prices[sym] = float(price)
                                self._price_ts[sym] = now

            twm.start_miniticker_socket(callback=on_ticker)
            self._twm = twm
            self._last_ws_msg = time.time()
            logger.info("WebSocket başlatıldı")
        except Exception as e:
            logger.error(f"WebSocket başlatma hatası: {e}")
            self._twm = None

    def _ws_watchdog(self):
        """Ağ kesintilerinde WebSocket'i otomatik yeniden başlat."""
        while self._running:
            time.sleep(15)
            if not self._running:
                break
            age = time.time() - self._last_ws_msg
            if age <= 60:
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

    # ── Erişim (thread-safe) ─────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float | None:
        with self._lock:
            return self._prices.get(symbol)

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

    def get_prices_for(self, symbols: list[str], max_age_seconds: float = 90) -> dict[str, float]:
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
        olsun aynı sonucu (sessiz, kalıcı fiyat donması) engeller."""
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
