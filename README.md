# Crypto Trading Bot

## Kurulum

```bash
cd ~/ClaudeWork/crypto-bot
pip install -r requirements.txt
```

## Çalıştırma (Paper Trading)

```bash
python main.py
```

## Gerçek Paraya Geçiş

1. Binance'te API Key al (Futures iznini aç)
2. `config.py` dosyasını aç:
   ```python
   API_KEY       = "buraya_api_key"
   API_SECRET    = "buraya_api_secret"
   PAPER_TRADING = False
   ```
3. Botu başlat

## Risk Ayarları (config.py)

| Ayar | Varsayılan | Açıklama |
|------|-----------|----------|
| MAX_CAPITAL_RISK_PCT | 0.50 | Portföyün max %50'si riskte |
| MAX_LEVERAGE | 10 | Maksimum kaldıraç |
| STOP_LOSS_PCT | 0.03 | %3 stop-loss |
| TAKE_PROFIT_PCT | 0.06 | %6 take-profit |
| MAX_OPEN_POSITIONS | 5 | Aynı anda max 5 pozisyon |
| LOOP_INTERVAL | 60 | 60 saniyede bir analiz |

## Durdurma

`Ctrl+C` ile güvenli şekilde durdurulur.

## Log Dosyası

`bot.log` — tüm açılan/kapanan pozisyonlar kaydedilir.

---
⚠️ **UYARI:** Kripto işlemi yüksek risk taşır. Bot kâr garantisi vermez.
