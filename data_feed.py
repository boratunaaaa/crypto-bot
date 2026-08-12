"""
Market data fetcher via Binance REST API.
"""

import pandas as pd
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import API_KEY, API_SECRET, TESTNET, TIMEFRAME, CANDLE_LIMIT

logger = logging.getLogger("data_feed")

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    return _client


def fetch_ohlcv(symbol: str) -> pd.DataFrame | None:
    """Binance'ten mum verisi çek, DataFrame döndür."""
    try:
        client = get_client()
        klines = client.get_klines(
            symbol=symbol,
            interval=TIMEFRAME,
            limit=CANDLE_LIMIT,
        )
        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]
    except BinanceAPIException as e:
        logger.error(f"Veri çekme hatası {symbol}: {e}")
        return None
    except Exception as e:
        logger.error(f"Beklenmedik hata {symbol}: {e}")
        return None


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Birden fazla coin için anlık fiyat çek."""
    try:
        client = get_client()
        tickers = client.get_symbol_ticker()
        price_map = {t["symbol"]: float(t["price"]) for t in tickers}
        return {s: price_map[s] for s in symbols if s in price_map}
    except BinanceAPIException as e:
        logger.error(f"Fiyat çekme hatası: {e}")
        return {}
