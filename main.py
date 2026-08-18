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
    PAPER_TRADING, SIGNAL_INTERVAL, POSITION_CHECK_INTERVAL,
    MAX_CORRELATED_POSITIONS, MIN_CONFIDENCE, MIN_MARGIN_USDT, PAPER_BALANCE,
    MOMENTUM_ENABLED, MOMENTUM_SHADOW_MODE, MOMENTUM_MIN_SCORE, MOMENTUM_BALANCE,
    EXPERIMENT_STRATEGIES_ENABLED, EXPERIMENT_BALANCE, EXPERIMENT_MIN_SCORE,
    HEARTBEAT_ENABLED, HEARTBEAT_INTERVAL_MIN, STALE_ALERT_MINUTES,
    DAILY_RISK_BUDGET_R, MAX_SPREAD_PCT, MAX_PRICE_AGE_SEC,
    CORRELATION_GUARD_ENABLED, MAX_NET_DIRECTIONAL_R,
    STRESS_SHOCK_PCT, MAX_STRESS_LOSS_R, BETA_LOOKBACK_BARS,
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
    allocated_r,
)
from trader import create_trader, PaperPortfolio
from portfolios import StrategyBook
from dashboard import build_layout
from sentiment import SentimentTracker
from database import (
    init_db, save_missed_signal, get_performance_summary,
    get_summary_excluding_top, get_recent_pnls,
    state_get, state_set_many, load_open_positions, save_open_positions,
    save_shadow_signal, save_equity_snapshot,
    get_pending_shadow_signals, update_shadow_outcome,
    get_week_realized_pnl,
)
from telegram_bot import TelegramBot
import momentum as momentum_mod
import strategies as strategies_mod
import correlation as corr

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


