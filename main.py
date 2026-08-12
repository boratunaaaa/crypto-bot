"""
Ana döngü — TREND + MOMENTUM_FAST, risk bütçeli kill switch, kalıcı state.
"""

import fcntl
import sys
import time
import signal
import logging
import threading
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from rich.live import Live
from rich.console import Console

_BOT_DIR = Path(__file__).parent

from config import (
    PAPER_TRADING, SIGNAL_INTERVAL,
    MAX_CORRELATED_POSITIONS, MIN_CONFIDENCE, MIN_MARGIN_USDT, PAPER_BALANCE,
    MOMENTUM_ENABLED, MOMENTUM_SHADOW_MODE, MOMENTUM_MIN_SCORE,
    HEARTBEAT_ENABLED, HEARTBEAT_INTERVAL_MIN, STALE_ALERT_MINUTES,
    DAILY_RISK_BUDGET_R, MAX_SPREAD_PCT,
)
from market_data import MarketDataManager
from analyzer import analyze_all
from risk_manager import (
    calculate_leverage,
    calculate_position,
    can_add_position,
    portfolio_open_risk_r,
    KillSwitchTracker,
    dynamic_risk_pct,
)
from trader import create_trader, PaperPortfolio
from dashboard import build_layout
from sentiment import SentimentTracker
from database import (
    init_db, save_missed_signal, get_performance_summary,
    get_summary_excluding_top, get_recent_pnls,
    state_get, state_set_many, load_open_positions, save_open_positions,
    save_shadow_signal,
)
from telegram_bot import TelegramBot
import momentum as momentum_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[RotatingFileHandler(str(_BOT_DIR / "bot.log"), maxBytes=10 * 1024 * 1024, backupCount=3)],
)
logger  = logging.getLogger("main")
console = Console()
running = True

# Watchdog: ana döngünün son tur zamanı. Heartbeat thread'i bunu izler.
_last_loop_ts = time.time()


def handle_exit(sig, frame):
    global running
    console.print("\n[yellow]Durduruluyor...[/yellow]")
    running = False


