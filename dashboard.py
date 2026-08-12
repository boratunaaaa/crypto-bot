"""Terminal dashboard."""

from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

console = Console()


def build_layout(
    signals, positions, balance, total_pnl,
    trade_log, paper, loop_ms=0, ready_count=0,
    total_count=0, win_rate=0.0, initial_balance=1000.0,
    kill_switch=None, btc_trend="SIDEWAYS", sentiment=None,
) -> Layout:

    # ── Portföy hesapla ──────────────────────────────────────────────────────
    open_margin   = sum(p["margin"] for p in positions.values())
    total_value   = balance + open_margin          # nakit + kilitli marjin
    net_change    = total_value - initial_balance  # başlangıçtan fark
    net_pct       = (net_change / initial_balance * 100) if initial_balance else 0.0
    unrealized    = total_value - initial_balance - total_pnl  # açık poz tahmini

    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=3),
        Layout(name="stats",     size=7),
        Layout(name="killzone",  size=4),
        Layout(name="body"),
        Layout(name="positions", size=12),
        Layout(name="footer",    size=3),
    )

    # ── Header ───────────────────────────────────────────────────────────────
    mode    = "[yellow]PAPER[/yellow]" if paper else "[red bold]GERÇEK[/red bold]"
    header  = (
        f"  🚀 Maksimum Kazanç Botu  |  {mode}  |  "
        f"{datetime.now().strftime('%H:%M:%S')}  |  "
        f"Tarama: {loop_ms:.0f}ms  |  "
        f"Veri: {ready_count}/{total_count}"
    )
    layout["header"].update(Panel(Text.from_markup(header), box=box.SIMPLE))

    # ── Finansal özet ────────────────────────────────────────────────────────
    pnl_c    = "green" if total_pnl  >= 0 else "red"
    net_c    = "green" if net_change >= 0 else "red"
    wrate_c  = "green" if win_rate   >= 0.5 else "red"
    pct_sign = "+" if net_pct >= 0 else ""
    pnl_sign = "+" if total_pnl >= 0 else ""

    stats_table = Table(box=box.SIMPLE, expand=True, show_header=False, padding=(0, 2))
    stats_table.add_column(justify="left",  ratio=1)
    stats_table.add_column(justify="left",  ratio=1)
    stats_table.add_column(justify="left",  ratio=1)
    stats_table.add_column(justify="left",  ratio=1)

    stats_table.add_row(
        Text.from_markup(f"💰 Başlangıç\n[dim]{initial_balance:.2f} USDT[/dim]"),
        Text.from_markup(f"🏦 Toplam Değer\n[{net_c}]{total_value:.2f} USDT[/{net_c}]"),
        Text.from_markup(f"📈 Net Değişim\n[{net_c}]{pct_sign}{net_pct:.2f}%  ({pnl_sign}{net_change:.2f}$)[/{net_c}]"),
        Text.from_markup(f"✅ WinRate\n[{wrate_c}]{win_rate*100:.0f}%[/{wrate_c}]  (Kapalı PnL: [{pnl_c}]{pnl_sign}{total_pnl:.2f}$[/{pnl_c}])"),
    )
    stats_table.add_row(
        Text.from_markup(f"💵 Nakit\n[cyan]{balance:.2f} USDT[/cyan]"),
        Text.from_markup(f"🔒 Açık Marjin\n[yellow]{open_margin:.2f} USDT[/yellow]  ({len(positions)} poz.)"),
        Text.from_markup(f"📊 Bakiye\n[dim]Nakit + Marjin = {total_value:.2f}$[/dim]"),
        Text.from_markup(f"⚡ Kapalı PnL\n[{pnl_c}]{pnl_sign}{total_pnl:.2f} USDT[/{pnl_c}]"),
    )
    layout["stats"].update(Panel(stats_table, title="💹 Portföy Özeti"))

    # ── Kill Switch durumu ───────────────────────────────────────────────────
    ks = kill_switch
    if ks is not None:
        can_trade, ks_reason = ks.can_trade(balance)
        ks_status = "[green]✅ AKTIF[/green]" if can_trade else f"[red bold]🚫 DURDURULDU — {ks_reason}[/red bold]"
        daily_pnl_c = "green" if ks.daily_pnl >= 0 else "red"
        btc_icon = {"UP": "⬆ YUKARI", "DOWN": "⬇ AŞAĞI", "SIDEWAYS": "➡ YATAY"}.get(btc_trend, "?")
        btc_c    = "green" if btc_trend == "UP" else ("red" if btc_trend == "DOWN" else "yellow")

        ks_table = Table(box=box.SIMPLE, expand=True, show_header=False, padding=(0, 2))
        ks_table.add_column(ratio=1)
        ks_table.add_column(ratio=1)
        ks_table.add_column(ratio=1)
        ks_table.add_column(ratio=2)
        fg_str = ""
        if sentiment is not None:
            fv = sentiment.value
            fl = sentiment.label
            fc = "green" if fv < 40 else ("red" if fv > 60 else "yellow")
            fg_str = f"  |  F&G: [{fc}]{fv} {fl}[/{fc}]"

        ks_table.add_row(
            Text.from_markup(f"Bot Durumu: {ks_status}"),
            Text.from_markup(f"Günlük PnL: [{daily_pnl_c}]{ks.daily_pnl:+.2f}$[/{daily_pnl_c}]  ({ks.daily_trades} işlem)"),
            Text.from_markup(f"Üst üste zarar: [{'red' if ks.consecutive_losses>0 else 'green'}]{ks.consecutive_losses}/3[/]"),
            Text.from_markup(f"BTC: [{btc_c}]{btc_icon}[/{btc_c}]{fg_str}"),
        )
        layout["killzone"].update(Panel(ks_table, title="🛡 Risk Kontrol Merkezi"))
    else:
        layout["killzone"].update(Panel("", box=box.SIMPLE))

    # ── Sinyal tablosu ───────────────────────────────────────────────────────
    sig_table = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False)
    sig_table.add_column("Coin",    style="cyan", width=8,  no_wrap=True)
    sig_table.add_column("Fiyat",   justify="right", width=11)
    sig_table.add_column("RSI",     justify="right", width=5)
    sig_table.add_column("ATR%",    justify="right", width=6)
    sig_table.add_column("Trend",   width=8)
    sig_table.add_column("Rejim",   width=8)
    sig_table.add_column("Sinyal",  justify="center", width=10)
    sig_table.add_column("Skor",    justify="right", width=6)
    sig_table.add_column("SL",      justify="right", width=11)
    sig_table.add_column("TP",      justify="right", width=11)
    sig_table.add_column("Neden",   overflow="fold")

    trend_icon = {"UP": "⬆", "DOWN": "⬇", "SIDEWAYS": "➡"}

    for s in signals[:25]:
        if s.action == "BUY":
            sig_txt = Text("🟢 AL",   style="green bold")
        elif s.action == "SELL":
            sig_txt = Text("🔴 SAT",  style="red bold")
        else:
            sig_txt = Text("⚪",      style="dim")

        rsi_c = "green" if s.rsi < 40 else ("red" if s.rsi > 60 else "white")

        regime_c = {"TREND": "green", "RANGE": "yellow", "CHAOTIC": "red"}.get(
            getattr(s, "market_regime", ""), "dim")
        sig_table.add_row(
            s.symbol.replace("USDT", ""),
            f"{s.price:,.5g}",
            Text(f"{s.rsi:.0f}", style=rsi_c),
            f"{s.atr_pct*100:.2f}%",
            f"{trend_icon.get(s.trend,'?')} {s.trend[:4]}",
            Text(getattr(s, "market_regime", "?")[:5], style=regime_c),
            sig_txt,
            f"{s.score}",
            f"{s.sl:,.5g}" if s.sl else "—",
            f"{s.tp:,.5g}" if s.tp else "—",
            s.reason,
        )

    layout["body"].update(
        Panel(sig_table, title="📊 Sinyaller (100 coin | ilk 25 | 5m+15m+1h+4h MTF | 0-100 puan)")
    )

    # ── Pozisyonlar ──────────────────────────────────────────────────────────
    pos_table = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False)
    pos_table.add_column("Coin",    style="cyan", width=8)
    pos_table.add_column("Yön",     width=10)
    pos_table.add_column("Giriş",   justify="right", width=11)
    pos_table.add_column("Marjin",  justify="right", width=8)
    pos_table.add_column("Kaldıraç",justify="right", width=9)
    pos_table.add_column("SL",      justify="right", width=11)
    pos_table.add_column("TP",      justify="right", width=11)

    if positions:
        for sym, pos in positions.items():
            st = Text("🟢 LONG" if pos["side"]=="BUY" else "🔴 SHORT",
                      style="green" if pos["side"]=="BUY" else "red")
            pos_table.add_row(
                sym.replace("USDT",""),
                st,
                f"{pos['entry_price']:,.5g}",
                f"{pos['margin']:.1f}$",
                f"{pos['leverage']}x",
                f"{pos['stop_loss']:,.5g}",
                f"{pos['take_profit']:,.5g}",
            )
    else:
        pos_table.add_row("—","—","—","—","—","—","—")

    layout["positions"].update(Panel(pos_table, title="💼 Açık Pozisyonlar"))

    # ── Footer ───────────────────────────────────────────────────────────────
    last  = list(reversed(trade_log[-4:])) if trade_log else []
    parts = []
    for t in last:
        pnl_s = ""
        if "pnl" in t:
            c = "green" if t["pnl"] >= 0 else "red"
            pnl_s = f" [{c}]{t['pnl']:+.2f}$[/{c}]"
        parts.append(
            f"[dim]{t['time'].strftime('%H:%M:%S')}[/dim] "
            f"[cyan]{t['symbol']}[/cyan] {t['action']} @{t['price']:.5g}{pnl_s}"
        )
    footer = "   |   ".join(parts) if parts else "Henüz işlem yok"
    layout["footer"].update(Panel(Text.from_markup(f"Son işlemler: {footer}"), box=box.SIMPLE))

    return layout
