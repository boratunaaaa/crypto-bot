"""
Risk yönetimi — sabit dolar riski + risk bütçesi tabanlı kill switch.

2026-08-12 refactor'ının iki temel düzeltmesi:

1. KALDIRAÇ ARTIK RİSKİ DEĞİŞTİRMİYOR.
   Eski kodda pozisyon boyutu risk formülüyle başlıyor ama `MAX_POSITION_PCT`
   teminat tavanına çarpınca risk hedefi eziliyordu. Tavan bağladığında gerçek
   risk `0.15 × bakiye × kaldıraç × sl_pct` oluyordu — yani 5x işlemler 2x'in
   1.7-5 katı dolar riski taşıyordu. Ölçülen sonuç: 5x dilimi 8 işlemde
   -$5.77. Artık teminat tavanına takılan pozisyon KÜÇÜLTÜLMEZ, REDDEDİLİR;
   her açılan işlem tam olarak hedeflenen riski taşır.

   Bunun doğal sonucu: risk-bazlı boyutlandırmada notional = risk / sl_pct
   olduğu için notional kaldıraçtan BAĞIMSIZDIR. Kaldıraç yalnızca ne kadar
   teminat bloke edileceğini, dolayısıyla aynı anda kaç pozisyon taşınabildiğini
   belirler — kârı büyütmez.

2. GÜNLÜK DURDURMA İŞLEM SAYISINA DEĞİL, HARCANAN RİSKE BAKIYOR.
   `missed_signals` analizi (21.731 kayıt) kaçan sinyallerin %50'sinin ve
   skoru 75+ olan kaçanların %61'inin sabit 10 işlem limitinden kaynaklandığını
   gösterdi. Sabit sayı iyi günle kötü günü ayırt edemiyor. Risk bütçesi ayırt
   eder: kazanan işlem bütçeyi geri besler (bot daha çok işlem yapar), kaybeden
   işlem bütçeyi tüketir (bot kendini durdurur).
"""

from datetime import datetime, timedelta

from config import (
    KILL_SWITCH_COOLDOWN_HOURS,
    MAX_MARGIN_PCT, MAX_TOTAL_MARGIN_PCT,
    MAX_OPEN_POSITIONS,
    LEVERAGE_DEFAULT, LEVERAGE_MAX,
    LEV_WEAK, LEV_MEDIUM, LEV_STRONG,
    LIQ_SAFETY_BUFFER,
    MAX_SL_PCT, MIN_MARGIN_USDT,
    RISK_PER_TRADE_PCT, RISK_PER_TRADE_STRONG_PCT, STRONG_SIGNAL_SCORE,
    DAILY_LOSS_LIMIT_PCT, WEEKLY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT,
    MAX_CONSECUTIVE_LOSSES,
    DAILY_RISK_BUDGET_R, RISK_BUDGET_REFUND, MAX_DAILY_TRADES_HARD,
    MAX_PORTFOLIO_RISK_R, SYMBOL_COOLDOWN_MIN,
    DYNAMIC_RISK_ENABLED, RISK_MIN_PCT, RISK_MAX_PCT, DYNAMIC_RISK_LOOKBACK,
    MOMENTUM_RISK_SHARE,
)

# Geriye dönük uyumluluk: eski kod yolları hâlâ bu isimleri import ediyor olabilir.
MAX_POSITION_PCT     = MAX_MARGIN_PCT
MAX_CAPITAL_RISK_PCT = MAX_TOTAL_MARGIN_PCT


