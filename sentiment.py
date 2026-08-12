"""
Piyasa duygu göstergeleri — Fear & Greed Index.
"""

import json
import logging
import time
import threading
import urllib.request

logger = logging.getLogger("sentiment")

_CACHE_TTL = 3600   # 1 saat


class SentimentTracker:
    """Fear & Greed Index'i periyodik olarak çeker ve cache'ler."""

    def __init__(self):
        self._fear_greed_value = 50       # 0=Extreme Fear, 100=Extreme Greed
        self._fear_greed_label = "Neutral"
        self._last_fetch = 0.0
        self._lock = threading.Lock()

    def refresh(self):
        try:
            req = urllib.request.Request(
                "https://api.alternative.me/fng/?limit=1",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            resp = urllib.request.urlopen(req, timeout=8)
            data = json.loads(resp.read())
            val  = int(data["data"][0]["value"])
            lbl  = data["data"][0]["value_classification"]
            with self._lock:
                self._fear_greed_value = val
                self._fear_greed_label = lbl
                self._last_fetch = time.time()
            logger.info(f"Fear & Greed: {val} ({lbl})")
        except Exception as e:
            logger.debug(f"Fear & Greed çekilemedi: {e}")

    def _auto_refresh(self):
        while True:
            if time.time() - self._last_fetch >= _CACHE_TTL:
                self.refresh()
            time.sleep(300)

    def start(self):
        self.refresh()
        t = threading.Thread(target=self._auto_refresh, daemon=True)
        t.start()

    @property
    def value(self) -> int:
        with self._lock:
            return self._fear_greed_value

    @property
    def label(self) -> str:
        with self._lock:
            return self._fear_greed_label

    def trade_signal(self) -> str:
        """
        Ticari yorum:
        - Extreme Fear (<25)  → kontrendli alım fırsatı (piyasa aşırı kötümser)
        - Fear (25-45)        → dikkatli, long sinyallere öncelik ver
        - Neutral (45-55)     → yansız
        - Greed (55-75)       → dikkatli, short sinyallere öncelik ver
        - Extreme Greed (>75) → risk yüksek, yeni long açma
        """
        v = self.value
        if v < 25:
            return "EXTREME_FEAR"
        if v < 45:
            return "FEAR"
        if v < 55:
            return "NEUTRAL"
        if v < 75:
            return "GREED"
        return "EXTREME_GREED"

    def is_safe_to_trade(self) -> bool:
        """Extreme Greed'de yeni long, Extreme Fear'de yeni short açma."""
        return self.value not in range(0, 10)   # 0-9 arası çok tehlikeli
