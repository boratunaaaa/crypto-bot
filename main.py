"""
Ana döngü — 100 coin, her saniye, maksimum kazanç modu.
"""

import fcntl
import sys
import time
import signal
import socket
import logging
import threading
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from rich.live import Live
from rich.console import Console

_BOT_DIR = Path(__file__).parent

from config import (
    PAPER_TRADING, SIGNAL_INTERVAL,
    MAX_OPEN_POSITIONS, MAX_CORRELATED_POSITIONS, MIN_CONFIDENCE, MIN_MARGIN_USDT, PAPER_BALANCE,
    RISK_PER_TRADE_PCT,
)
from market_data import MarketDataManager
from analyzer import analyze_all
from risk_manager import (
    calculate_leverage,
    calculate_position_size_by_risk,
    KillSwitchTracker,
    dynamic_risk_pct,
)
from trader import create_trader, PaperPortfolio
from dashboard import build_layout
from sentiment import SentimentTracker
from database import init_db, save_missed_signal, get_performance_summary, get_daily_report
from telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[RotatingFileHandler(str(_BOT_DIR / "bot.log"), maxBytes=10 * 1024 * 1024, backupCount=3)],
)
logger  = logging.getLogger("main")
console = Console()
running = True


def handle_exit(sig, frame):
    global running
    console.print("\n[yellow]Durduruluyor...[/yellow]")
    running = False


