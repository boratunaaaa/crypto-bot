"""
Risk yönetimi — sıfır borç garantisi + kill switch sistemi.

Temel prensipler:
1. Isolated margin → bir pozisyon batsın, hesap etkilenmesin
2. SL her zaman liquidasyon fiyatının çok üzerinde → liquidasyon olmaz
3. Risk-based position sizing → her işlemde aynı USDT riskle gir
4. Kill switch → günlük/haftalık zarar limiti veya üst üste 3 zarar → dur
"""

from datetime import datetime

from config import (
    TRAIL_AFTER_TP,
    KILL_SWITCH_COOLDOWN_HOURS,
    MAX_CAPITAL_RISK_PCT, MAX_POSITION_PCT,
    MAX_OPEN_POSITIONS,
    LEV_WEAK, LEV_MEDIUM, LEV_STRONG,
    LIQ_SAFETY_BUFFER,
    MAX_SL_PCT, MIN_MARGIN_USDT,
    RISK_PER_TRADE_PCT,
    DAILY_LOSS_LIMIT_PCT, WEEKLY_LOSS_LIMIT_PCT,
    MAX_CONSECUTIVE_LOSSES, MAX_DAILY_TRADES,
    DYNAMIC_RISK_ENABLED, RISK_MIN_PCT, RISK_MAX_PCT, DYNAMIC_RISK_LOOKBACK,
)


class KillSwitchTracker:
    """Günlük/haftalık zarar limiti ve üst üste zarar takibi."""

    def __init__(self, initial_balance: float = 0.0):
        self.daily_pnl          = 0.0
        self.weekly_pnl         = 0.0
        self.daily_trades       = 0
        self.consecutive_losses = 0
        self.daily_start_balance= initial_balance
        self._last_day  = datetime.now().date()
        self._last_week = datetime.now().isocalendar()[1]
        self._halted_at: datetime | None = None  # soğuma sayacı

    def _reset_if_needed(self, balance: float):
        today = datetime.now().date()
        if today != self._last_day:
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.daily_start_balance = balance
            self._last_day = today
        week = datetime.now().isocalendar()[1]
        if week != self._last_week:
            self.weekly_pnl  = 0.0
            self._last_week  = week

    def initialize(self, balance: float):
        self.daily_start_balance = balance

    def record_trade_open(self, balance: float):
        self._reset_if_needed(balance)
        self.daily_trades += 1

    def record_trade_close(self, pnl: float, balance: float):
        self._reset_if_needed(balance)
        self.daily_pnl  += pnl
        self.weekly_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES and self._halted_at is None:
                self._halted_at = datetime.now()
        else:
            self.consecutive_losses = 0
            self._halted_at = None

    def can_trade(self, balance: float) -> tuple[bool, str]:
        self._reset_if_needed(balance)
        ref = self.daily_start_balance if self.daily_start_balance > 0 else balance

        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            # Soğuma dolduysa sayacı sıfırla ve devam et. Bu olmadan kill switch
            # KALICI oluyordu: sayaç yalnızca kârlı işlemle sıfırlanıyor, ama bot
            # durunca işlem açamıyor, dolayısıyla kârlı işlem hiç gelemiyordu.
            if self._halted_at is not None:
                elapsed_h = (datetime.now() - self._halted_at).total_seconds() / 3600
                if elapsed_h >= KILL_SWITCH_COOLDOWN_HOURS:
                    self.consecutive_losses = 0
                    self._halted_at = None
                    return True, "OK (soğuma doldu, yeniden başlandı)"
                kalan = KILL_SWITCH_COOLDOWN_HOURS - elapsed_h
                return False, (
                    f"Üst üste {self.consecutive_losses} zarar — "
                    f"{kalan:.1f} saat sonra yeniden denenecek"
                )
            return False, f"Üst üste {self.consecutive_losses} zarar — bot durdu"
        if self.daily_trades >= MAX_DAILY_TRADES:
            return False, f"Günlük işlem limiti ({self.daily_trades}/{MAX_DAILY_TRADES})"
        if self.daily_pnl <= -(ref * DAILY_LOSS_LIMIT_PCT):
            return False, f"Günlük zarar limiti ({self.daily_pnl:.2f}$)"
        if self.weekly_pnl <= -(ref * WEEKLY_LOSS_LIMIT_PCT):
            return False, f"Haftalık zarar limiti ({self.weekly_pnl:.2f}$)"
        return True, "OK"


def dynamic_risk_pct(recent_pnls: list[float]) -> float:
    """
    Son N işlemin PnL listesine göre risk yüzdesini otomatik ayarla.

    İyi gidiyorsa → %10 artır (tavan: RISK_MAX_PCT)
    Kötü gidiyorsa → %20 azalt (taban: RISK_MIN_PCT)
    """
    if not DYNAMIC_RISK_ENABLED or len(recent_pnls) < 5:
        return RISK_PER_TRADE_PCT

    n    = min(DYNAMIC_RISK_LOOKBACK, len(recent_pnls))
    last = recent_pnls[-n:]
    wins = [p for p in last if p > 0]
    loss = [p for p in last if p < 0]

    gross_win  = sum(wins) if wins else 0.0
    gross_loss = abs(sum(loss)) if loss else 0.001
    pf         = gross_win / gross_loss

    consecutive_losses = 0
    for p in reversed(last):
        if p < 0:
            consecutive_losses += 1
        else:
            break

    base = RISK_PER_TRADE_PCT
    if pf > 1.5 and consecutive_losses == 0:
        base = min(base * 1.10, RISK_MAX_PCT)
    elif pf < 0.8 or consecutive_losses >= 2:
        base = max(base * 0.80, RISK_MIN_PCT)

    return round(base, 4)