def drop_stale_prices(held: list, fresh: dict, prices: dict) -> list:
    """Taze fiyatı olmayan sembolleri `prices`'tan DÜŞÜR (yerinde değiştirir).

    Bayat fiyatla stop kararı vermek, kararı hiç vermemekten kötüdür: fiyat
    stop'u çoktan geçmişken bot "henüz geçmedi" sanır ve pozisyonu taşır.

    Bu kural ana döngünün içinde satır satır duruyordu ve bu yüzden hiçbir test
    ona dokunamıyordu — mutasyon denetimi kuralı bozduğunda tüm testler yeşil
    kalıyordu. Kural artık burada, çağrılabilir ve ölçülebilir hâlde.

    Dönen liste: düşürülen semboller (çağıran loglayabilsin diye).
    """
    dropped = []
    for sym in list(held):
        if sym not in fresh:
            prices.pop(sym, None)
            dropped.append(sym)
    return dropped


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

    n_restored = trader.restore_positions(load_open_positions("TREND"))

    # ── MOMENTUM: kendi portföyü ───────────────────────────────────────────
    # Gölge moddan çıkıp AYRI sanal sermayeyle işlem açıyor. TREND'in defterine
    # dokunulmuyor: onun state anahtarları ve pozisyonları olduğu gibi kaldı.
    #
    # Neden ayrı portföy: ortak kasada MOMENTUM'un zararı TREND'in sermayesini
    # küçültür, biri pozisyon limitini doldurunca diğeri sinyal üretse de işlem
    # açamaz. O zaman ölçtüğümüz şey "hangi strateji iyi" değil, "hangisi önce
    # davrandı" olurdu.
    mom_book = StrategyBook("MOMENTUM_FAST", PaperPortfolio(MOMENTUM_BALANCE))
    if PAPER_TRADING and MOMENTUM_ENABLED and not MOMENTUM_SHADOW_MODE:
        vardi = mom_book.restore()
        logger.info(
            f"MOMENTUM portföyü {'geri yüklendi' if vardi else 'ilk kez kuruldu'}: "
            f"{mom_book.portfolio.balance:.2f}$, "
            f"{len(mom_book.portfolio.positions)} açık pozisyon"
        )

    # ── Deney stratejileri: her biri KENDİ sanal sermayesiyle ───────────────
    # Hepsi TREND ile AYNI başlangıç sermayesi alır: karşılaştırma ancak eşit
    # koşulda anlamlıdır. Ortak olan piyasa verisi, çıkış motoru ve emir
    # simülasyonu; ayrı olan sermaye, pozisyon, PnL ve state.
    lab_books: dict = {}
    if PAPER_TRADING and EXPERIMENT_STRATEGIES_ENABLED:
        for _ad in strategies_mod.REGISTRY:
            _kitap = StrategyBook(_ad, PaperPortfolio(EXPERIMENT_BALANCE))
            _vardi = _kitap.restore()
            lab_books[_ad] = _kitap
            logger.info(
                f"{_ad} portföyü {'geri yüklendi' if _vardi else 'ilk kez kuruldu'}: "
                f"{_kitap.portfolio.balance:.2f}$, "
                f"{len(_kitap.portfolio.positions)} açık pozisyon"
            )

    def book_of(strategy: str):
        """Stratejinin portföyü. Bilinmeyen strateji TREND'e düşer.

        Gerçek modda çoklu portföy YOK: deney kâğıt üzerinde yürüyor ve gerçek
        para yolunu bu refactor'a maruz bırakmak gereksiz risk.
        """
        if (strategy == "MOMENTUM_FAST" and PAPER_TRADING
                and MOMENTUM_ENABLED and not MOMENTUM_SHADOW_MODE):
            return mom_book.portfolio
        if strategy in lab_books:
            return lab_books[strategy].portfolio
        return trader

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

    # Haftalık zarar limiti artık bellekteki sayaca değil, kapanmış işlemlere
    # bakıyor. Sayaç bir restart'ta sıfırlandığında bot KÂRDA olduğu bir haftada
    # kendini günlerce durdurmuştu; ölçüm bu sınıf hatayı yapısal olarak keser.
    kill_switch.set_weekly_pnl_source(get_week_realized_pnl)
    _wk_measured = get_week_realized_pnl()
    if _wk_measured is not None and abs(_wk_measured - kill_switch.weekly_pnl) > 0.01:
        logger.warning(
            f"Haftalık PnL sayacı sapmış: sayaç {kill_switch.weekly_pnl:+.2f}$, "
            f"ölçüm {_wk_measured:+.2f}$ — ölçüm esas alınıyor"
        )
        kill_switch.weekly_pnl = _wk_measured

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
            save_open_positions(trader.positions, strategy="TREND")
            # MOMENTUM ayrı defter: kendi satırlarına yazar, TREND'inkine dokunmaz.
            if mom_book.portfolio.positions or mom_book.portfolio.total_pnl:
                mom_book.save()
            for _k in lab_books.values():
                # Her defter AYRI kaydedilir; birinin hatası diğerlerini
                # engellemesin diye tek tek sarmalanıyor.
                try:
                    _k.save()
                except Exception:
                    logger.exception(f"{_k.name}: portföy kaydedilemedi")
        except Exception as e:
            logger.error(f"State kaydedilemedi: {e}")

    # ── Telegram bot + komutlar ────────────────────────────────────────────
    tg = TelegramBot()

    def _evaluate_shadow_signals(mdm) -> int:
        """
        Bekleyen gölge sinyalleri sonuçlandır.

        Momentum stratejisi gölge modda çalışıyor: sinyaller kaydediliyor ama
        işlem açılmıyor. Canlıya alma kararı bu kayıtların SONUCUNA bakılarak
        verilecek — ama sonuç hesaplayan kod hiç çağrılmıyordu, kayıtlar
        süresiz PENDING kalıyordu. Yani karar için gereken kanıt üretilmiyordu.

        Ölçüt basit ve stratejinin kendi çıkış mantığıyla uyumlu: sinyalden
        sonraki en uç lehte/aleyhte hareket. +1R'ye eşdeğer hareket önce
        geldiyse WIN, stop mesafesi önce geldiyse LOSS.
        """
        from config import SL_ATR_MULT
        pending = get_pending_shadow_signals(max_age_min=240)
        done = 0
        for row in pending:
            df = mdm.get_ohlcv(row["symbol"])
            if df is None or len(df) < 5:
                continue
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (TypeError, ValueError):
                continue
            after = df[df.index > ts] if hasattr(df.index, "__gt__") else None
            if after is None or len(after) < 3:
                continue

            entry = float(row["price"])
            if entry <= 0:
                continue
            hi = float(after["high"].max())
            lo = float(after["low"].min())
            if row["action"] == "BUY":
                peak_pct, trough_pct = (hi - entry) / entry, (lo - entry) / entry
            else:
                peak_pct, trough_pct = (entry - lo) / entry, (entry - hi) / entry

            # Stop mesafesi sinyaldeki ATR'den; yoksa muhafazakâr %2.
            feats = row.get("features") or {}
            atr_pct = float(feats.get("atr_pct", 0.01)) if isinstance(feats, dict) else 0.01
            stop_pct = max(atr_pct * SL_ATR_MULT, 0.005)

            if trough_pct <= -stop_pct:
                outcome = "LOSS"          # önce stop'a değdi varsayımı (muhafazakâr)
            elif peak_pct >= stop_pct:
                outcome = "WIN"
            else:
                continue                  # henüz sonuçlanmadı
            update_shadow_outcome(row["id"], outcome, peak_pct, trough_pct)
            done += 1
        if done:
            logger.info(f"Gölge sinyal değerlendirildi: {done} kayıt sonuçlandı")
        return done

    def _betas_for(symbols) -> dict:
        """
        Sembollerin BTC'ye betası — 5m getirilerinden.

        Veri yoksa 1.0 döner: bilinmeyen maruziyeti sıfır saymak, tam da
        korunmak istediğimiz riski görünmez kılardı.
        """
        out = {}
        try:
            btc = mdm.get_ohlcv("BTCUSDT")
            if btc is None or len(btc) < 30:
                return {s: 1.0 for s in symbols}
            btc_r = corr.returns(list(btc["close"])[-BETA_LOOKBACK_BARS:])
            for s in symbols:
                df = mdm.get_ohlcv(s)
                if df is None or len(df) < 30:
                    out[s] = 1.0
                    continue
                out[s] = corr.beta_to(
                    corr.returns(list(df["close"])[-BETA_LOOKBACK_BARS:]), btc_r
                )
        except Exception as e:
            logger.debug(f"Beta hesaplanamadı: {e}")
            return {s: 1.0 for s in symbols}
        return out

    def _equity():
        # DENETİM: burada canlı equity YENİDEN hesaplanıyordu ve aynı çift
        # sayma hatasını tekrarlıyordu (wallet balance bloke teminatı zaten
        # içerir). Tek kaynak trader.equity olmalı.
        return trader.equity

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

                # "Yaşıyorum" demek yetmiyor: bot 2,5 gün boyunca kill switch
                # yüzünden hiç işlem açmadığı hâlde bu mesaj her saat sağlıklı
                # görünüyordu. Nabız, KALP ATIYOR MU değil, İŞ YAPIYOR MU
                # sorusunu cevaplamalı — engel varsa sebebi burada görünür.
                try:
                    # Salt-okunur: nabız thread'i ana döngünün durumunu bozmamalı.
                    ok_now, why = kill_switch.blocking_reason(eq)
                except Exception as e:
                    ok_now, why = False, f"durum okunamadı ({e})"
                head = "💓 <b>Yaşıyorum</b>" if ok_now else "⛔️ <b>Yaşıyorum ama İŞLEM AÇMIYORUM</b>"
                blocked = "" if ok_now else f"\n<b>Sebep:</b> {why}"

                tg.send(
                    f"{head}{blocked}\n"
                    f"Equity: {eq:.2f}$ | PnL: {getattr(trader,'total_pnl',0):+.2f}$\n"
                    f"Açık: {len(trader.positions)} | "
                    f"Risk: {kill_switch.daily_risk_used_r:.1f}R/{DAILY_RISK_BUDGET_R:.0f}R\n"
                    f"Günlük: {kill_switch.daily_pnl:+.2f}$ ({kill_switch.daily_trades} işlem)\n"
                    f"Haftalık: {kill_switch.effective_weekly_pnl():+.2f}$"
                )

    threading.Thread(target=_heartbeat, daemon=True, name="heartbeat").start()

    console.print("[bold green]🚀 Bot başlatılıyor...[/bold green]")

    # ── Gerçek mod: spot coinleri USDT'ye çevir, Futures'a transfer ──────────
    from trader import SpotTrader, FuturesTrader
    from binance.client import Client as _Client
    from config import API_KEY as _KEY, API_SECRET as _SEC
    from config import TESTNET, AUTO_LIQUIDATE_SPOT

    # DENETİM BULGUSU (en tehlikeli): burası gerçek modda AÇILIŞTA spot
    # hesaptaki USDT/BNB dışındaki TÜM varlıkları satıyordu. Bayrak yok,
    # allowlist yok, onay yok. systemd `Restart=always` altında her çökme
    # bunu tekrarlıyordu — botla hiç ilgisi olmayan kişisel coinler de
    # tasfiye olabilirdi. Artık varsayılan KAPALI; yalnız açıkça istenirse
    # (`--prepare-capital` veya AUTO_LIQUIDATE_SPOT=True) çalışır.
    _prepare_capital = AUTO_LIQUIDATE_SPOT or "--prepare-capital" in sys.argv

    if isinstance(trader, (SpotTrader, FuturesTrader)) and not _prepare_capital:
        console.print(
            "[dim]Spot tasfiyesi atlandı (AUTO_LIQUIDATE_SPOT=False). "
            "Sermaye hazırlamak için: --prepare-capital[/dim]"
        )

    if isinstance(trader, (SpotTrader, FuturesTrader)) and _prepare_capital:
        cl = _Client(_KEY, _SEC, testnet=TESTNET)
        console.print("[yellow]⚠ Spot varlıklar USDT'ye çevriliyor...[/yellow]")
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
    _ks_last_reason   = ""
    _ks_last_log      = 0.0
    _guard_notified: dict = {}
    last_shadow_eval  = 0.0
    # "STRATEJI:sembol" -> o işleme ayrılan R (bütçe iadesini sınırlamak için).
    # Anahtar strateji adını TAŞIMALI: iki defter aynı sembolü tutabilir ve tek
    # anahtarla biri diğerinin tahsisini ezerdi. Ezilen tahsis sessizce
    # varsayılana (1.0R) düşer, yani risk bütçesi yanlış iade edilir.
    _alloc_of: dict = {
        f"TREND:{p}": pos.get("allocated_r", 1.0)
        for p, pos in getattr(trader, "positions", {}).items()
    }
    momentum_signals: list = []
    # Art arda kaç tur hata aldık. Sıfırlanması 'toparlandı' demektir.
    _cycle_errors     = 0

    def _watch_positions() -> None:
        """Açık pozisyonların stop/çıkış kontrolü — sinyal taramasından BAĞIMSIZ.

        Neden ayrı: stop kontrolü ana döngüye bağlıydı ve ana döngü
        `SIGNAL_INTERVAL` (30 sn) uyuyordu. Yani fiyat stop'u geçse bile bot
        bunu ortalama 15, en kötü 30 saniye sonra fark ediyordu; üstüne fiyat
        tazeliği 90 saniyeye kadar gevşekti. Ölçülen sonuç: `INITIAL_STOP` ile
        kapanan 32 işlemin 32'si de planlanan -1R'yi aştı (fazla zarar 119.50$).

        Sinyal taraması ağırdır (yüzlerce sembol, gösterge hesabı); onu
        hızlandırmak pahalı ve gereksiz. Pozisyon kontrolü ise yalnız açık
        sembollerin fiyatına bakar — ucuzdur. Bu yüzden pahalı tarama 30
        saniyede kalır, ucuz koruma sık çalışır.

        Tek iş parçacığında, ana döngünün bekleme aralığında çalışır: yeni
        eşzamanlılık (ve `trader.positions` üzerinde yarış) getirmez.
        """
        # Her defteri AYRI yönet. Ortak olan yalnız fiyat: aynı sembolü iki
        # strateji tutuyorsa fiyat bir kez çekilir, karar iki kez verilir.
        defterler = [("TREND", trader)]
        if PAPER_TRADING and MOMENTUM_ENABLED and not MOMENTUM_SHADOW_MODE:
            defterler.append(("MOMENTUM_FAST", mom_book.portfolio))
        defterler.extend((ad, k.portfolio) for ad, k in lab_books.items())

        semboller = {
            sym
            for _, pf in defterler
            for sym in getattr(pf, "positions", {})
        }
        if not semboller:
            return

        px = mdm.get_prices_for(sorted(semboller), max_age_seconds=MAX_PRICE_AGE_SEC)
        # Taze fiyat yoksa karar YOK. Bayat fiyatla stop kararı vermek,
        # kararı hiç vermemekten kötüdür.
        if not px:
            return

        for ad, pf in defterler:
            if not getattr(pf, "positions", None) or not hasattr(pf, "check_positions"):
                continue
            try:
                kapananlar = pf.check_positions(px)
            except Exception:
                # Bu fonksiyon hem normal turda hem HATA turunda çağrılıyor.
                # Buradan istisna kaçarsa hata işleyicisinin kendisi patlar ve
                # döngü yine ölür — koruma kendi koruduğu şeyi öldürmemeli.
                # Bir defterin hatası DİĞER defteri de yönetimsiz bırakmamalı.
                logger.exception(f"{ad}: check_positions başarısız")
                continue

            for sym, reason, pnl, r_mult in kapananlar:
                logger.info(f"KAPANDI [{ad}]: {sym} | {reason} | PnL:{pnl:+.2f}$ ({r_mult:+.2f}R)")
                kill_switch.record_trade_close(
                    pnl, _equity(), realized_r=r_mult, symbol=sym,
                    allocated=_alloc_of.pop(f"{ad}:{sym}", 1.0),
                )
                tg.notify_close(sym, "", px.get(sym, 0), pnl, reason)
                _save_state()

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while running:
            try:
                t0 = time.perf_counter()
                _last_loop_ts = time.time()

                prices = mdm.get_all_prices()

                # Açık pozisyonlar için fiyat tazeliği garantisi (bayat fiyatla
                # asla stop kontrolü yapma — 2026-08-07 WS olayı)
                held_positions = getattr(trader, "positions", {})
                if held_positions:
                    fresh = mdm.get_prices_for(
                        list(held_positions.keys()), max_age_seconds=MAX_PRICE_AGE_SEC
                    )
                    prices.update(fresh)
                    # DENETİM BULGUSU: REST yenilemesi BAŞARISIZ olursa sözlükteki
                    # eski WebSocket fiyatı yerinde kalıyor ve stop motoru onu
                    # kullanmaya devam ediyordu. Bayat fiyatla stop kararı vermek,
                    # kararı hiç vermemekten kötüdür.
                    #
                    # 2026-08-18: bu koruma yazıldığı günden beri HİÇ çalışmamıştı.
                    # `get_prices_for` varsayılan 90 saniyelik eşikle çağrılıyordu,
                    # yani 89 saniyelik fiyat `fresh` içinde dönüyordu; koruma ise
                    # yalnız `fresh` DIŞINDA kalanlara bakıyordu. Korumanın kendi
                    # eşiği (60s) getirme eşiğinden (90s) sıkı olduğu için elemek
                    # istediği fiyatlar ona hiç ulaşmadı. Artık eşik tek: `fresh`
                    # değilse karar verilmez — "taze değil" ile "kullanılabilir"
                    # arasında boşluk bırakmıyoruz.
                    for _sym in drop_stale_prices(list(held_positions), fresh, prices):
                        logger.warning(
                            f"{_sym}: fiyat bayat ({mdm.price_age(_sym):.0f}s) — "
                            f"bu turda çıkış değerlendirilmiyor"
                        )
                if hasattr(trader, "mark"):
                    trader.mark(prices)

                if hasattr(trader, "sync_positions"):
                    trader.sync_positions()
                    # Borsa stopuyla kapanan işlemler kill switch'e İŞLENMELİ.
                    # Gerçek modda en sık kapanış yolu budur; buraya bağlanmazsa
                    # günlük zarar limiti ve ardışık kayıp sayacı kör kalır.
                    for sym, reason, pnl, r_mult, alloc in getattr(trader, "exchange_closed", []):
                        logger.info(f"BORSA KAPANIŞI: {sym} | {reason} | {pnl:+.2f}$ ({r_mult:+.2f}R)")
                        kill_switch.record_trade_close(
                            pnl, _equity(), realized_r=r_mult, symbol=sym, allocated=alloc,
                        )
                        tg.notify_close(sym, "", 0.0, pnl, reason)
                        _alloc_of.pop(f"TREND:{sym}", None)
                    if getattr(trader, "exchange_closed", None):
                        trader.exchange_closed.clear()
                        _save_state()

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
                        kill_switch.record_trade_close(
                            pnl, _equity(), realized_r=r_mult, symbol=sym,
                            allocated=_alloc_of.pop(f"TREND:{sym}", 1.0),
                        )
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
                # Tepe noktası açık pozisyon kârıyla da ilerlemeli.
                kill_switch.observe_equity(equity)
                can_trade, ks_reason = kill_switch.can_trade(equity)

                # ── KÂĞIT MODDA KİLL SWITCH DURDURMAZ, KAYDEDER ──────────────
                # 2026-08-18 canlı testte yakalandı: kill switch GLOBAL. TREND'in
                # 4 ardışık zararı BEŞ stratejinin hepsini birden durdurdu —
                # henüz tek işlem bile açmamış olanları dahil. İki ayrı sorun:
                #
                #  1) DENEY GEÇERSİZLEŞİR. Bir stratejinin kötü serisi
                #     diğerlerinin veri toplamasını kesiyorsa ölçtüğümüz şey
                #     stratejiler değil, aralarındaki bulaşma olur.
                #  2) ÖLÇÜM SANSÜRLENİR. Zarar ederken durdurmak kötü sonuç
                #     kuyruğunu keser ve stratejiyi olduğundan iyi gösterir.
                #
                # Kâğıtta gerçek para riski YOK; korunacak sermaye de yok. O
                # yüzden burada limit UYGULANMAZ, olay KAYDEDİLİR — bilgi
                # kaybolmuyor, veri akışı kesilmiyor. Gerçek modda limit aynen
                # bağlayıcı (bkz. strategy_lab.risk_action: bilinmeyen mod bile
                # güvenli tarafa, yani durdurmaya düşer).
                if not can_trade and PAPER_TRADING:
                    if ks_reason != _ks_last_reason or time.time() - _ks_last_log >= 900:
                        logger.warning(
                            f"KİLL SWİTCH (kâğıt — KAYDEDİLDİ, durdurulmadı): {ks_reason}"
                        )
                        _ks_last_reason = ks_reason
                        _ks_last_log = time.time()
                        try:
                            from strategy_lab import RiskBreach, record_risk_event
                            import sqlite3 as _sq
                            from database import DB_PATH as _DBP
                            _con = _sq.connect(str(_DBP), timeout=10)
                            record_risk_event(
                                _con,
                                RiskBreach("ALL", "KILL_SWITCH", ks_reason, datetime.now()),
                                "paper",
                            )
                            _con.close()
                        except Exception:
                            logger.exception("Risk olayı kaydedilemedi")
                    can_trade = True

                if not can_trade:
                    # Aynı satırı her turda yazmak logu şişiriyordu: tek bir
                    # kilitte 2,5 günde 7.154 özdeş satır. Sebep değiştiğinde ya da
                    # 15 dakikada bir yaz — kilidin sürdüğü görünür kalsın ama log
                    # okunabilir olsun (bu sunucuda log şişmesi daha önce yaşandı).
                    _now_ts = time.time()
                    if (ks_reason != _ks_last_reason
                            or _now_ts - _ks_last_log >= 900):
                        logger.warning(f"KİLL SWİTCH: {ks_reason}")
                        _ks_last_reason = ks_reason
                        _ks_last_log = _now_ts
                    if not _ks_notified:
                        tg.notify_kill_switch(ks_reason)
                        _ks_notified = True
                else:
                    _ks_notified = False

                # ── Koruma bütünlüğü ──────────────────────────────────────────
                # Korumasız veya sahipsiz pozisyon varsa yeni işlem AÇILMAZ. Borsa
                # tarafındaki koruma, botun belleğindeki stop'tan daha önemlidir:
                # süreç ölürse bellekteki stop da ölür, borsadaki emir kalır.
                guard_issues = {
                    **{s: f"korumasız ({r})" for s, r in getattr(trader, "unprotected", {}).items()},
                    **{s: f"sahipsiz ({r})" for s, r in getattr(trader, "orphans", {}).items()},
                    # Borsada gerçekleşmiş ama yerel kayda işlenmemiş dolum /
                    # doğrulanamamış kapanış PnL'i. Kayıt ile borsa ayrıştığı için
                    # pozisyon boyutu da risk hesabı da güvenilmezdir.
                    **{s: f"uzlaştırma gerekli ({r})"
                       for s, r in getattr(trader, "reconcile_needed", {}).items()},
                    **{s: f"stop doğrulanamıyor ({r})"
                       for s, r in getattr(trader, "protection_unknown", {}).items()},
                    **{s: "emir akıbeti bilinmiyor"
                       for s in getattr(trader, "pending_unknown", {})},
                }
                if hasattr(trader, "position_sync_ok") and not trader.position_sync_ok:
                    guard_issues["SYSTEM/POSITION_SYNC"] = "borsa pozisyonları doğrulanamıyor"
                if hasattr(trader, "pending_state_ok") and not trader.pending_state_ok:
                    guard_issues["SYSTEM/PENDING_STATE"] = "emir karantinası okunamıyor/yazılamıyor"
                if hasattr(trader, "reconcile_state_ok") and not trader.reconcile_state_ok:
                    guard_issues["SYSTEM/RECONCILE_STATE"] = "uzlaştırma durumu okunamıyor/yazılamıyor"
                if getattr(trader, "equity_stale", False):
                    guard_issues["SYSTEM/EQUITY"] = "canlı equity doğrulanamıyor"
                if guard_issues:
                    can_trade = False
                    ks_reason = "Koruma bütünlüğü bozuk: " + ", ".join(
                        f"{s} {r}" for s, r in guard_issues.items()
                    )
                    logger.error(ks_reason)
                    if guard_issues != _guard_notified:
                        tg.send(
                            "🚨 KORUMA UYARISI\n\n"
                            + "\n".join(f"• {s}: {r}" for s, r in guard_issues.items())
                            + "\n\nYeni işlem açılmayacak. Binance'i elle kontrol et."
                        )
                        _guard_notified = dict(guard_issues)
                elif _guard_notified:
                    tg.send("✅ Koruma bütünlüğü normale döndü.")
                    _guard_notified = {}

                # ── İşlem kararları ───────────────────────────────────────────
                positions = getattr(trader, "positions", {})

                # MOMENTUM canlı modda ise sinyalleri TREND sinyallerinin önüne al
                candidates = []
                if MOMENTUM_ENABLED and not MOMENTUM_SHADOW_MODE:
                    candidates.extend([
                        s for s in momentum_signals
                        if s.action in ("BUY", "SELL") and s.score >= MOMENTUM_MIN_SCORE
                    ])
                # ── Deney stratejileri ─────────────────────────────────
                # Ayrı tarama: kendi sinyallerini üretirler, kendi
                # portföylerine açarlar. Hata olursa DİĞER stratejiler ve
                # TREND etkilenmez — deneyin veri akışı kesilmemeli.
                if lab_books:
                    try:
                        lab_signals = strategies_mod.scan(
                            ready[:60],
                            lambda sym: mdm.get_ohlcv(sym),
                            lambda sym: mdm.get_price(sym),
                            btc_trend=btc_trend,
                        )
                        for ls in lab_signals:
                            if ls.action == "HOLD":
                                if ls.reject_reason:
                                    save_missed_signal(
                                        ls.symbol, ls.action, ls.score, ls.confidence,
                                        f"[{ls.strategy}] {ls.reject_reason}",
                                        btc_trend, ls.market_regime,
                                    )
                                continue
                            if ls.score >= EXPERIMENT_MIN_SCORE:
                                candidates.append(ls)
                    except Exception as exc:
                        logger.warning(f"Deney taraması hatası: {exc}")

                candidates.extend(signals)

                for sig in candidates:
                    if not running:
                        break

                    strategy = getattr(sig, "strategy", "TREND")
                    # Bu adayın defteri. Pozisyon limiti, açık risk ve marj
                    # kontrolleri KENDİ portföyüne göre yapılır; aksi hâlde
                    # MOMENTUM, TREND'in doluluğu yüzünden işlem açamaz ve
                    # ölçtüğümüz şey stratejinin kendisi olmaktan çıkar.
                    pf = book_of(strategy)
                    positions = getattr(pf, "positions", {})
                    equity = (
                        pf.balance + sum(x.get("margin", 0.0) for x in positions.values())
                        if isinstance(pf, PaperPortfolio) else _equity()
                    )
                    is_trade_signal = (
                        sig.action in ("BUY", "SELL")
                        and getattr(sig, "confidence", 0) >= (
                            MIN_CONFIDENCE if strategy == "TREND"
                            else (EXPERIMENT_MIN_SCORE / 100 if strategy in lab_books
                                  else MOMENTUM_MIN_SCORE / 100)
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

                    # Bu adayın gerçek risk ağırlığı. Korelasyon kapısı da bunu
                    # kullanmalı: %0.4 riskli işlemle %1.2 riskli işlem aynı
                    # maruziyeti üretmez.
                    _risk_pct_probe = dynamic_risk_pct(get_recent_pnls(30), sig.score)
                    alloc_r = allocated_r(_risk_pct_probe)

                    # Gerçek korelasyon kapısı — sayı değil, BİRLİKTE HAREKET.
                    # Yön limiti (MAX_CORRELATED_POSITIONS) 4 alt long'a izin verir;
                    # BTC %5 düşerse dördü birden stop olur. Bu kapı onu ölçer.
                    if CORRELATION_GUARD_ENABLED and positions:
                        betas = _betas_for(list(positions.keys()) + [sig.symbol])
                        ok_corr, corr_reason = corr.can_add_correlated(
                            positions, sig.symbol, sig.action, alloc_r, betas,
                            max_net_r=MAX_NET_DIRECTIONAL_R,
                            max_stress_r=MAX_STRESS_LOSS_R,
                            shock_pct=STRESS_SHOCK_PCT,
                        )
                        if not ok_corr:
                            save_missed_signal(
                                sig.symbol, sig.action, sig.score, sig.confidence,
                                corr_reason, btc_trend, sig.market_regime,
                            )
                            continue

                    # Momentum stratejisi kendi izole bütçesini aşamaz
                    if strategy == "MOMENTUM_FAST":
                        ok_mom, mom_reason = kill_switch.can_trade(equity, strategy="MOMENTUM_FAST")
                        # DENETİM: alt bütçe ADAY DAHİL EDİLMEDEN kontrol
                        # ediliyordu — kalan 0.4R iken 1.3R'lik aday kabul
                        # edilebiliyordu. Ana bütçede bu kontrol vardı, alt
                        # bütçede atlanmıştı.
                        if ok_mom and kill_switch.momentum_budget_remaining() < alloc_r:
                            ok_mom = False
                            mom_reason = (
                                f"Momentum bütçesi yetersiz "
                                f"({kill_switch.momentum_budget_remaining():.2f}R < {alloc_r:.2f}R)"
                            )
                        if PAPER_TRADING:
                            # Kâğıtta alt bütçe de bağlamaz; olay kill switch
                            # kaydında zaten görünüyor.
                            ok_mom = True
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
                    risk_pct = _risk_pct_probe
                    open_margin = sum(p.get("margin", 0.0) for p in positions.values())

                    # DENETİM: `can_trade` aday DÖNGÜSÜNDEN ÖNCE bir kez
                    # çalışıyordu. Aynı taramada arka arkaya açılan pozisyonlar
                    # günlük 8R bütçesini aşabiliyordu. Her adaydan hemen önce,
                    # o adayın GERÇEK riskiyle yeniden kontrol ediliyor.
                    ok_budget, budget_reason = kill_switch.can_trade(equity, strategy=strategy)
                    # Kâğıtta risk bütçesi de ORTAK sayaçtan geliyor: bir
                    # stratejinin harcadığı bütçe diğerlerini kilitliyordu.
                    # Portföyleri ayırıp bütçeyi ortak bırakmak, ayrımı yarım
                    # bırakmak olurdu — deney yine bulaşır.
                    _butce_yetersiz = kill_switch.risk_budget_remaining() < alloc_r
                    if PAPER_TRADING and (not ok_budget or _butce_yetersiz):
                        ok_budget, _butce_yetersiz = True, False
                    if not ok_budget or _butce_yetersiz:
                        save_missed_signal(
                            sig.symbol, sig.action, sig.score, sig.confidence,
                            budget_reason if not ok_budget else
                            f"Bütçe yetersiz ({kill_switch.risk_budget_remaining():.1f}R < {alloc_r:.1f}R)",
                            btc_trend, sig.market_regime,
                        )
                        continue

                    ok_port, port_reason = can_add_position(positions, equity, new_risk_r=alloc_r)
                    if not ok_port:
                        save_missed_signal(
                            sig.symbol, sig.action, sig.score, sig.confidence,
                            port_reason, btc_trend, sig.market_regime,
                        )
                        continue

                    # Bayat fiyatla pozisyon açma — sinyal üretimi ile emir arasında
                    # WebSocket düşmüş olabilir.
                    fresh_px = mdm.get_price(sig.symbol)
                    if fresh_px is None:
                        # DENETİM BULGUSU (2026-08-16): açık pozisyonlar için REST
                        # yedeği vardı, GİRİŞ için yoktu. WS kütüphanesi sürüm
                        # uyumsuzluğu yüzünden akış dakikalar boyunca kesilebiliyor;
                        # o pencerede her sinyal "Fiyat bayat" diye çöpe gidiyordu.
                        # Sinyal nadirdir — tek bir REST çağrısı ucuz, kaçan fırsat
                        # pahalı. Tazelik kuralı korunuyor: REST de veremezse giriş
                        # YİNE yapılmaz.
                        fresh_px = mdm.get_prices_for(
                            [sig.symbol], max_age_seconds=MAX_PRICE_AGE_SEC
                        ).get(sig.symbol)
                    if fresh_px is None:
                        save_missed_signal(
                            sig.symbol, sig.action, sig.score, sig.confidence,
                            f"Fiyat bayat ({mdm.price_age(sig.symbol):.0f}s)",
                            btc_trend, sig.market_regime,
                        )
                        continue

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

                    ok, msg = pf.open_position(
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
                        kill_switch.record_trade_open(equity, risk_r=alloc_r, strategy=strategy)
                        if sig.symbol in pf.positions:
                            pf.positions[sig.symbol]["allocated_r"] = alloc_r
                        # Anahtar strateji ADIYLA: iki defter aynı sembolü
                        # tutabilir, tek anahtar kullanılsa biri diğerinin
                        # risk tahsisini ezerdi.
                        _alloc_of[f"{strategy}:{sig.symbol}"] = alloc_r
                        positions = pf.positions
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

                # ── Gölge sinyal değerlendirmesi ───────────────────────────────
                # DENETİM: gölge sinyaller kaydediliyor ama sonuçları HİÇ
                # değerlendirilmiyordu; kayıtlar sonsuza kadar PENDING kalıyordu.
                # Yani momentum stratejisini canlıya almak için gereken kanıt
                # hiç üretilmiyordu.
                if time.time() - last_shadow_eval >= 300:
                    try:
                        _evaluate_shadow_signals(mdm)
                    except Exception as e:
                        logger.debug(f"Gölge değerlendirme hatası: {e}")
                    last_shadow_eval = time.time()

                # ── Periyodik state kaydı + equity örneklemesi ─────────────────
                if time.time() - last_state_save >= 60:
                    _save_state()
                    # Gerçek drawdown yalnız kapanmış işlemlerden ölçülemez; açık
                    # pozisyonun anlık zararını görebilmek için düzenli örnek şart.
                    save_equity_snapshot(
                        equity=_equity(),
                        balance=trader.balance if isinstance(trader, PaperPortfolio)
                                else trader.get_balance(),
                        open_positions=len(getattr(trader, "positions", {})),
                        open_risk_r=portfolio_open_risk_r(getattr(trader, "positions", {})),
                        mode="paper" if PAPER_TRADING else "live",
                    )
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
                # Beklerken uyumak yerine pozisyonları izle: bekleme süresi
                # POSITION_CHECK_INTERVAL'lık dilimlere bölünür ve her dilimde
                # yalnız açık pozisyonların stopları kontrol edilir. Sinyal
                # taraması yine SIGNAL_INTERVAL'da bir çalışır.
                _slept = 0.0
                while running and _slept < sleep_for:
                    _slice = min(POSITION_CHECK_INTERVAL, sleep_for - _slept)
                    time.sleep(_slice)
                    _slept += _slice
                    if running:
                        _watch_positions()
            except Exception as exc:
                # ── DÖNGÜ ÖLMEZ ────────────────────────────────────────────
                # Eskiden gövdede genel istisna koruması YOKTU: tek bir
                # beklenmedik hata (bozuk API cevabı, bir göstergede KeyError,
                # geçici ağ kesintisi) süreci öldürüyordu. systemd 15 saniye
                # sonra yeniden başlatıyor, üstüne ~35 saniye piyasa verisi
                # ısınması biniyordu — yani açık pozisyonlar yaklaşık BİR
                # DAKİKA boyunca stopsuz kalıyordu. Ölüm sessizdi de: dışarıdan
                # "servis yeniden başladı" görünüyordu, "pozisyonlar korumasız
                # kaldı" görünmüyordu.
                _cycle_errors += 1
                logger.error(
                    f"Döngü hatası ({_cycle_errors}. kez): "
                    f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                # İlk hatada ve her 20 hatada bir haber ver; her turda bildirim
                # göndermek gürültü üretir ve gürültü görmezden gelinir.
                if _cycle_errors == 1 or _cycle_errors % 20 == 0:
                    try:
                        tg.send(
                            f"⚠️ <b>DÖNGÜ HATASI</b> ({_cycle_errors}. kez)\n"
                            f"{type(exc).__name__}: {str(exc)[:200]}\n"
                            f"Bot çalışmaya devam ediyor; stop kontrolü sürüyor."
                        )
                    except Exception:
                        pass

                # EN ÖNEMLİSİ: tarama çökse bile STOP KONTROLÜ SÜRER.
                # Sinyal taraması pahalı ve kırılgan, stop kontrolü ucuz ve
                # hayati. Pahalı olanın hatası ucuz olanı durdurmamalı.
                try:
                    _watch_positions()
                except Exception:
                    logger.exception("Stop kontrolü de başarısız")

                # Sıcak döngü olmasın: art arda hata varsa yavaşla (üst sınır
                # SIGNAL_INTERVAL — bekleme süresiz uzayıp bot fiilen ölmesin).
                time.sleep(min(2.0 * _cycle_errors, float(SIGNAL_INTERVAL)))
            else:
                if _cycle_errors:
                    logger.info(f"Döngü toparlandı ({_cycle_errors} hatadan sonra)")
                    _cycle_errors = 0

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