signal.signal(signal.SIGINT,  handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def run():
    # ── Çift başlatma koruması ─────────────────────────────────────────────────
    _lock_fh = open(str(_BOT_DIR / "crypto-bot.lock"), "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[bold red]❌ Bot zaten çalışıyor! Önce mevcut örneği kapatın.[/bold red]")
        sys.exit(1)

    init_db()
    mdm          = MarketDataManager()
    trader       = create_trader()
    kill_switch  = KillSwitchTracker()
    sentiment    = SentimentTracker()
    sentiment.start()

    # ── Telegram bot + komutlar ────────────────────────────────────────────
    tg = TelegramBot()

    def _tg_status(_):
        bal = (trader.balance if isinstance(trader, PaperPortfolio)
               else trader.get_balance())
        pos = getattr(trader, "positions", {})
        ks  = kill_switch
        lines = [
            "📊 <b>BOT DURUMU</b>",
            f"Mod      : {'PAPER' if PAPER_TRADING else 'GERÇEK'}",
            f"Bakiye   : {bal:.2f} USDT",
            f"Açık Poz : {len(pos)}",
            f"Günlük PnL: {ks.daily_pnl:+.2f}$ ({ks.daily_trades} işlem)",
            f"Üst üste zarar: {ks.consecutive_losses}/3",
            f"Kill switch: {'🚫 AKTİF' if not ks.can_trade(bal)[0] else '✅ SERBEST'}",
            f"F&G      : {sentiment.value} ({sentiment.label})",
        ]
        if pos:
            lines.append("\n<b>Açık Pozisyonlar:</b>")
            for sym, p in pos.items():
                lines.append(f"  {sym} {p['side']} | Giriş:{p['entry_price']:.5g} | SL:{p['stop_loss']:.5g}")
        tg.send("\n".join(lines))

    def _tg_pause(_):
        tg._paused.set()
        tg.send("⏸ Bot duraklatıldı. Yeni işlem açılmayacak. /resume ile devam et.")

    def _tg_resume(_):
        tg._paused.clear()
        tg.send("▶️ Bot devam ediyor.")

    def _tg_positions(_):
        pos = getattr(trader, "positions", {})
        if not pos:
            tg.send("Açık pozisyon yok.")
            return
        lines = ["💼 <b>AÇIK POZİSYONLAR</b>"]
        for sym, p in pos.items():
            lines.append(
                f"{sym} {p['side']} | Giriş: {p['entry_price']:.5g}\n"
                f"  SL: {p['stop_loss']:.5g} | TP: {p['take_profit']:.5g}\n"
                f"  Marjin: {p['margin']:.1f}$ | {p['leverage']}x"
            )
        tg.send("\n".join(lines))

    def _tg_report(_):
        summary = get_performance_summary()
        if not summary:
            tg.send("Henüz kaydedilen işlem yok.")
            return
        pf  = summary.get("profit_factor", 0)
        pf_c= "✅" if pf >= 1.3 else ("⚠️" if pf >= 1.0 else "❌")
        wr  = summary.get("win_rate", 0)
        tg.send(
            f"📈 <b>PERFORMANS RAPORU</b>\n"
            f"Toplam İşlem : {summary['total']}\n"
            f"Win Rate     : {wr*100:.1f}%\n"
            f"Net PnL      : {summary['net_pnl']:+.2f}$\n"
            f"Komisyon     : -{summary['commission']:.2f}$\n"
            f"Profit Factor: {pf_c} {pf:.2f}\n"
            f"En İyi       : +{summary['max_win']:.2f}$\n"
            f"En Kötü      : {summary['max_loss']:.2f}$\n"
            f"Ort. Süre    : {summary['avg_duration']:.0f} dk"
        )

    def _tg_help(_):
        tg.send(
            "🤖 <b>KOMUTLAR</b>\n"
            "/status     — Bakiye, pozisyonlar, durum\n"
            "/positions  — Açık pozisyonlar\n"
            "/report     — Performans raporu\n"
            "/pause      — Yeni işlem açmayı durdur\n"
            "/resume     — Devam et\n"
            "/help       — Bu mesaj"
        )

    tg.register("status",    _tg_status)
    tg.register("pause",     _tg_pause)
    tg.register("resume",    _tg_resume)
    tg.register("positions", _tg_positions)
    tg.register("report",    _tg_report)
    tg.register("help",      _tg_help)
    tg.start()

    # ── IP izleyici ───────────────────────────────────────────────────────────
    _IP_FILE = _BOT_DIR / ".last_ip"

    def _get_public_ip(timeout: int = 8) -> str | None:
        for url in [
            "https://api.ipify.org",
            "https://ifconfig.me/ip",
            "https://icanhazip.com",
        ]:
            try:
                req  = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
                resp = urllib.request.urlopen(req, timeout=timeout)
                ip   = resp.read().decode().strip()
                if ip:
                    return ip
            except Exception:
                continue
        return None

    def _ip_monitor():
        last_ip = _IP_FILE.read_text().strip() if _IP_FILE.exists() else None
        # Başlangıçta mevcut IP'yi bildir
        current = _get_public_ip()
        if current:
            _IP_FILE.write_text(current)
            if last_ip and last_ip != current:
                # Farklı IP ile başlatıldı
                tg.send(
                    "🔄 <b>IP DEĞİŞTİ — Bot yeniden başlatıldı</b>\n"
                    f"Eski IP : <code>{last_ip}</code>\n"
                    f"Yeni IP : <code>{current}</code>\n\n"
                    "⚠️ Binance'e ekle:\n"
                    "Binance → API Management → Bot key → Edit → IP Restriction → bu IP'yi ekle"
                )
                logger.warning(f"Bot yeni IP ile başlatıldı: {last_ip} → {current}")
            else:
                tg.send(
                    f"🤖 <b>Bot başlatıldı</b>\n"
                    f"IP : <code>{current}</code>\n"
                    f"Mod: {'PAPER' if PAPER_TRADING else 'GERÇEK'}"
                )
            last_ip = current

        while running:
            time.sleep(300)  # 5 dakikada bir kontrol
            if not running:
                break
            new_ip = _get_public_ip()
            if new_ip and new_ip != last_ip:
                logger.warning(f"IP değişti: {last_ip} → {new_ip}")
                _IP_FILE.write_text(new_ip)
                tg.send(
                    "🚨 <b>IP DEĞİŞTİ!</b>\n"
                    f"Eski IP : <code>{last_ip}</code>\n"
                    f"Yeni IP : <code>{new_ip}</code>\n\n"
                    "⚠️ Hemen Binance'e ekle:\n"
                    "Binance → API Management → Bot key → Edit → IP Restriction → yeni IP'yi ekle\n\n"
                    "Bot bağlanmaya çalışmaya devam edecek."
                )
                last_ip = new_ip

    threading.Thread(target=_ip_monitor, daemon=True, name="ip-monitor").start()

    console.print("[bold green]🚀 Maksimum kazanç modu başlatılıyor...[/bold green]")

    # ── Başlangıç: coinleri USDT'ye çevir, Futures modunda transfer et ───────
    from trader import SpotTrader, FuturesTrader
    from binance.client import Client as _Client
    from config import API_KEY as _KEY, API_SECRET as _SEC

    if isinstance(trader, (SpotTrader, FuturesTrader)):
        cl = _Client(_KEY, _SEC)
        console.print("[yellow]Spot coinler kontrol ediliyor...[/yellow]")
        try:
            for b in cl.get_account()["balances"]:
                asset, free = b["asset"], float(b["free"])
                if asset in ("USDT", "BNB") or free <= 0:
                    continue
                try:
                    cl.get_symbol_info(f"{asset}USDT")
                    tmp = SpotTrader(); tmp.client = cl
                    recv = tmp.sell_asset_to_usdt(asset)
                    if recv > 0:
                        console.print(f"  [green]{asset} satıldı → {recv:.4f} USDT[/green]")
                except Exception:
                    pass
        except Exception as e:
            console.print(f"  [dim]Coin kontrolü atlandı: {e}[/dim]")

        # Futures modunda Spot→Futures transfer
        if isinstance(trader, FuturesTrader):
            try:
                spot_usdt = next(
                    (float(b["free"]) for b in cl.get_account()["balances"] if b["asset"] == "USDT"), 0.0
                )
                if spot_usdt >= 1.0:
                    amt = round(spot_usdt * 0.99, 2)
                    if trader.transfer_spot_to_futures(amt):
                        console.print(f"  [green]{amt:.4f} USDT Spot → Futures transfer edildi[/green]")
            except Exception as e:
                console.print(f"  [yellow]Transfer atlandı: {e}[/yellow]")

        bal = trader.sync_balance()
        console.print(f"[bold cyan]💰 Kullanılabilir USDT: {bal:.4f}$[/bold cyan]")

    mdm.start()
    time.sleep(3)

    balance_now = (trader.balance if isinstance(trader, PaperPortfolio)
                   else trader.get_balance())
    kill_switch.initialize(balance_now)

    console.print(f"[green]✅ {len(mdm.ready_symbols())} coin hazır[/green]")

    signals           = []
    loop_ms           = 0.0
    last_balance_sync = 0.0
    btc_trend         = "SIDEWAYS"
    last_btc_check    = 0.0
    _ks_notified      = False   # kill switch bildirimini tek seferlik gönder

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while running:
            t0 = time.perf_counter()

            prices = mdm.get_all_prices()

            # ── Açık pozisyonlar için fiyat tazeliği garantisi ─────────────
            # SL/TP kontrolü yanlış (bayat) fiyatla asla yapılmasın — WS akışı
            # bir sembolü sessizce düşürürse REST'ten tazelenir (bkz.
            # MarketDataManager.get_prices_for docstring'i, 2026-08-07 olayı).
            held_positions = getattr(trader, "positions", {})
            if held_positions:
                prices.update(mdm.get_prices_for(list(held_positions.keys())))

            # ── Binance pozisyon senkronizasyonu ───────────────────────────
            if hasattr(trader, "sync_positions"):
                trader.sync_positions()

            # ── Periyodik bakiye sync ──────────────────────────────────────
            if hasattr(trader, "sync_balance"):
                now = time.perf_counter()
                if now - last_balance_sync >= 60:
                    trader.sync_balance()
                    last_balance_sync = now

            # ── SL/TP kontrolü + kill switch kayıt ────────────────────────
            balance = (trader.balance if isinstance(trader, PaperPortfolio)
                       else trader.get_balance())

            if hasattr(trader, "check_positions"):
                for sym, reason, pnl in trader.check_positions(prices):
                    logger.info(f"KAPANDI: {sym} | {reason} | PnL:{pnl:+.2f}$")
                    kill_switch.record_trade_close(pnl, balance)
                    tg.notify_close(sym, "", prices.get(sym, 0), pnl, reason)

            # ── BTC trend (60sn'de bir güncelle) ──────────────────────────
            now = time.perf_counter()
            if now - last_btc_check >= 60:
                btc_trend = mdm.get_btc_trend()
                last_btc_check = now

            # ── Coin analizi ───────────────────────────────────────────────
            ready   = mdm.ready_symbols()
            signals = analyze_all(
                ready,
                get_ohlcv    = mdm.get_ohlcv,
                get_price    = mdm.get_price,
                get_funding  = mdm.get_funding,
                get_oi_trend = mdm.get_oi_trend,
                btc_trend    = btc_trend,
                max_workers  = 20,
            )

            # ── Kill switch kontrolü ───────────────────────────────────────
            balance    = (trader.balance if isinstance(trader, PaperPortfolio)
                          else trader.get_balance())
            can_trade, ks_reason = kill_switch.can_trade(balance)
            if not can_trade:
                logger.warning(f"KİLL SWİTCH: {ks_reason}")
                if not _ks_notified:
                    tg.notify_kill_switch(ks_reason)
                    _ks_notified = True
            else:
                _ks_notified = False

            # ── İşlem kararları ───────────────────────────────────────────
            positions  = getattr(trader, "positions", {})
            open_count = len(positions)

            for sig in signals:
                if not running:
                    break

                # Açılamayacak sinyalleri "missed" olarak kaydet
                if sig.action in ("BUY", "SELL") and sig.confidence >= MIN_CONFIDENCE:
                    missed_reason = None
                    if not can_trade or tg.is_paused:
                        missed_reason = ks_reason if not can_trade else "Telegram /pause"
                    elif open_count >= MAX_OPEN_POSITIONS:
                        missed_reason = f"Max pozisyon ({MAX_OPEN_POSITIONS})"
                    elif sig.symbol in positions:
                        missed_reason = "Zaten açık pozisyon var"

                    if missed_reason:
                        save_missed_signal(
                            sig.symbol, sig.action, sig.score, sig.confidence,
                            missed_reason, btc_trend, sig.market_regime,
                        )

                if open_count >= MAX_OPEN_POSITIONS:
                    break
                if not can_trade or tg.is_paused:
                    break
                if sig.action == "HOLD" or sig.confidence < MIN_CONFIDENCE:
                    continue
                if sig.symbol in positions:
                    continue
                if sig.symbol == "BTCUSDT":
                    continue

                # Korelasyon filtresi — aynı yönde çok fazla pozisyon açma
                same_dir = sum(
                    1 for p in positions.values()
                    if p["side"] == sig.action
                )
                if same_dir >= MAX_CORRELATED_POSITIONS:
                    save_missed_signal(
                        sig.symbol, sig.action, sig.score, sig.confidence,
                        f"Korelasyon limiti ({same_dir}/{MAX_CORRELATED_POSITIONS} {sig.action})",
                        btc_trend, sig.market_regime,
                    )
                    continue

                leverage = calculate_leverage(sig.confidence)

                # Dinamik risk — son işlemlerin PnL listesine göre ayarla
                recent_pnls = [
                    t["pnl"] for t in trader.trade_log
                    if "pnl" in t
                ]
                risk_pct = dynamic_risk_pct(recent_pnls)
                margin   = calculate_position_size_by_risk(
                    balance, leverage, sig.price, sig.sl
                )
                # risk_pct dinamik olarak scaling yap
                if abs(risk_pct - RISK_PER_TRADE_PCT) > 1e-9:
                    margin = margin * (risk_pct / RISK_PER_TRADE_PCT)
                    margin = round(margin, 2)

                if margin < MIN_MARGIN_USDT:
                    continue

                ok, msg = trader.open_position(
                    sig.symbol, sig.action, sig.price,
                    margin, leverage, sig.sl, sig.tp,
                    reason=sig.reason, score=sig.score,
                    btc_trend=btc_trend,
                    market_regime=sig.market_regime,
                    atr_pct=sig.atr_pct,
                    fear_greed=sentiment.value,
                )
                if ok:
                    open_count += 1
                    kill_switch.record_trade_open(balance)
                    logger.info(
                        f"AÇILDI: {sig.symbol} {sig.action} "
                        f"@{sig.price:.5g} | {leverage}x | "
                        f"Marjin:{margin:.1f}$ | SL:{sig.sl:.5g} TP:{sig.tp:.5g} | "
                        f"Skor:{sig.score}/100 | Rejim:{sig.market_regime} | "
                        f"F&G:{sentiment.value}({sentiment.label}) | {sig.reason}"
                    )
                    tg.notify_open(
                        sig.symbol, sig.action, sig.price,
                        margin, leverage, sig.sl, sig.tp,
                        sig.score, sig.market_regime,
                        f"{sentiment.value} ({sentiment.label})",
                    )
                else:
                    logger.warning(f"Açılamadı {sig.symbol}: {msg}")

            # ── Dashboard ───────────────────────────────────────────────────
            loop_ms   = (time.perf_counter() - t0) * 1000
            balance   = (trader.balance if isinstance(trader, PaperPortfolio)
                         else trader.get_balance())
            total_pnl = getattr(trader, "total_pnl", 0.0)
            win_rate  = getattr(trader, "win_rate", 0.0)
            positions = getattr(trader, "positions", {})
            trade_log = getattr(trader, "trade_log", [])

            layout = build_layout(
                signals, positions, balance, total_pnl,
                trade_log, PAPER_TRADING,
                loop_ms         = loop_ms,
                ready_count     = len(ready),
                total_count     = len(mdm.get_symbols()),
                win_rate        = win_rate,
                initial_balance = PAPER_BALANCE if PAPER_TRADING else trader.sync_balance() or balance,
                kill_switch     = kill_switch,
                btc_trend       = btc_trend,
                sentiment       = sentiment,
            )
            live.update(layout)

            # ── Bir sonraki döngüye kadar bekle ────────────────────────────
            elapsed   = time.perf_counter() - t0
            sleep_for = max(0.0, SIGNAL_INTERVAL - elapsed)
            if sleep_for > 0 and running:
                time.sleep(sleep_for)

    mdm.stop()
    console.print("\n[bold]Bot durduruldu.[/bold]")
    if isinstance(trader, PaperPortfolio):
        total = trader.wins + trader.losses
        console.print(f"Bakiye    : [cyan]{trader.balance:.2f}[/cyan] USDT")
        console.print(f"PnL       : [{'green' if trader.total_pnl>=0 else 'red'}]{trader.total_pnl:+.2f}[/] USDT")
        console.print(f"İşlemler  : {total}  (Kazanan: {trader.wins}  Kaybeden: {trader.losses})")
        console.print(f"Win Rate  : {trader.win_rate*100:.1f}%")


if __name__ == "__main__":
    run()