def calculate_leverage(confidence: float) -> int:
    """Sinyal gücüne göre kaldıraç — max 5x."""
    if confidence >= 0.80:
        return LEV_STRONG    # 5x
    if confidence >= 0.70:
        return LEV_MEDIUM    # 3x
    return LEV_WEAK          # 2x


def calculate_position_size_by_risk(
    balance:  float,
    leverage: int,
    entry:    float,
    sl:       float,
) -> float:
    """
    Risk-based position sizing.

    Her işlemde kaybedilecek maksimum USDT = balance × RISK_PER_TRADE_PCT.
    Pozisyon büyüklüğü SL mesafesine göre otomatik hesaplanır.

    Örnek: 1000 USDT bakiye, %2 risk, SL %2 uzakta, 3x kaldıraç
      risk_amount  = 20 USDT
      sl_pct       = 0.02
      notional     = 20 / 0.02 = 1000 USDT
      margin       = 1000 / 3  = 333 USDT  (bakiyenin %33)
    """
    if balance <= 0 or entry <= 0 or sl <= 0:
        return 0.0
    sl_pct = abs(entry - sl) / entry
    if sl_pct < 0.001:   # SL çok yakın — en az %0.1 mesafe zorunlu
        return 0.0
    risk_amount = balance * RISK_PER_TRADE_PCT
    notional    = risk_amount / sl_pct
    margin      = notional / leverage
    # Tavan: bakiyenin %30'undan fazla olmasın
    max_margin  = balance * MAX_POSITION_PCT
    return round(max(min(margin, max_margin), 0), 2)


def calculate_position_size(
    balance:    float,
    leverage:   int,
    open_count: int,
    confidence: float,
) -> float:
    """Eski fallback sizing — risk_by_risk mümkün değilse kullanılır."""
    if open_count >= MAX_OPEN_POSITIONS or balance <= 0:
        return 0.0
    max_per_position = balance * MAX_POSITION_PCT
    max_total = balance * MAX_CAPITAL_RISK_PCT
    per_slot  = max_total / MAX_OPEN_POSITIONS
    slot_size = min(max_per_position, per_slot)
    scale     = 0.80 + confidence * 0.20
    return round(max(slot_size * scale, 0), 2)


def liquidation_price(entry: float, side: str, leverage: int) -> float:
    """
    Teorik liquidasyon fiyatı (isolated margin, sıfır bakiye varsayımı).
    Gerçek liquidasyon biraz farklı olabilir ama bu her zaman daha kötü taraftadır.
    """
    liq_pct = 1.0 / leverage   # 5x → %20, 3x → %33, 2x → %50
    if side == "BUY":
        return round(entry * (1 - liq_pct), 8)
    else:
        return round(entry * (1 + liq_pct), 8)


def safe_stop_loss(entry: float, side: str, leverage: int, atr_sl: float) -> float:
    """
    Liquidasyon güvenlik tamponu uygulayan SL hesabı.

    SL iki koşulu da sağlamalı:
    1. ATR tabanlı SL (teknik)
    2. Liquidasyon fiyatı ile giriş arasındaki mesafenin %50 öncesinde

    İkisinden daha güvenli (girişe yakın) olanı seçilir.
    """
    liq   = liquidation_price(entry, side, leverage)
    liq_dist = abs(entry - liq)

    # Liquidasyon mesafesinin %50'sinde dur (tampon)
    safe_dist = liq_dist * (1 - LIQ_SAFETY_BUFFER)   # max bu kadar gidebilir SL
    safe_dist = min(safe_dist, entry * MAX_SL_PCT)    # ayrıca max %3 ile sınırla

    if side == "BUY":
        # ATR SL ve güvenli SL'den girişe yakın olanı seç
        atr_sl_price  = entry - (entry - atr_sl)      # atr_sl zaten fiyat
        safe_sl_price = entry - safe_dist
        # İkisinden YÜKSEK olanı (girişe daha yakın = daha güvenli)
        return round(max(atr_sl_price, safe_sl_price), 8)
    else:
        atr_sl_price  = atr_sl
        safe_sl_price = entry + safe_dist
        # İkisinden DÜŞÜK olanı (girişe daha yakın)
        return round(min(atr_sl_price, safe_sl_price), 8)


def should_close_position(position: dict, current_price: float) -> tuple[bool, str]:
    """Pozisyon kapanmalı mı?

    TRAIL_AFTER_TP açıkken TP artık bir çıkış değil, bir *mod değişimi*: fiyat
    TP'yi geçtiğinde pozisyon kapatılmaz, `trailing` bayrağı açılır ve bundan
    sonra yalnızca stop'a dokunulursa çıkılır. Amaç, büyük hareketleri sonuna
    kadar taşımak — eski davranışta %66'lık bir yükseliş +%1.2'de kesiliyordu.

    Bunun bedeli bilinçli: TP'de garanti kâr almak yerine, dönüşte kârın bir
    kısmı geri verilir. Küçük dalgalanmalarda eski yöntem daha kârlıydı; büyük
    hareketi yakalamanın karşılığı budur.
    """
    sl = position["stop_loss"]
    tp = position["take_profit"]

    if position["side"] == "BUY":
        if current_price <= sl: return True, "STOP_LOSS"
        if current_price >= tp:
            if not TRAIL_AFTER_TP:
                return True, "TAKE_PROFIT"
            position["trailing"] = True   # artık sadece stop ile çıkılır
    else:
        if current_price >= sl: return True, "STOP_LOSS"
        if current_price <= tp:
            if not TRAIL_AFTER_TP:
                return True, "TAKE_PROFIT"
            position["trailing"] = True

    return False, ""
