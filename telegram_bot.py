"""
Telegram bildirim ve kontrol sistemi.

.env dosyasına eklenecekler:
  TELEGRAM_BOT_TOKEN=<BotFather'dan alınan token>
  TELEGRAM_CHAT_ID=<kendi chat ID'n>

Chat ID'ni öğrenmek için:
  1. @userinfobot'a Telegram'dan /start yaz
  2. Gelen mesajdaki "Id" değeri senin chat_id'n
"""

import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger("telegram_bot")


class TelegramBot:

    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        self._paused  = threading.Event()   # set → pause aktif
        self._offset  = 0
        self._running = False
        self._handlers: dict[str, callable] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    # ── HTTP yardımcısı ──────────────────────────────────────────────────────

    def _api(self, method: str, data: dict = None, timeout: int = 10) -> dict:
        url     = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = json.dumps(data or {}).encode()
        req     = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())

    # ── Mesaj gönder ─────────────────────────────────────────────────────────

    def send(self, text: str):
        if not self.enabled:
            return
        try:
            self._api("sendMessage", {
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
            })
        except Exception as e:
            logger.debug(f"Telegram gönderim hatası: {e}")

    # ── Komut handler kayıt ──────────────────────────────────────────────────

    def register(self, command: str, callback):
        """callback(args: str) imzasıyla çağrılır."""
        self._handlers[command.lstrip("/").lower()] = callback

    # ── Komut alma döngüsü ───────────────────────────────────────────────────

    def _poll(self):
        while self._running:
            try:
                data = self._api("getUpdates", {
                    "offset":           self._offset,
                    "timeout":          25,
                    "allowed_updates":  ["message"],
                }, timeout=30)
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg  = update.get("message", {})
                    text = msg.get("text", "").strip()
                    cid  = str(msg.get("chat", {}).get("id", ""))
                    if cid != self.chat_id:
                        continue   # başka kişilerden komut kabul etme
                    if not text.startswith("/"):
                        continue
                    parts   = text.split(maxsplit=1)
                    cmd     = parts[0].lstrip("/").lower().split("@")[0]
                    args    = parts[1] if len(parts) > 1 else ""
                    handler = self._handlers.get(cmd)
                    if handler:
                        try:
                            handler(args)
                        except Exception as e:
                            self.send(f"❌ Hata: {e}")
                    else:
                        cmds = " | ".join(f"/{c}" for c in self._handlers)
                        self.send(f"❓ Bilinmeyen komut.\nKomutlar: {cmds}")
            except Exception as e:
                logger.debug(f"Telegram poll hatası: {e}")
                time.sleep(5)

    # ── Başlat / durdur ──────────────────────────────────────────────────────

    def start(self):
        if not self.enabled:
            logger.info("Telegram devre dışı — .env'e TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ekle")
            return
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()
        logger.info("Telegram bot başlatıldı")

    def stop(self):
        self._running = False

    # ── Hazır bildirim mesajları ─────────────────────────────────────────────

    def notify_open(self, symbol, side, price, margin, leverage, sl, tp, score, regime, fg):
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        self.send(
            f"{emoji} <b>POZİSYON AÇILDI</b>\n"
            f"Coin     : <b>{symbol}</b> {direction}\n"
            f"Giriş    : {price:.5g}\n"
            f"SL       : {sl:.5g}  |  TP: {tp:.5g}\n"
            f"Marjin   : {margin:.1f}$  |  Kaldıraç: {leverage}x\n"
            f"Skor     : {score}/100  |  Rejim: {regime}\n"
            f"F&amp;G  : {fg}"
        )

    def notify_close(self, symbol, side, price, net_pnl, reason):
        emoji = "✅" if net_pnl >= 0 else "❌"
        color = "+" if net_pnl >= 0 else ""
        self.send(
            f"{emoji} <b>POZİSYON KAPANDI</b>\n"
            f"Coin   : <b>{symbol}</b>  ({reason})\n"
            f"Fiyat  : {price:.5g}\n"
            f"Net PnL: <b>{color}{net_pnl:.2f}$</b>"
        )

    def notify_kill_switch(self, reason):
        self.send(
            f"🚫 <b>KİLL SWİTCH AKTİF</b>\n"
            f"Sebep: {reason}\n"
            f"Yeni işlem açılmayacak."
        )

    def notify_daily_summary(self, balance, daily_pnl, daily_trades, win_rate, consecutive_losses):
        sign = "+" if daily_pnl >= 0 else ""
        self.send(
            f"📊 <b>GÜNLÜK ÖZET</b>\n"
            f"Bakiye       : {balance:.2f} USDT\n"
            f"Günlük PnL   : {sign}{daily_pnl:.2f}$\n"
            f"İşlem Sayısı : {daily_trades}\n"
            f"Win Rate     : {win_rate*100:.1f}%\n"
            f"Üst üste zarar: {consecutive_losses}"
        )