signal.signal(signal.SIGINT,  handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def run():
    global _last_loop_ts

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
    sentiment    = SentimentTracker()
    sentiment.start()

    # ── KALICI STATE GERİ YÜKLEME ──────────────────────────────────────────────
    # Eski davranış: her başlatmada bakiye PAPER_BALANCE'a, kill switch sayaçları
    # sıfıra dönüyordu. systemd'nin Restart=always ayarıyla birleşince bu,
    # günlük zarar limitinin sınırsız kez sıfırlanabilmesi anlamına geliyordu.
    restored_state = False
    if isinstance(trader, PaperPortfolio):
        saved_balance = state_get("paper_balance")
        if saved_balance is not None:
            trader.balance          = float(saved_balance)
            trader.total_pnl        = float(state_get("total_pnl", 0.0))
            trader.total_commission = float(state_get("total_commission", 0.0))
            trader.wins             = int(state_get("wins", 0))
            trader.losses           = int(state_get("losses", 0))
            restored_state = True
            logger.info(
                f"State geri yüklendi: bakiye {trader.balance:.2f}$, "
                f"PnL {trader.total_pnl:+.2f}$, {trader.wins}W/{trader.losses}L"
            )

    n_restored = trader.restore_positions(load_open_positions())

    ks_saved = state_get("kill_switch")
    if ks_saved:
        kill_switch = KillSwitchTracker.from_dict(ks_saved)
        logger.info(
            f"Kill switch geri yüklendi: günlük {kill_switch.daily_pnl:+.2f}$, "
            f"risk {kill_switch.daily_risk_used_r:.1f}R, "
            f"üst üste zarar {kill_switch.consecutive_losses}"
        )
    else:
        kill_switch = KillSwitchTracker()

    def _save_state():
        """Tüm kritik state'i diske yaz. Her işlem sonrası + periyodik çağrılır."""
        try:
            payload = {
                "kill_switch": kill_switch.to_dict(),
                "last_save": datetime.now().isoformat(),
            }
            if isinstance(trader, PaperPortfolio):
                payload.update({
                    "paper_balance":    trader.balance,
                    "total_pnl":        trader.total_pnl,
                    "total_commission": getattr(trader, "total_commission", 0.0),
                    "wins":             trader.wins,
                    "losses":           trader.losses,
                })
            state_set_many(payload)
            save_open_positions(trader.positions)
        except Exception as e:
            logger.error(f"State kaydedilemedi: {e}")

    # ── Telegram bot + komutlar ────────────────────────────────────────────
    tg = TelegramBot()

    def _equity():
        if isinstance(trader, PaperPortfolio):
            return trader.equity
        return trader.get_balance() + sum(p.get("margin", 0.0) for p in trader.positions.values())

    def _tg_status(_):
        bal = trader.balance if isinstance(trader, PaperPortfolio) else trader.get_balance()
        eq  = _equity()
        pos = getattr(trader, "positions", {})
        ks  = kill_switch
        can, reason = ks.can_trade(eq)
        lines = [
            "📊 <b>BOT DURUMU</b>",
            f"Mod       : {'PAPER' if PAPER_TRADING else 'GERÇEK'}",
            f"Equity    : {eq:.2f} USDT  (nakit {bal:.2f})",
            f"Toplam PnL: {getattr(trader, 'total_pnl', 0.0):+.2f}$",
            f"Açık Poz  : {len(pos)}  |  Açık risk: {portfolio_open_risk_r(pos):.2f}R",
            f"Günlük PnL: {ks.daily_pnl:+.2f}$ ({ks.daily_trades} işlem)",
            f"Risk bütçe: {ks.daily_risk_used_r:.1f}R / {DAILY_RISK_BUDGET_R:.1f}R",
            f"Drawdown  : %{ks.current_drawdown(eq)*100:.1f} (tepe {ks.high_water_mark:.0f}$)",
            f"Üst üste zarar: {ks.consecutive_losses}",
            f"Kill switch: {'🚫 ' + reason if not can else '✅ SERBEST'}",
            f"Momentum  : {'GÖLGE' if MOMENTUM_SHADOW_MODE else 'CANLI'}" if MOMENTUM_ENABLED else "Momentum  : KAPALI",
            f"F&G       : {sentiment.value} ({sentiment.label})",
        ]
        if pos:
            lines.append("\n<b>Açık Pozisyonlar:</b>")
            for sym, p in pos.items():
                pct = p.get("qty", 0) / max(p.get("initial_qty", 1), 1e-9) * 100
                lines.append(
                    f"  {sym} {p['side']} [{p.get('strategy','TREND')}] "
                    f"Giriş:{p['entry_price']:.5g} SL:{p['stop_loss']:.5g} "
                    f"({pct:.0f}% açık, MFE {p.get('mfe_r',0):.1f}R)"
                )
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
                f"{sym} {p['side']} [{p.get('strategy','TREND')}] | Giriş: {p['entry_price']:.5g}\n"
                f"  SL: {p['stop_loss']:.5g} ({p.get('stop_kind','INITIAL')})\n"
                f"  MFE: {p.get('mfe_r',0):.2f}R  MAE: {p.get('mae_r',0):.2f}R\n"
                f"  Marjin: {p['margin']:.1f}$ | {p['leverage']}x | "
                f"TP1:{'✓' if p.get('tp1_done') else '−'} TP2:{'✓' if p.get('tp2_done') else '−'}"
            )
        tg.send("\n".join(lines))

    def _tg_report(_):
        summary = get_performance_summary()
        if not summary:
            tg.send("Henüz kaydedilen işlem yok.")
            return
        pf  = summary.get("profit_factor", 0)
        pf_c= "✅" if pf >= 1.3 else ("⚠️" if pf >= 1.0 else "❌")
        ex  = get_summary_excluding_top(2)
        msg = (
            f"📈 <b>PERFORMANS RAPORU</b>\n"
            f"Toplam İşlem : {summary['total']}\n"
            f"Win Rate     : {summary['win_rate']*100:.1f}%\n"
            f"Net PnL      : {summary['net_pnl']:+.2f}$\n"
            f"Ort. R       : {summary.get('avg_r', 0):+.3f}R\n"
            f"Komisyon     : -{summary['commission']:.2f}$\n"
            f"Profit Factor: {pf_c} {pf:.2f}\n"
            f"En İyi       : +{summary['max_win']:.2f}$\n"
            f"En Kötü      : {summary['max_loss']:.2f}$\n"
            f"Ort. Süre    : {summary['avg_duration']:.0f} dk"
        )
        if ex:
            ok = "✅" if ex["net_pnl"] > 0 else "❌"
            msg += (
                f"\n\n<b>En iyi 2 işlem hariç:</b>\n"
                f"{ok} {ex['net_pnl']:+.2f}$ ({ex['total']} işlem)\n"
                f"<i>Gerçek paraya geçiş kapısı: bu değer pozitif olmalı</i>"
            )
        for strat in ("TREND", "MOMENTUM_FAST"):
            s = get_performance_summary(strat)
            if s:
                msg += (
                    f"\n\n<b>{strat}</b>: {s['total']} işlem, "
                    f"{s['net_pnl']:+.2f}$, WR %{s['win_rate']*100:.0f}, "
                    f"PF {s['profit_factor']:.2f}"
                )
        tg.send(msg)

    def _tg_help(_):
        tg.send(
            "🤖 <b>KOMUTLAR</b>\n"
            "/status     — Bakiye, pozisyonlar, risk bütçesi\n"
            "/positions  — Açık pozisyon detayı\n"
            "/report     — Performans + strateji kırılımı\n"
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
        for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
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
        current = _get_public_ip()
        if current:
            _IP_FILE.write_text(current)
            if last_ip and last_ip != current:
                tg.send(
                    "🔄 <b>IP DEĞİŞTİ — Bot yeniden başlatıldı</b>\n"
                    f"Eski IP : <code>{last_ip}</code>\n"
                    f"Yeni IP : <code>{current}</code>\n\n"
                    "⚠️ Binance'e ekle: API Management → Bot key → IP Restriction"
                )
                logger.warning(f"Bot yeni IP ile başlatıldı: {last_ip} → {current}")
            else:
                tg.send(
                    f"🤖 <b>Bot başlatıldı</b>\n"
                    f"IP : <code>{current}</code>\n"
                    f"Mod: {'PAPER' if PAPER_TRADING else 'GERÇEK'}\n"
                    f"State: {'geri yüklendi' if restored_state else 'sıfırdan'}"
                    + (f" | {n_restored} açık pozisyon" if n_restored else "")
                )
            last_ip = current

        while running:
            time.sleep(300)
            if not running:
                break
            new_ip = _get_public_ip()
            if new_ip and new_ip != last_ip:
                logger.warning(f"IP değişti: {last_ip} → {new_ip}")
                _IP_FILE.write_text(new_ip)
                tg.send(
                    "🚨 <b>IP DEĞİŞTİ!</b>\n"
                    f"Eski: <code>{last_ip}</code>\nYeni: <code>{new_ip}</code>\n\n"
                    "⚠️ Hemen Binance API IP kısıtlamasına ekle."
                )
                last_ip = new_ip

    threading.Thread(target=_ip_monitor, daemon=True, name="ip-monitor").start()

    # ── HEARTBEAT + WATCHDOG ──────────────────────────────────────────────────
    # Uzun kesintilerin ortak özelliği "kimse fark etmedi" idi: 27→30 Temmuz
    # (~2.5 gün) ve 30 Temmuz→5 Ağustos (~6.75 gün). systemd botu ayağa kaldırır
    # ama "ayakta ama veri akmıyor" durumunu göremez. Bu thread onu görür.
    def _heartbeat():
        last_beat = time.time()
        stale_notified = False
        while running:
            time.sleep(30)
            if not running:
                break
            now = time.time()
            silence_min = (now - _last_loop_ts) / 60

            if silence_min >= STALE_ALERT_MINUTES and not stale_notified:
                tg.send(
                    f"🚨 <b>BOT SESSİZ</b>\n"
                    f"Ana döngü {silence_min:.0f} dakikadır tur atmıyor.\n"
                    f"Süreç ayakta ama veri akışı durmuş olabilir."
                )
                logger.error(f"WATCHDOG: ana döngü {silence_min:.0f} dakikadır sessiz")
                stale_notified = True
            elif silence_min < STALE_ALERT_MINUTES and stale_notified:
                tg.send("✅ Bot tekrar akışta.")
                stale_notified = False

            if HEARTBEAT_ENABLED and (now - last_beat) / 60 >= HEARTBEAT_INTERVAL_MIN:
                last_beat = now
                eq = _equity()
                tg.send(
                    f"💓 <b>Yaşıyorum</b>\n"
                    f"Equity: {eq:.2f}$ | PnL: {getattr(trader,'total_pnl',0):+.2f}$\n"
                    f"Açık: {len(trader.positions)} | "
                    f"Risk: {kill_switch.daily_risk_used_r:.1f}R/{DAILY_RISK_BUDGET_R:.0f}R\n"
                    f"Günlük: {kill_switch.daily_pnl:+.2f}$ ({kill_switch.daily_trades} işlem)"
                )

    threading.Thread(target=_heartbeat, daemon=True, name="heartbeat").start()

    console.print("[bold green]🚀 Bot başlatılıyor...[/bold green]")

    # ── Gerçek mod: spot coinleri USDT'ye çevir, Futures'a transfer ──────────
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

        if isinstance(trader, FuturesTrader):
            try:
                spot_usdt = next(
                    (float(b["free"]) for b in cl.get_account()["balances"] if b["asset"] == "USDT"), 0.0
                )
                if spot_usdt >= 1.0:
                    amt = round(spot_usdt * 0.99, 2)
                    if trader.transfer_spot_to_futures(amt):
                        console.print(f"  [green]{amt:.4f} USDT Spot → Futures[/green]")
            except Exception as e:
                console.print(f"  [yellow]Transfer atlandı: {e}[/yellow]")

        bal = trader.sync_balance()
        console.print(f"[bold cyan]💰 Kullanılabilir USDT: {bal:.4f}$[/bold cyan]")

    mdm.start()
    time.sleep(3)
    mdm.refresh_ticker()

    kill_switch.initialize(_equity())
    _save_state()

    console.print(f"[green]✅ {len(mdm.ready_symbols())} coin hazır[/green]")

    signals           = []
    loop_ms           = 0.0
    last_balance_sync = 0.0
    btc_trend         = "SIDEWAYS"
    last_btc_check    = 0.0
    last_momentum     = 0.0
    last_ticker       = time.time()
    last_state_save   = 0.0
    _ks_notified      = False
    momentum_signals: list = []

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while running:
            t0 = time.perf_counter()
            _last_loop_ts = time.time()

            prices = mdm.get_all_prices()

            # Açık pozisyonlar için fiyat tazeliği garantisi (bayat fiyatla
            # asla stop kontrolü yapma — 2026-08-07 WS olayı)
            held_positions = getattr(trader, "positions", {})
            if held_positions:
                prices.update(mdm.get_prices_for(list(held_positions.keys())))

            if hasattr(trader, "sync_positions"):
                trader.sync_positions()

            if hasattr(trader, "sync_balance"):
                now = time.perf_counter()
                if now - last_balance_sync >= 60:
                    trader.sync_balance()
                    last_balance_sync = now

            equity = _equity()

            # ── Çıkış motoru: kısmi çıkış / breakeven / trailing / time-stop ──
            if hasattr(trader, "check_positions"):
                closed = trader.check_positions(prices)
                for sym, reason, pnl, r_mult in closed:
                    logger.info(
                        f"KAPANDI: {sym} | {reason} | PnL:{pnl:+.2f}$ ({r_mult:+.2f}R)"
                    )
                    kill_switch.record_trade_close(pnl, _equity(), realized_r=r_mult, symbol=sym)
                    tg.notify_close(sym, "", prices.get(sym, 0), pnl, reason)
                if closed:
                    _save_state()

            now = time.perf_counter()
            if now - last_btc_check >= 60:
                btc_trend = mdm.get_btc_trend()
                last_btc_check = now

            # ── TREND stratejisi taraması ──────────────────────────────────
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

            # ── MOMENTUM_FAST taraması ─────────────────────────────────────
            if MOMENTUM_ENABLED and time.time() - last_momentum >= 60:
                last_momentum = time.time()
                try:
                    if time.time() - last_ticker >= 120:
                        mdm.refresh_ticker()
                        mdm.clear_spread_cache()
                        last_ticker = time.time()
                    movers = mdm.get_movers(n=25)
                    if movers:
                        mdm.refresh_fast_ohlcv(movers)
                        momentum_signals = momentum_mod.scan(
                            mdm, movers, btc_change_15m=mdm.get_btc_change_15m()
                        )
                        for ms in momentum_signals:
                            if ms.action == "HOLD":
                                if ms.reject_reason:
                                    save_missed_signal(
                                        ms.symbol, "MOMENTUM", ms.score, ms.confidence,
                                        f"[MOM] {ms.reject_reason}", btc_trend, "MOMENTUM",
                                    )
                                continue
                            if MOMENTUM_SHADOW_MODE:
                                # GÖLGE MOD: işlem açılmaz, yalnız kaydedilir.
                                save_shadow_signal(
                                    ms.symbol, ms.action, ms.score, ms.price, ms.features
                                )
                                logger.info(
                                    f"[GÖLGE] MOMENTUM {ms.symbol} {ms.action} "
                                    f"@{ms.price:.6g} skor:{ms.score} | {ms.reason}"
                                )
                except Exception as e:
                    logger.warning(f"Momentum taraması hatası: {e}")

            # ── Kill switch ────────────────────────────────────────────────
            equity = _equity()
            can_trade, ks_reason = kill_switch.can_trade(equity)
            if not can_trade:
                logger.warning(f"KİLL SWİTCH: {ks_reason}")
                if not _ks_notified:
                    tg.notify_kill_switch(ks_reason)
                    _ks_notified = True
            else:
                _ks_notified = False

            # ── İşlem kararları ───────────────────────────────────────────
            positions = getattr(trader, "positions", {})

            # MOMENTUM canlı modda ise sinyalleri TREND sinyallerinin önüne al
            candidates = []
            if MOMENTUM_ENABLED and not MOMENTUM_SHADOW_MODE:
                candidates.extend([
                    s for s in momentum_signals
                    if s.action in ("BUY", "SELL") and s.score >= MOMENTUM_MIN_SCORE
                ])
            candidates.extend(signals)

            for sig in candidates:
                if not running:
                    break

                strategy = getattr(sig, "strategy", "TREND")
                is_trade_signal = (
                    sig.action in ("BUY", "SELL")
                    and getattr(sig, "confidence", 0) >= (
                        MIN_CONFIDENCE if strategy == "TREND" else MOMENTUM_MIN_SCORE / 100
                    )
                )
                if not is_trade_signal:
                    continue

                # Açılamama sebeplerini kaydet (missed_signals)
                missed_reason = None
                if not can_trade or tg.is_paused:
                    missed_reason = ks_reason if not can_trade else "Telegram /pause"
                else:
                    ok_port, port_reason = can_add_position(positions, equity)
                    if not ok_port:
                        missed_reason = port_reason
                    elif sig.symbol in positions:
                        missed_reason = "Zaten açık pozisyon var"
                    elif kill_switch.symbol_on_cooldown(sig.symbol):
                        missed_reason = "Sembol soğuma süresinde"

                if missed_reason:
                    save_missed_signal(
                        sig.symbol, sig.action, sig.score, sig.confidence,
                        missed_reason, btc_trend, sig.market_regime,
                    )
                    if not can_trade or tg.is_paused:
                        break
                    continue

                if sig.symbol == "BTCUSDT":
                    continue

                same_dir = sum(1 for p in positions.values() if p["side"] == sig.action)
                if same_dir >= MAX_CORRELATED_POSITIONS:
                    save_missed_signal(
                        sig.symbol, sig.action, sig.score, sig.confidence,
                        f"Korelasyon limiti ({same_dir}/{MAX_CORRELATED_POSITIONS} {sig.action})",
                        btc_trend, sig.market_regime,
                    )
                    continue

                # Momentum stratejisi kendi izole bütçesini aşamaz
                if strategy == "MOMENTUM_FAST":
                    ok_mom, mom_reason = kill_switch.can_trade(equity, strategy="MOMENTUM_FAST")
                    if not ok_mom:
                        save_missed_signal(
                            sig.symbol, sig.action, sig.score, sig.confidence,
                            f"[MOM] {mom_reason}", btc_trend, "MOMENTUM",
                        )
                        continue

                # Spread kontrolü — geniş spread'de execution kalitesi düşer
                spread = mdm.get_spread_pct(sig.symbol)
                if spread is not None and spread > MAX_SPREAD_PCT:
                    save_missed_signal(
                        sig.symbol, sig.action, sig.score, sig.confidence,
                        f"Spread geniş (%{spread*100:.2f})", btc_trend, sig.market_regime,
                    )
                    continue

                leverage = calculate_leverage(sig.confidence, sig.score)
                risk_pct = dynamic_risk_pct(get_recent_pnls(30), sig.score)
                open_margin = sum(p.get("margin", 0.0) for p in positions.values())

                margin, qty, risk_amount, err = calculate_position(
                    equity, leverage, sig.price, sig.sl, risk_pct,
                    open_margin_total=open_margin,
                )
                if err:
                    save_missed_signal(
                        sig.symbol, sig.action, sig.score, sig.confidence,
                        err, btc_trend, sig.market_regime,
                    )
                    continue

                snapshot = {
                    "score": sig.score, "confidence": sig.confidence,
                    "risk_pct": risk_pct, "leverage": leverage,
                    "btc_trend": btc_trend, "regime": sig.market_regime,
                    "atr_pct": getattr(sig, "atr_pct", 0.0),
                    "spread": spread, "equity": equity,
                    "stop_distance_pct": abs(sig.price - sig.sl) / sig.price,
                    "fear_greed": sentiment.value,
                }
                if strategy == "MOMENTUM_FAST":
                    snapshot["momentum_features"] = getattr(sig, "features", {})

                ok, msg = trader.open_position(
                    sig.symbol, sig.action, sig.price,
                    margin, leverage, sig.sl, sig.tp,
                    reason=sig.reason, score=sig.score,
                    btc_trend=btc_trend,
                    market_regime=sig.market_regime,
                    atr_pct=getattr(sig, "atr_pct", 0.0),
                    fear_greed=sentiment.value,
                    strategy=strategy, qty=qty, risk_amount=risk_amount,
                    entry_snapshot=snapshot,
                )
                if ok:
                    kill_switch.record_trade_open(equity, risk_r=1.0, strategy=strategy)
                    positions = trader.positions
                    logger.info(
                        f"AÇILDI [{strategy}]: {sig.symbol} {sig.action} "
                        f"@{sig.price:.5g} | {leverage}x | "
                        f"Marjin:{margin:.1f}$ Risk:{risk_amount:.2f}$ | "
                        f"SL:{sig.sl:.5g} | Skor:{sig.score}/100 | "
                        f"Bütçe:{kill_switch.daily_risk_used_r:.1f}R | {sig.reason}"
                    )
                    tg.notify_open(
                        sig.symbol, sig.action, sig.price,
                        margin, leverage, sig.sl, sig.tp,
                        sig.score, sig.market_regime,
                        f"{sentiment.value} ({sentiment.label})",
                    )
                    _save_state()
                else:
                    logger.warning(f"Açılamadı {sig.symbol}: {msg}")

            # ── Periyodik state kaydı ──────────────────────────────────────
            if time.time() - last_state_save >= 60:
                _save_state()
                last_state_save = time.time()

            # ── Dashboard ───────────────────────────────────────────────────
            loop_ms   = (time.perf_counter() - t0) * 1000
            balance   = trader.balance if isinstance(trader, PaperPortfolio) else trader.get_balance()
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
                initial_balance = PAPER_BALANCE,
                kill_switch     = kill_switch,
                btc_trend       = btc_trend,
                sentiment       = sentiment,
            )
            live.update(layout)

            elapsed   = time.perf_counter() - t0
            sleep_for = max(0.0, SIGNAL_INTERVAL - elapsed)
            if sleep_for > 0 and running:
                time.sleep(sleep_for)

    _save_state()
    mdm.stop()
    console.print("\n[bold]Bot durduruldu. State kaydedildi.[/bold]")
    if isinstance(trader, PaperPortfolio):
        total = trader.wins + trader.losses
        console.print(f"Bakiye    : [cyan]{trader.balance:.2f}[/cyan] USDT")
        console.print(f"PnL       : [{'green' if trader.total_pnl>=0 else 'red'}]{trader.total_pnl:+.2f}[/] USDT")
        console.print(f"İşlemler  : {total}  (Kazanan: {trader.wins}  Kaybeden: {trader.losses})")
        console.print(f"Win Rate  : {trader.win_rate*100:.1f}%")


if __name__ == "__main__":
    run()
