"""
Test ortamı.

İki güvenlik önlemi:
1. Gerçek API anahtarı gerekmez — sahte değerler set edilir (hiçbir test ağa çıkmaz).
2. `CRYPTO_BOT_DB` geçici bir dosyaya yönlendirilir, böylece testler canlı
   `trades.db`'ye ASLA dokunmaz.
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# config.py import edilmeden ÖNCE ayarlanmalı
os.environ.setdefault("BINANCE_API_KEY", "test-key-not-real")
os.environ.setdefault("BINANCE_API_SECRET", "test-secret-not-real")

_TMP_DB = Path(tempfile.gettempdir()) / "crypto_bot_test.db"
os.environ["CRYPTO_BOT_DB"] = str(_TMP_DB)

import pytest


@pytest.fixture(autouse=True)
def clean_db():
    """Her test temiz bir veritabanıyla başlar."""
    import database
    if _TMP_DB.exists():
        _TMP_DB.unlink()
    database.DB_PATH = _TMP_DB
    database.init_db()
    yield
    if _TMP_DB.exists():
        _TMP_DB.unlink()