class KillSwitchTracker:
    """
    Günlük/haftalık zarar, drawdown, üst üste zarar ve RİSK BÜTÇESİ takibi.

    Tüm sayaçlar `to_dict()`/`from_dict()` ile diske yazılabilir. Eski kodda
    bu sınıf her başlatmada sıfırdan kuruluyordu (`KillSwitchTracker()`), yani
    her restart günlük zarar limitini sıfırlıyordu. systemd'nin `Restart=always`
    ayarı bunu tehlikeli hâle getirmişti: çökme döngüsündeki bir bot günlük
    zarar limitini sınırsız kez sıfırlayabiliyordu.
    """

    def __init__(self, initial_balance: float = 0.0):
        self.daily_pnl          = 0.0
        self.weekly_pnl         = 0.0
        self.daily_trades       = 0
        self.consecutive_losses = 0
        self.daily_start_balance = initial_balance
        self.high_water_mark    = initial_balance
        self.daily_risk_used_r  = 0.0
        self.momentum_risk_used_r = 0.0
        self._last_day  = datetime.now().date()
        self._last_week = datetime.now().isocalendar()[1]
        self._halted_at: datetime | None = None
        self._symbol_cooldown: dict[str, datetime] = {}

    # ── Gün/hafta devri ───────────────────────────────────────────────────────

    def _reset_if_needed(self, balance: float):
        today = datetime.now().date()
        if today != self._last_day:
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.daily_risk_used_r = 0.0
            self.momentum_risk_used_r = 0.0
            self.daily_start_balance = balance
            self._last_day = today
        week = datetime.now().isocalendar()[1]
        if week != self._last_week:
            self.weekly_pnl  = 0.0
            self._last_week  = week

    def initialize(self, balance: float):
        if self.daily_start_balance <= 0:
            self.daily_start_balance = balance
        if self.high_water_mark <= 0:
            self.high_water_mark = balance

    # ── Kayıt ─────────────────────────────────────────────────────────────────

    def record_trade_open(self, balance: float, risk_r: float = 1.0, strategy: str = "TREND"):
        self._reset_if_needed(balance)
        self.daily_trades += 1
        self.daily_risk_used_r += risk_r
        if strategy == "MOMENTUM_FAST":
            self.momentum_risk_used_r += risk_r

    def record_trade_close(self, pnl: float, balance: float, realized_r: float = 0.0,
                           symbol: str | None = None, allocated: float = 1.0):
        self._reset_if_needed(balance)
        self.daily_pnl  += pnl
        self.weekly_pnl += pnl
        self.high_water_mark = max(self.high_water_mark, balance)

        if symbol:
            self._symbol_cooldown[symbol] = datetime.now()

        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES and self._halted_at is None:
                self._halted_at = datetime.now()
        else:
            self.consecutive_losses = 0
            self._halted_at = None
            # Kârlı işlem risk bütçesini geri besler — ama kendi riskinden
            # fazlasını değil. Böylece iyi giden günde bot daha çok işlem
            # yapabilir, ama tek bir büyük kazanç bütçeyi sınırsız açamaz.
            # İade, o işleme AYRILAN R ile sınırlı — sabit 1.0 değil. Yarım
            # riskle açılmış bir işlem bütçeye tam 1R geri veremez, aksi hâlde
            # bütçe küçük risklerle sonsuza kadar şişer.
            if RISK_BUDGET_REFUND and realized_r > 0:
                cap = allocated if allocated and allocated > 0 else 1.0
                refund = min(realized_r, cap)
                self.daily_risk_used_r = max(0.0, self.daily_risk_used_r - refund)

    # ── Sorgular ──────────────────────────────────────────────────────────────

    def current_drawdown(self, balance: float) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, (self.high_water_mark - balance) / self.high_water_mark)

    def symbol_on_cooldown(self, symbol: str) -> bool:
        last = self._symbol_cooldown.get(symbol)
        if last is None:
            return False
        return (datetime.now() - last) < timedelta(minutes=SYMBOL_COOLDOWN_MIN)

    def risk_budget_remaining(self) -> float:
        return max(0.0, DAILY_RISK_BUDGET_R - self.daily_risk_used_r)

    def momentum_budget_remaining(self) -> float:
        """MOMENTUM_FAST'in izole bütçesi — kötüyse ana stratejiyi tüketemez."""
        cap = DAILY_RISK_BUDGET_R * MOMENTUM_RISK_SHARE
        return max(0.0, cap - self.momentum_risk_used_r)

    def can_trade(self, balance: float, strategy: str = "TREND") -> tuple[bool, str]:
        self._reset_if_needed(balance)
        ref = self.daily_start_balance if self.daily_start_balance > 0 else balance

        # Drawdown — en sert koruma, her şeyin önünde
        dd = self.current_drawdown(balance)
        if dd >= MAX_DRAWDOWN_PCT:
            return False, f"Maksimum drawdown aşıldı (%{dd*100:.1f})"

        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
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

        # Risk bütçesi — sabit işlem sayısı limitinin yerini alan mekanizma
        if self.daily_risk_used_r >= DAILY_RISK_BUDGET_R:
            return False, (
                f"Günlük risk bütçesi doldu "
                f"({self.daily_risk_used_r:.1f}R/{DAILY_RISK_BUDGET_R:.1f}R)"
            )

        if strategy == "MOMENTUM_FAST" and self.momentum_budget_remaining() <= 0:
            return False, "Momentum risk bütçesi doldu"

        # Runaway/bug koruması — normal işleyişte asla bağlamaz
        if self.daily_trades >= MAX_DAILY_TRADES_HARD:
            return False, f"Güvenlik tavanı ({self.daily_trades}/{MAX_DAILY_TRADES_HARD})"

        if self.daily_pnl <= -(ref * DAILY_LOSS_LIMIT_PCT):
            return False, f"Günlük zarar limiti ({self.daily_pnl:.2f}$)"
        if self.weekly_pnl <= -(ref * WEEKLY_LOSS_LIMIT_PCT):
            return False, f"Haftalık zarar limiti ({self.weekly_pnl:.2f}$)"
        return True, "OK"

    # ── Kalıcılık ─────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "daily_trades": self.daily_trades,
            "consecutive_losses": self.consecutive_losses,
            "daily_start_balance": self.daily_start_balance,
            "high_water_mark": self.high_water_mark,
            "daily_risk_used_r": self.daily_risk_used_r,
            "momentum_risk_used_r": self.momentum_risk_used_r,
            "last_day": self._last_day.isoformat(),
            "last_week": self._last_week,
            "halted_at": self._halted_at.isoformat() if self._halted_at else None,
            "symbol_cooldown": {
                k: v.isoformat() for k, v in self._symbol_cooldown.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KillSwitchTracker":
        ks = cls(d.get("daily_start_balance", 0.0))
        ks.daily_pnl           = d.get("daily_pnl", 0.0)
        ks.weekly_pnl          = d.get("weekly_pnl", 0.0)
        ks.daily_trades        = d.get("daily_trades", 0)
        ks.consecutive_losses  = d.get("consecutive_losses", 0)
        ks.daily_start_balance = d.get("daily_start_balance", 0.0)
        ks.high_water_mark     = d.get("high_water_mark", 0.0)
        ks.daily_risk_used_r   = d.get("daily_risk_used_r", 0.0)
        ks.momentum_risk_used_r= d.get("momentum_risk_used_r", 0.0)
        try:
            ks._last_day = datetime.fromisoformat(d["last_day"]).date()
        except Exception:
            ks._last_day = datetime.now().date()
        ks._last_week = d.get("last_week", datetime.now().isocalendar()[1])
        ha = d.get("halted_at")
        ks._halted_at = datetime.fromisoformat(ha) if ha else None
        ks._symbol_cooldown = {}
        for k, v in (d.get("symbol_cooldown") or {}).items():
            try:
                ks._symbol_cooldown[k] = datetime.fromisoformat(v)
            except Exception:
                pass
        return ks


# ── Risk yüzdesi ──────────────────────────────────────────────────────────────

def base_risk_pct(score: int = 0) -> float:
    """Sinyal kalitesine göre temel risk. Çok güçlü sinyalde tavan %1."""
    if score >= STRONG_SIGNAL_SCORE:
        return RISK_PER_TRADE_STRONG_PCT
    return RISK_PER_TRADE_PCT


def dynamic_risk_pct(recent_pnls: list[float], score: int = 0) -> float:
    """
    Son işlemlere göre risk yüzdesini ölçekle.

    İyi gidiyorsa %10 artır (tavan RISK_MAX_PCT), kötü gidiyorsa %20 azalt
    (taban RISK_MIN_PCT).
    """
    base = base_risk_pct(score)
    if not DYNAMIC_RISK_ENABLED or len(recent_pnls) < 5:
        return base

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

    if pf > 1.5 and consecutive_losses == 0:
        base = min(base * 1.10, RISK_MAX_PCT)
    elif pf < 0.8 or consecutive_losses >= 2:
        base = max(base * 0.80, RISK_MIN_PCT)

    return round(base, 5)


# ── Kaldıraç ──────────────────────────────────────────────────────────────────

def calculate_leverage(confidence: float = 0.0, score: int = 0) -> int:
    """
    Sabit kaldıraç.

    Eski davranış confidence >= 0.80'de 5x veriyordu — yani en yüksek kaldıracı
    botun en çok güvendiği sinyale. Ölçüm bunun ters teptiğini gösterdi
    (5x: 8 işlem, -$5.77 net). Risk-bazlı boyutlandırmada kaldıraç zaten kârı
    değiştirmediği için, güven skoruyla ilişkilendirmenin bir faydası yok;
    yalnızca likidasyon mesafesini kısaltıyor.
    """
    return min(LEVERAGE_DEFAULT, LEVERAGE_MAX)


# ── Pozisyon boyutu ───────────────────────────────────────────────────────────

def calculate_position(
    equity:   float,
    leverage: int,
    entry:    float,
    sl:       float,
    risk_pct: float,
    open_margin_total: float = 0.0,
) -> tuple[float, float, float, str]:
    """
    Risk-bazlı pozisyon boyutu.

    Dönüş: (margin, qty, risk_amount, red_nedeni)
    red_nedeni boş string ise pozisyon açılabilir.

        risk_amount = equity × risk_pct          ← kaldıraçtan bağımsız
        notional    = risk_amount / sl_pct       ← kaldıraçtan bağımsız
        margin      = notional / leverage        ← kaldıraç SADECE burada

    Teminat tavanı aşılırsa pozisyon küçültülmez, reddedilir: küçültmek riski
    hedeften saptırır ve ölçümü bozar (eski kodun temel hatası buydu).
    """
    if equity <= 0 or entry <= 0 or sl <= 0:
        return 0.0, 0.0, 0.0, "Geçersiz girdi"

    sl_pct = abs(entry - sl) / entry
    if sl_pct < 0.001:
        return 0.0, 0.0, 0.0, "SL çok yakın (<%0.1)"
    if sl_pct > MAX_SL_PCT:
        return 0.0, 0.0, 0.0, f"SL çok uzak (%{sl_pct*100:.1f} > %{MAX_SL_PCT*100:.0f})"

    risk_amount = equity * risk_pct
    notional    = risk_amount / sl_pct
    margin      = notional / max(leverage, 1)
    qty         = notional / entry

    if margin < MIN_MARGIN_USDT:
        return 0.0, 0.0, 0.0, f"Teminat çok küçük ({margin:.2f}$ < {MIN_MARGIN_USDT}$)"

    max_margin = equity * MAX_MARGIN_PCT
    if margin > max_margin:
        return 0.0, 0.0, 0.0, (
            f"Teminat tavanı aşıldı ({margin:.1f}$ > {max_margin:.1f}$) — "
            f"stop çok yakın, risk hedefi korunamaz"
        )

    if open_margin_total + margin > equity * MAX_TOTAL_MARGIN_PCT:
        return 0.0, 0.0, 0.0, "Toplam teminat tavanı aşıldı"

    return round(margin, 2), qty, round(risk_amount, 4), ""


def portfolio_open_risk_r(positions: dict) -> float:
    """
    Açık pozisyonların toplam riski (R cinsinden).

    Stop breakeven'e taşınmış bir pozisyon artık risk taşımaz — bu yüzden
    kalan risk stop'un GÜNCEL yerine göre hesaplanır, girişe göre değil.
    """
    total = 0.0
    for pos in positions.values():
        risk_amount = pos.get("risk_amount", 0.0)   # 1R'nin dolar karşılığı
        if risk_amount <= 0:
            continue
        entry = pos["entry_price"]
        stop  = pos.get("stop_loss", entry)
        qty   = pos.get("qty", 0.0)
        # Stop lehte tarafa geçtiyse (breakeven/trail) kalan risk sıfırdır.
        if pos["side"] == "BUY":
            risk_per_unit_now = max(0.0, entry - stop)
        else:
            risk_per_unit_now = max(0.0, stop - entry)
        total += (risk_per_unit_now * qty) / risk_amount
    return round(total, 3)


def can_add_position(positions: dict, equity: float,
                     new_risk_r: float = 0.0) -> tuple[bool, str]:
    """
    Portföy seviyesi kontroller — PROSPEKTİF.

    DENETİM BULGUSU: yalnız MEVCUT risk kontrol ediliyordu. 3.5R açıkken 1R'lik
    yeni bir pozisyon kabul ediliyor ve toplam 4.5R'ye çıkıyordu — yani "max 4R"
    tavanı aşılabiliyordu. Artık açılacak pozisyonun riski de hesaba katılıyor.
    """
    if len(positions) >= MAX_OPEN_POSITIONS:
        return False, f"Max pozisyon ({MAX_OPEN_POSITIONS})"
    open_risk = portfolio_open_risk_r(positions)
    if open_risk >= MAX_PORTFOLIO_RISK_R:
        return False, f"Portföy risk tavanı ({open_risk:.1f}R/{MAX_PORTFOLIO_RISK_R:.1f}R)"
    projected = open_risk + max(0.0, new_risk_r)
    if projected > MAX_PORTFOLIO_RISK_R:
        return False, (
            f"Portföy risk tavanı ({open_risk:.1f}R + {new_risk_r:.1f}R "
            f"= {projected:.1f}R > {MAX_PORTFOLIO_RISK_R:.1f}R)"
        )
    return True, ""


def allocated_r(risk_pct: float) -> float:
    """
    Bir işlemin günlük bütçeden tüketeceği R miktarı.

    DENETİM BULGUSU: her işlem sabit 1.0R sayılıyordu. Ama gerçek risk
    `dynamic_risk_pct()` ile %0.4–%1.2 arasında değişiyor — 3 kat fark. Sonuç:
    "risk bütçesi" fiilen bir İŞLEM SAYACI gibi davranıyordu, yani refactor'ın
    kaldırdığı sabit-sayı limitinin ta kendisi. Artık bütçe gerçek riski ölçüyor.
    """
    if RISK_PER_TRADE_PCT <= 0:
        return 1.0
    return round(risk_pct / RISK_PER_TRADE_PCT, 4)


# ── Likidasyon güvenliği ──────────────────────────────────────────────────────

def liquidation_price(entry: float, side: str, leverage: int) -> float:
    liq_pct = 1.0 / leverage
    if side == "BUY":
        return round(entry * (1 - liq_pct), 8)
    return round(entry * (1 + liq_pct), 8)


def safe_stop_loss(entry: float, side: str, leverage: int, atr_sl: float) -> float:
    """SL'yi hem ATR'ye hem de likidasyon tamponuna göre güvenli tarafa çek."""
    liq   = liquidation_price(entry, side, leverage)
    liq_dist = abs(entry - liq)

    safe_dist = liq_dist * (1 - LIQ_SAFETY_BUFFER)
    safe_dist = min(safe_dist, entry * MAX_SL_PCT)

    if side == "BUY":
        return round(max(atr_sl, entry - safe_dist), 8)
    return round(min(atr_sl, entry + safe_dist), 8)


# ── Geriye dönük uyumluluk ────────────────────────────────────────────────────

def calculate_position_size_by_risk(balance, leverage, entry, sl):
    """Eski imza (backtest.py kullanıyor). Yeni motor `calculate_position`."""
    margin, _qty, _risk, err = calculate_position(
        balance, leverage, entry, sl, RISK_PER_TRADE_PCT
    )
    return 0.0 if err else margin


def should_close_position(position: dict, current_price: float) -> tuple[bool, str]:
    """
    Eski basit stop/TP kontrolü.

    Yeni çıkış motoru `position.evaluate()`. Bu fonksiyon yalnızca geriye dönük
    uyumluluk için duruyor ve TRAIL/breakeven mantığı İÇERMEZ — canlı kod
    yollarında kullanılmamalıdır.
    """
    sl = position["stop_loss"]
    if position["side"] == "BUY":
        if current_price <= sl:
            return True, "STOP_LOSS"
    else:
        if current_price >= sl:
            return True, "STOP_LOSS"
    return False, ""
