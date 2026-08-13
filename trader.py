"""
Trader modülleri:
- PaperPortfolio : simülasyon
- SpotTrader     : kaldıraçsız spot
- FuturesTrader  : kaldıraçlı long/short

2026-08-12 refactor — PARİTE:
Çıkış kararı artık tek bir yerde üretilir (`position.evaluate`). PaperPortfolio
ve FuturesTrader aynı kararı alır; yalnızca o kararın NASIL uygulandığı farklıdır
(biri simüle eder, diğeri Binance'e emir gönderir). Eski kodda trailing/breakeven
yalnızca paper tarafında vardı ve gerçek modda pozisyonlar sabit TP emriyle
kapanıyordu — yani gerçek paraya geçmek, kârı üreten mekanizmayı kapatmak
anlamına geliyordu.
"""

import logging
import math
from datetime import datetime

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import (
    API_KEY, API_SECRET, TESTNET, PAPER_TRADING, PAPER_BALANCE, SPOT_MODE,
    COMMISSION_RATE, SLIPPAGE_PCT, SLIPPAGE_STOP_MULT, FUNDING_RATE_8H,
    MARGIN_MODE,
)
from position import (
    new_position, evaluate, position_summary, realized_r, confirm_partial,
    EXIT_MANUAL, EXIT_TP1, EXIT_TP2,
)
from database import save_trade, save_open_positions

logger = logging.getLogger("trader")


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _round_step(qty: float, step: float) -> float:
    if step == 0:
        return qty
    precision = int(round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)


def _round_tick(price: float, tick: float, side_up: bool) -> float:
    """
    Fiyatı sembolün tickSize'ına oturt.

    DENETİM BULGUSU: stop fiyatı `round(price, 6)` ile gönderiliyordu. Binance
    PRICE_FILTER'a uymayan fiyatı -1111 ile REDDEDER; bu da aşağıdaki
    `_sync_protective_stop` hatasını tetikleyip pozisyonu korumasız bırakıyordu.

    Yuvarlama yönü önemli: SHORT'un stop'u yukarıda, LONG'unki aşağıda. Her
    zaman fiyattan UZAĞA yuvarlanır. Fiyata doğru yuvarlamak stop'u bir tick
    daraltır (kazancı ihmal edilebilir) ama emrin "hemen tetiklenir" (-2021)
    hatasıyla reddedilme ihtimalini artırır — o hata pozisyonu korumasız
    bırakıyor, yani ihmal edilebilir bir kazanç için ciddi bir risk.
    """
    if tick <= 0:
        return round(price, 6)
    precision = max(0, int(round(-math.log10(tick))))
    n = price / tick
    n = math.ceil(n) if side_up else math.floor(n)
    return round(n * tick, precision)


def fill_price(price: float, side: str, is_entry: bool, reason: str = "") -> float:
    """
    Gerçekleşme fiyatı — her zaman ALEYHTE kayar.

    DENETİM BULGUSU: `PaperPortfolio` emirleri tam görülen fiyattan dolduruyordu;
    `backtest.py` ise her bacakta kayma uyguluyordu. İkisi aynı stratejiyi farklı
    maliyetle ölçüyordu ve `go_live_check` kapıları kâğıt tarafından besleniyordu
    — yani gerçek paraya geçiş eşiği olduğundan kolaydı.

    Stop çıkışları ayrıca cezalandırılır: stop emri piyasa emridir ve tam da
    hareketin sert olduğu anda tetiklenir, normal emirden kötü dolar.
    """
    slip = SLIPPAGE_PCT
    if not is_entry and "STOP" in (reason or ""):
        slip *= SLIPPAGE_STOP_MULT
    # Giriş: BUY yukarı, SELL aşağı kayar. Çıkış: tersi.
    if is_entry:
        adverse_up = side == "BUY"
    else:
        adverse_up = side != "BUY"
    return price * (1 + slip) if adverse_up else price * (1 - slip)


def funding_cost(pos: dict, price: float, now: datetime | None = None) -> float:
    """
    Perpetual funding — 8 saatte bir notional üzerinden.

    Kısa pozisyonlarda küçük, ama time-stop 480 dakikaya kadar izin verdiği için
    sıfır değil. Modellenmezse kâğıt sonucu yine iyimser çıkar.
    """
    now = now or datetime.now()
    hours = (now - pos["open_time"]).total_seconds() / 3600
    periods = int(hours // 8)
    charged = pos.get("_funding_periods", 0)
    if periods <= charged:
        return 0.0
    pos["_funding_periods"] = periods
    return pos["qty"] * price * FUNDING_RATE_8H * (periods - charged)


# ── Ortak çıkış yürütücüsü ────────────────────────────────────────────────────

class ExitEngineMixin:
    """
    `position.evaluate()` kararlarını uygulayan ortak katman.

    Alt sınıflar şunları sağlar:
        _do_partial(pos, qty_close, price) -> bool
        _do_close(pos, qty_close, price)   -> bool
        _sync_protective_stop(pos)         -> None   (opsiyonel)
    """

    def check_positions(self, prices):
        closed = []
        for sym, pos in list(self.positions.items()):
            cur = prices.get(sym)
            if cur is None:
                continue

            prev_stop = pos.get("stop_loss")
            risk_amount = pos.get("risk_amount", 0.0)
            actions = evaluate(pos, cur)

            for act in actions:
                if act["type"] == "PARTIAL":
                    self._apply_partial(pos, act["fraction"], cur, act["reason"])
                elif act["type"] == "CLOSE":
                    ok, pnl = self.close_position(sym, cur, act["reason"])
                    if ok:
                        # R-katsayısı risk bütçesinin geri beslemesinde kullanılır
                        r_mult = (pnl / risk_amount) if risk_amount > 0 else 0.0
                        closed.append((sym, act["reason"], pnl, round(r_mult, 3)))
                    break

            # Koruyucu stop emri borsada tutuluyorsa yerini güncelle
            if sym in self.positions and pos.get("stop_loss") != prev_stop:
                try:
                    self._sync_protective_stop(pos)
                except Exception as e:
                    logger.debug(f"Koruyucu stop güncellenemedi {sym}: {e}")

        self._persist_positions()
        return closed

    # Kâğıt tarafı gerçekleşmeyi simüle eder (kayma uygular); gerçek trader'da
    # fiyat borsadan gelir, sentetik kayma eklemek çift sayım olur.
    SIMULATE_FILLS = False

    def _apply_partial(self, pos, fraction, price, reason):
        """Pozisyonun bir kısmını realize et; kalan koşmaya devam eder."""
        qty_close = pos["initial_qty"] * fraction
        qty_close = min(qty_close, pos["qty"])
        if qty_close <= 0:
            return
        ok = self._do_partial(pos, qty_close, price)
        if not ok:
            return
        # Emir gerçekleşti — durum ancak ŞİMDİ güncellenir.
        confirm_partial(pos, reason)
        if self.SIMULATE_FILLS:
            price = fill_price(price, pos["side"], is_entry=False, reason=reason)

        gross = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                 else (pos["entry_price"] - price)) * qty_close
        comm  = qty_close * price * COMMISSION_RATE
        net   = gross - comm

        margin_released = pos["initial_margin"] * (qty_close / pos["initial_qty"])
        pos["qty"]    -= qty_close
        pos["margin"] -= margin_released
        pos["realized_pnl"]        += net
        pos["realized_commission"] += comm

        self.balance   += margin_released + net
        self.total_pnl += net
        self.total_commission = getattr(self, "total_commission", 0.0) + comm

        self.trade_log.append(dict(
            time=datetime.now(), symbol=pos["symbol"],
            action=f"PARTIAL ({reason})", price=price, pnl=net,
        ))
        logger.info(
            f"KISMİ ÇIKIŞ: {pos['symbol']} | {reason} | "
            f"%{fraction*100:.0f} @ {price:.6g} | +{net:.2f}$ | "
            f"kalan {pos['qty']/pos['initial_qty']*100:.0f}%"
        )

    def _do_partial(self, pos, qty_close, price) -> bool:
        return True

    def _sync_protective_stop(self, pos) -> None:
        return None

    def _persist_positions(self):
        try:
            save_open_positions(self.positions)
        except Exception as e:
            logger.debug(f"Pozisyon kalıcılığı hatası: {e}")

    def restore_positions(self, saved: dict):
        """Restart sonrası açık pozisyonları geri yükle."""
        restored = 0
        for sym, pos in (saved or {}).items():
            if not isinstance(pos, dict) or "entry_price" not in pos:
                continue
            pos.setdefault("initial_qty", pos.get("qty", 0.0))
            pos.setdefault("initial_margin", pos.get("margin", 0.0))
            pos.setdefault("risk_per_unit", abs(pos["entry_price"] - pos.get("initial_stop", pos.get("stop_loss", 0))))
            pos.setdefault("risk_amount", pos["risk_per_unit"] * pos["initial_qty"])
            self.positions[sym] = pos
            restored += 1
        if restored:
            logger.info(f"{restored} açık pozisyon geri yüklendi (restart sonrası)")
        return restored


# ── Paper Portfolio ───────────────────────────────────────────────────────────

class PaperPortfolio(ExitEngineMixin):

    SIMULATE_FILLS = True

    def __init__(self, initial_balance: float):
        self.balance   = initial_balance
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.total_commission = 0.0
        self.wins = self.losses = 0

    def open_position(self, symbol, side, price, margin, leverage, sl, tp,
                      reason="", score=0, btc_trend="SIDEWAYS",
                      market_regime="UNKNOWN", atr_pct=0.0, fear_greed=50,
                      strategy="TREND", qty=None, risk_amount=0.0,
                      entry_snapshot=None):
        if symbol in self.positions:
            return False, "Zaten açık"
        # Kâğıt da olsa emir aleyhte dolar — canlıyla aynı maliyet modeli.
        price = fill_price(price, side, is_entry=True)
        if qty is None:
            qty = (margin * leverage) / price
        notional = qty * price
        commission_open = notional * COMMISSION_RATE
        if self.balance < margin + commission_open:
            return False, "Yetersiz bakiye"

        self.balance -= margin + commission_open
        self.total_commission += commission_open

        pos = new_position(
            symbol=symbol, side=side, entry_price=price, qty=qty,
            margin=margin, leverage=leverage, stop_loss=sl, take_profit=tp,
            atr_pct=atr_pct or 0.01, strategy=strategy,
            reason_open=reason, score=score, btc_trend=btc_trend,
            market_regime=market_regime, fear_greed=fear_greed,
            entry_snapshot=entry_snapshot or {},
            open_commission=commission_open,
        )
        if risk_amount:
            pos["risk_amount"] = risk_amount
        self.positions[symbol] = pos

        self.trade_log.append(dict(
            time=datetime.now(), symbol=symbol,
            action=f"OPEN {side}", price=price, margin=margin, leverage=leverage,
        ))
        self._persist_positions()
        return True, "OK"

    def close_position(self, symbol, price, reason=EXIT_MANUAL):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        qty = pos["qty"]
        price = fill_price(price, pos["side"], is_entry=False, reason=reason)

        gross = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                 else (pos["entry_price"] - price)) * qty
        comm  = qty * price * COMMISSION_RATE
        fund  = funding_cost(pos, price) + pos.get("_funding_paid", 0.0)
        net   = gross - comm - fund

        self.balance += pos["margin"] + net
        self.total_commission += comm

        # DENETİM BULGUSU: açılış komisyonu bakiyeden düşülüyordu ama işlemin
        # net_pnl'ine GİRMİYORDU. Yani DB'ye, R-katsayısına, kill switch'e ve
        # go-live kapılarına giden sayı her işlemde bir bacak komisyon kadar
        # iyimserdi. Artık tam maliyet net'e dahil.
        open_comm   = pos.get("open_commission", 0.0)
        total_comm  = comm + pos.get("realized_commission", 0.0) + open_comm
        total_net   = net + pos.get("realized_pnl", 0.0) - open_comm
        total_gross = total_net + total_comm + fund
        # `total_pnl` gerçekleşen TAM kâr olmalı: açılış komisyonu bakiyeden
        # düşülüyordu ama buraya girmiyordu, yani bakiye ile toplam PnL
        # her işlemde bir bacak komisyon kadar ayrışıyordu.
        self.total_pnl += net - open_comm

        if total_net >= 0: self.wins += 1
        else:              self.losses += 1

        self.trade_log.append(dict(
            time=datetime.now(), symbol=symbol,
            action=f"CLOSE ({reason})", price=price, pnl=net,
        ))
        self._record_trade(pos, price, reason, total_gross, total_comm, total_net)
        self._persist_positions()
        return True, total_net

    def _record_trade(self, pos, price, reason, gross, comm, net):
        summary = position_summary(pos)
        try:
            save_trade(
                symbol=pos["symbol"], side=pos["side"],
                entry_price=pos["entry_price"], close_price=price,
                margin=pos["initial_margin"], leverage=pos["leverage"],
                gross_pnl=gross, commission=comm, net_pnl=net,
                reason_open=pos.get("reason_open", ""),
                reason_close=reason,
                open_time=pos["open_time"], close_time=datetime.now(),
                score=pos.get("score", 0),
                btc_trend=pos.get("btc_trend", "SIDEWAYS"),
                market_regime=pos.get("market_regime", "UNKNOWN"),
                atr_pct=pos.get("atr_pct", 0.0),
                fear_greed=pos.get("fear_greed", 50),
                strategy=summary["strategy"],
                r_multiple=round(realized_r(pos, net), 3),
                mae_r=summary["mae_r"], mfe_r=summary["mfe_r"],
                risk_amount=summary["risk_amount"],
                initial_stop=summary["initial_stop"],
                partial_pnl=summary["partial_pnl"],
                exit_detail=(
                    f"tp1={summary['tp1_done']} tp2={summary['tp2_done']} "
                    f"stop_kind={pos.get('stop_kind')}"
                ),
                entry_snapshot=pos.get("entry_snapshot", {}),
            )
        except Exception as e:
            logger.error(f"Trade kaydedilemedi: {e}")

    def get_balance(self): return self.balance
    def sync_balance(self): return self.balance

    def mark(self, prices: dict) -> None:
        """Son fiyatları kaydet — equity'nin mark-to-market olabilmesi için."""
        for sym, px in (prices or {}).items():
            if sym in self.positions and px:
                self.positions[sym]["_mark"] = float(px)

    @property
    def equity(self):
        """
        Bakiye + teminat + GERÇEKLEŞMEMİŞ kâr/zarar.

        DENETİM BULGUSU: burada yalnız `balance + margin` vardı. Yani açık
        pozisyonun anlık zararı equity'ye HİÇ yansımıyordu. Bunun iki sonucu:
        (a) drawdown kapısı ciddi zararı görmüyordu, (b) `equity_snapshots`
        tablosu "gerçek drawdown" iddiasıyla kaydediliyor ama içinde
        gerçekleşmemiş zarar olmadığı için o iddia YANLIŞTI.
        """
        total = self.balance
        for p in self.positions.values():
            total += p.get("margin", 0.0)
            mark = p.get("_mark")
            if mark:
                diff = ((mark - p["entry_price"]) if p["side"] == "BUY"
                        else (p["entry_price"] - mark))
                total += diff * p.get("qty", 0.0)
        return total

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Spot Trader ───────────────────────────────────────────────────────────────

class SpotTrader(ExitEngineMixin):

    def __init__(self):
        self.client    = Client(API_KEY, API_SECRET, testnet=TESTNET)
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.total_commission = 0.0
        self.wins = self.losses = 0
        self.balance = 0.0

    def sync_balance(self):
        try:
            acc = self.client.get_account()
            for b in acc["balances"]:
                if b["asset"] == "USDT":
                    self.balance = float(b["free"])
                    return self.balance
        except BinanceAPIException as e:
            logger.error(f"Spot bakiye hatası: {e}")
        return self.balance

    def get_balance(self): return self.balance

    @property
    def equity(self):
        return self.balance + sum(p.get("margin", 0.0) for p in self.positions.values())

    def _symbol_filters(self, symbol):
        info  = self.client.get_symbol_info(symbol)
        step  = next((float(f["stepSize"]) for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.001)
        min_q = next((float(f["minQty"])   for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
        return step, min_q

    def sell_asset_to_usdt(self, asset: str) -> float:
        try:
            acc = self.client.get_account()
            qty = next((float(b["free"]) for b in acc["balances"] if b["asset"] == asset), 0.0)
            if qty <= 0: return 0.0
            symbol = f"{asset}USDT"
            step, min_q = self._symbol_filters(symbol)
            qty    = _round_step(qty, step)
            if qty < min_q:
                logger.warning(f"{asset} miktarı çok küçük: {qty} < {min_q}")
                return 0.0
            order = self.client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
            usdt  = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            self.balance += usdt
            logger.info(f"{asset} satıldı: {qty} → {usdt:.4f} USDT")
            return usdt
        except Exception as e:
            logger.error(f"{asset} satış hatası: {e}")
            return 0.0

    def open_position(self, symbol, side, price, margin, leverage, sl, tp,
                      strategy="TREND", qty=None, risk_amount=0.0,
                      entry_snapshot=None, **kwargs):
        if side != "BUY" or symbol in self.positions:
            return False, "Sadece BUY / zaten açık"
        try:
            step, min_q = self._symbol_filters(symbol)
            spend = min(margin, self.balance * 0.995)
            if spend < 1.0:
                return False, f"Yetersiz ({spend:.2f}$)"
            q = _round_step(spend / price, step)
            if q < min_q:
                return False, "Miktar küçük"
            order = self.client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=q)
            ap    = float(order["fills"][0]["price"]) if order.get("fills") else price
            aq    = float(order["executedQty"])
            asp   = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            self.balance -= asp
            pos = new_position(
                symbol=symbol, side="BUY", entry_price=ap, qty=aq,
                margin=asp, leverage=1, stop_loss=sl, take_profit=tp,
                atr_pct=kwargs.get("atr_pct", 0.01), strategy=strategy,
                entry_snapshot=entry_snapshot or {},
                open_commission=asp * COMMISSION_RATE,
                **{k: v for k, v in kwargs.items() if k != "atr_pct"},
            )
            if risk_amount:
                pos["risk_amount"] = risk_amount
            self.positions[symbol] = pos
            self._persist_positions()
            return True, "OK"
        except BinanceAPIException as e:
            return False, str(e)

    def _do_partial(self, pos, qty_close, price) -> bool:
        try:
            step, min_q = self._symbol_filters(pos["symbol"])
            q = _round_step(qty_close, step)
            if q < min_q:
                return False
            self.client.create_order(symbol=pos["symbol"], side="SELL", type="MARKET", quantity=q)
            return True
        except BinanceAPIException as e:
            logger.error(f"Spot kısmi çıkış hatası {pos['symbol']}: {e}")
            return False

    def close_position(self, symbol, price, reason=EXIT_MANUAL):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        try:
            step, min_q = self._symbol_filters(symbol)
            qty = _round_step(pos["qty"], step)
            if qty < min_q:
                self.positions[symbol] = pos
                return False, "Miktar küçük"
            order = self.client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
            back  = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            comm  = back * COMMISSION_RATE
            net   = back - pos["margin"] - comm
            self.balance += back - comm
            self.total_pnl += net
            self.total_commission += comm
            total_net = net + pos.get("realized_pnl", 0.0)
            if total_net >= 0: self.wins += 1
            else:              self.losses += 1
            self._persist_positions()
            return True, total_net
        except BinanceAPIException as e:
            self.positions[symbol] = pos
            return False, str(e)

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Futures Trader ────────────────────────────────────────────────────────────

class FuturesTrader(ExitEngineMixin):

    # Stop emri yalnızca bu orandan fazla kaydıysa borsada güncellenir
    # (her tick'te cancel/create API kotasını tüketir).
    STOP_UPDATE_THRESHOLD = 0.001

    def __init__(self):
        self.client    = Client(API_KEY, API_SECRET, testnet=TESTNET)
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.total_commission = 0.0
        self.wins = self.losses = 0
        self.balance = 0.0
        self._step_cache: dict[str, tuple[float, float]] = {}
        self._tick_cache: dict[str, float] = {}
        # Borsada koruyucu stop'u OLMADIĞI bilinen pozisyonlar: symbol -> sebep.
        # main.py bunu okuyup alarm üretir ve yeni işlem açmayı durdurur.
        self.unprotected: dict[str, str] = {}
        # Binance'de açık ama yerel kaydı olmayan pozisyonlar.
        self.orphans: dict[str, str] = {}
        # Borsa tarafında kapanan işlemler — main döngüsü bunları kill
        # switch'e işler ve listeyi tüketir.
        self.exchange_closed: list = []
        self._load_step_cache()

    def _load_step_cache(self):
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_cache[s["symbol"]] = (float(f["stepSize"]), float(f["minQty"]))
                    elif f["filterType"] == "PRICE_FILTER":
                        self._tick_cache[s["symbol"]] = float(f["tickSize"])
            logger.info(
                f"Filtre cache yüklendi: {len(self._step_cache)} lot, "
                f"{len(self._tick_cache)} tick"
            )
        except Exception as e:
            logger.error(f"Filtre cache yüklenemedi: {e}")

    def _get_tick(self, symbol: str) -> float:
        if symbol not in self._tick_cache:
            self._load_step_cache()
        return self._tick_cache.get(symbol, 0.0)

    def sync_balance(self):
        try:
            for b in self.client.futures_account_balance():
                if b["asset"] == "USDT":
                    self.balance = float(b["balance"])
                    return self.balance
        except BinanceAPIException as e:
            logger.error(f"Futures bakiye hatası: {e}")
        return self.balance

    def get_balance(self): return self.balance

    @property
    def equity(self):
        return self.balance + sum(p.get("margin", 0.0) for p in self.positions.values())

    def transfer_spot_to_futures(self, amount: float) -> bool:
        try:
            self.client.futures_account_transfer(asset="USDT", amount=round(amount, 2), type=1)
            logger.info(f"Transfer: {amount:.4f} USDT Spot → Futures")
            return True
        except BinanceAPIException as e:
            logger.error(f"Transfer hatası: {e}")
            return False

    def _client_order_id(self, symbol: str) -> str:
        """
        Benzersiz emir kimliği — idempotency'nin temeli.

        Timeout aldığımızda emrin gidip gitmediğini bilemeyiz. Aynı emri
        körlemesine tekrar göndermek çift pozisyon açar. Bu kimlikle borsaya
        "şu emri gönderdim mi?" diye sorabiliyoruz.
        """
        self._order_seq = getattr(self, "_order_seq", 0) + 1
        stamp = int(datetime.now().timestamp() * 1000)
        return f"cb{stamp}{self._order_seq:03d}"[:36]

    def _recover_order(self, symbol: str, coid: str):
        """Timeout/hata sonrası emrin gerçek akıbetini borsadan öğren."""
        try:
            return self.client.futures_get_order(symbol=symbol, origClientOrderId=coid)
        except BinanceAPIException as e:
            if getattr(e, "code", None) == -2013:     # emir hiç oluşmamış
                return None
            logger.warning(f"Emir sorgulanamadı {symbol} ({coid}): {e}")
            return None
        except Exception as e:
            logger.warning(f"Emir sorgulanamadı {symbol} ({coid}): {e}")
            return None

    @staticmethod
    def _fill_of(order) -> tuple[float, float]:
        """(gerçekleşen miktar, ortalama fiyat) — ACK cevabında ikisi de 0'dır."""
        if not order:
            return 0.0, 0.0
        try:
            return float(order.get("executedQty") or 0.0), float(order.get("avgPrice") or 0.0)
        except (TypeError, ValueError):
            return 0.0, 0.0

    def _set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=MARGIN_MODE)
        except BinanceAPIException:
            pass
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except BinanceAPIException as e:
            logger.error(f"Kaldıraç ayarlanamadı {symbol} ({leverage}x): {e}")
            return False

    def _get_step(self, symbol: str) -> tuple[float, float]:
        if symbol in self._step_cache:
            return self._step_cache[symbol]
        self._load_step_cache()
        return self._step_cache.get(symbol, (0.001, 0.001))

    def open_position(self, symbol, side, price, margin, leverage, sl, tp,
                      reason="", score=0, btc_trend="SIDEWAYS",
                      market_regime="UNKNOWN", atr_pct=0.0, fear_greed=50,
                      strategy="TREND", qty=None, risk_amount=0.0,
                      entry_snapshot=None):
        if symbol in self.positions:
            return False, "Zaten açık"
        try:
            if not self._set_leverage(symbol, leverage):
                return False, f"Kaldıraç ayarlanamadı ({leverage}x), pozisyon açılmadı"

            step, min_q = self._get_step(symbol)
            target_qty = qty if qty is not None else (margin * leverage) / price
            q = _round_step(target_qty, step)
            if q < min_q:
                return False, f"Miktar küçük: {q} < {min_q}"

            # DENETİM BULGUSU: emrin varsayılan cevabı ACK'tir — "alındı",
            # "gerçekleşti" değil. Eski kod `avgPrice`'ı doğrudan okuyup
            # gerçekleşmiş sayıyordu; ACK'te o alan 0 gelir ve pozisyon
            # sinyaldeki fiyattan açılmış gibi kaydediliyordu. Timeout/5xx
            # durumunda ise emrin akıbeti hiç bilinmiyordu.
            #
            # Artık: benzersiz clientOrderId + RESULT cevabı + doğrulama.
            # clientOrderId sayesinde timeout sonrası emir KÖRLEMESİNE
            # tekrarlanmıyor, borsadan sorgulanıyor.
            coid = self._client_order_id(symbol)
            try:
                order = self.client.futures_create_order(
                    symbol=symbol, side=side, type="MARKET", quantity=q,
                    newClientOrderId=coid, newOrderRespType="RESULT",
                )
            except Exception as e:
                order = self._recover_order(symbol, coid)
                if order is None:
                    logger.error(f"Emir durumu bilinmiyor {symbol} ({coid}): {e}")
                    return False, f"Emir durumu doğrulanamadı: {e}"

            filled, ap = self._fill_of(order)
            if filled <= 0:
                order = self._recover_order(symbol, coid) or order
                filled, ap = self._fill_of(order)
            if filled <= 0:
                logger.error(f"Emir gerçekleşmedi {symbol} ({coid})")
                return False, "Emir gerçekleşmedi"
            if ap <= 0:
                ap = price
            # Kısmi dolum: gerçekten dolan miktarla devam et, hayali miktarla değil.
            if abs(filled - q) / max(q, 1e-9) > 0.001:
                logger.warning(f"{symbol}: kısmi dolum {filled}/{q} — gerçek miktar esas alındı")
            q = filled

            # Koruyucu stop — bot çökerse pozisyon korumasız kalmasın.
            # Bot yaşadığı sürece stop'u kendisi yönetir ve bu emri günceller.
            sl_side = "SELL" if side == "BUY" else "BUY"
            stop_order_id = self._place_stop(symbol, sl_side, sl)
            if stop_order_id is None:
                # DENETİM BULGUSU: timeout aldığında emrin GERÇEKTEN kurulup
                # kurulmadığı araştırılmıyordu. Doğrudan acil kapamaya
                # geçilirse hem stop hem kapama emri gönderilmiş olabilir.
                stop_order_id = self._find_live_stop(symbol, sl_side)
            if stop_order_id is None:
                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=sl_side, type="MARKET",
                        quantity=q, reduceOnly=True,
                    )
                except Exception as ce:
                    logger.error(f"Acil kapama da başarısız {symbol}: {ce}")
                return False, "SL kurulamadı, pozisyon koruma amaçlı kapatıldı"

            # DİKKAT: sabit TAKE_PROFIT_MARKET emri BİLEREK konmuyor.
            # Eski kod `closePosition=True` ile TP emri koyuyordu; bu, runner
            # mantığını gerçek modda tamamen devre dışı bırakıyordu (pozisyon
            # 3.5×ATR'de kesin kapanıyordu). Çıkış artık `position.evaluate()`
            # tarafından yönetilir ve ÜST LİMİTİ YOKTUR.

            notional = q * ap
            commission_open = notional * COMMISSION_RATE
            self.balance -= margin
            self.total_commission += commission_open

            pos = new_position(
                symbol=symbol, side=side, entry_price=ap, qty=q,
                margin=margin, leverage=leverage, stop_loss=sl, take_profit=tp,
                atr_pct=atr_pct or 0.01, strategy=strategy,
                reason_open=reason, score=score, btc_trend=btc_trend,
                market_regime=market_regime, fear_greed=fear_greed,
                entry_snapshot=entry_snapshot or {},
                open_commission=commission_open,
                stop_order_id=stop_order_id,
            )
            if risk_amount:
                pos["risk_amount"] = risk_amount
            self.positions[symbol] = pos

            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"OPEN {side}", price=ap, margin=margin, leverage=leverage,
            ))
            self._persist_positions()
            return True, "OK"
        except Exception as e:
            logger.error(f"Futures açma hatası {symbol}: {type(e).__name__}: {e}")
            return False, str(e)

    def _place_stop(self, symbol, sl_side, stop_price):
        """
        Borsaya koruyucu STOP_MARKET koy. Başarısızsa None döner — çağıran taraf
        bu ayrımı MUTLAKA dikkate almalı.
        """
        tick = self._get_tick(symbol)
        # sl_side "SELL" ise pozisyon LONG, stop fiyatın altında → aşağı yuvarla.
        px = _round_tick(stop_price, tick, side_up=(sl_side == "BUY"))
        try:
            o = self.client.futures_create_order(
                symbol=symbol, side=sl_side, type="STOP_MARKET",
                stopPrice=px, closePosition=True,
            )
            return o.get("orderId")
        except Exception as e:
            # Yalnız BinanceAPIException yakalamak yetmez: timeout/bağlantı
            # hatası da stop kurulamaması demektir ve aynı şekilde ele alınmalı.
            logger.error(f"SL order başarısız {symbol} @ {px}: {type(e).__name__}: {e}")
            return None

    def _find_live_stop(self, symbol: str, sl_side: str):
        """
        Borsada bu sembol için canlı bir koruyucu stop var mı?

        `_place_stop` timeout aldığında emir aslında kurulmuş olabilir. Sormadan
        acil kapamaya geçmek, iki emir birden göndermek demektir.
        """
        try:
            for o in self.client.futures_get_open_orders(symbol=symbol):
                if o.get("type") == "STOP_MARKET" and o.get("side") == sl_side:
                    logger.info(f"{symbol}: timeout'a rağmen stop kurulmuş (#{o.get('orderId')})")
                    return o.get("orderId")
        except Exception as e:
            logger.warning(f"Açık emirler okunamadı {symbol}: {e}")
        return None

    def _cancel_stop(self, symbol, order_id) -> bool:
        if not order_id:
            return True
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            return True
        except Exception as e:
            # -2011: emir zaten yok (tetiklenmiş veya iptal edilmiş) — sorun değil.
            if getattr(e, "code", None) == -2011:
                return True
            logger.warning(f"Eski stop iptal edilemedi {symbol}#{order_id}: {e}")
            return False

    def _sync_protective_stop(self, pos):
        """
        Bot stop'u taşıdığında borsadaki koruyucu emri de taşı.

        DENETİM BULGUSU (kritik): eski kod önce eski emri İPTAL EDİYOR, sonra
        yenisini kurmaya çalışıyordu. Yeni emir reddedilirse pozisyon tamamen
        korumasız kalıyor, üstelik `_exchange_stop` yine de yeni değere
        yazıldığı için bir sonraki tick eşiği geçemeyip TEKRAR DENEMİYORDU.
        Yani koruma sessizce ve kalıcı olarak kayboluyordu.

        Yeni sıra: ÖNCE yeni emri kur, BAŞARILIYSA eskisini iptal et. Böylece
        hiçbir an koruma boşluğu oluşmuyor. Yeni emir kurulamazsa eski stop
        yerinde kalır — daha geniş ama geçerli bir koruma.
        """
        symbol = pos["symbol"]
        new_stop = pos["stop_loss"]
        last = pos.get("_exchange_stop", pos.get("initial_stop"))
        if last and abs(new_stop - last) / max(last, 1e-9) < self.STOP_UPDATE_THRESHOLD:
            return

        sl_side = "SELL" if pos["side"] == "BUY" else "BUY"
        old_id = pos.get("stop_order_id")

        new_id = self._place_stop(symbol, sl_side, new_stop)
        if new_id is not None:
            # DENETİM BULGUSU: iptalin BAŞARISI kontrol edilmiyordu. İptal
            # başarısızsa borsada iki stop kalıyor ve eski emrin kimliği
            # kayboluyordu — bir daha temizlenemez hâle geliyordu.
            # (İki closePosition stopu güvenli tarafta hata verir: hangisi
            # önce tetiklenirse pozisyon kapanır. Ama takip edilmeli.)
            if not self._cancel_stop(symbol, old_id):
                pos.setdefault("stale_stop_ids", []).append(old_id)
                logger.warning(f"{symbol}: eski stop #{old_id} iptal edilemedi, takibe alındı")
            for sid in list(pos.get("stale_stop_ids", [])):
                if self._cancel_stop(symbol, sid):
                    pos["stale_stop_ids"].remove(sid)
            pos["stop_order_id"] = new_id
            pos["_exchange_stop"] = new_stop
            self.unprotected.pop(symbol, None)
            return

        # Yeni emir kurulamadı. Eskisi hâlâ duruyorsa pozisyon korumalı —
        # `_exchange_stop`'a DOKUNMUYORUZ ki bir sonraki tick yeniden denesin.
        if old_id and self._verify_stop_exists(symbol, old_id):
            logger.warning(
                f"{symbol}: stop güncellenemedi, eski koruma ({last}) yerinde. "
                f"Sonraki tick'te tekrar denenecek."
            )
            return

        # Ne yeni ne eski koruma var. Bu, pozisyonun gerçekten korumasız olduğu
        # tek durum — sessizce geçilemez.
        self._mark_unprotected(pos, "stop güncellenemedi ve eski emir de yok")

    def _verify_stop_exists(self, symbol, order_id) -> bool:
        try:
            o = self.client.futures_get_order(symbol=symbol, orderId=order_id)
            return o.get("status") in ("NEW", "PARTIALLY_FILLED")
        except Exception as e:
            logger.warning(f"Stop durumu okunamadı {symbol}#{order_id}: {e}")
            return False

    def _mark_unprotected(self, pos, reason: str):
        """
        Korumasız pozisyonu kaydet ve acil kapamayı dene.

        Korumasız açık pozisyon, botun girebileceği en kötü durum: kaldıraçlı ve
        stop'suz. Kapatma da başarısız olursa pozisyon `unprotected` içinde kalır
        ve main.py yeni işlem açmayı durdurup alarm üretir.
        """
        symbol = pos["symbol"]
        self.unprotected[symbol] = reason
        logger.error(f"KORUMASIZ POZİSYON {symbol}: {reason} — acil kapama deneniyor")
        try:
            step, _ = self._get_step(symbol)
            close_side = "SELL" if pos["side"] == "BUY" else "BUY"
            self.client.futures_create_order(
                symbol=symbol, side=close_side, type="MARKET",
                quantity=_round_step(pos["qty"], step), reduceOnly=True,
            )
        except BinanceAPIException as e:
            logger.error(f"Acil kapama başarısız {symbol}: {e}")
            return
        # Emir gönderildi diye kapandığını VARSAYMIYORUZ — borsadan doğrula.
        if self._position_amount(symbol) == 0.0:
            self.unprotected.pop(symbol, None)
            self.positions.pop(symbol, None)
            self._persist_positions()
            logger.info(f"{symbol} korumasız kalınca kapatıldı ve doğrulandı")
        else:
            logger.error(f"{symbol} kapama emri gönderildi ama pozisyon hâlâ açık")

    def _position_amount(self, symbol: str) -> float:
        try:
            raw = self.client.futures_position_information(symbol=symbol)
            return sum(abs(float(p.get("positionAmt", 0))) for p in raw)
        except Exception as e:
            logger.warning(f"Pozisyon miktarı okunamadı {symbol}: {e}")
            return float("nan")   # bilinmiyor — "kapandı" sayma

    def _do_partial(self, pos, qty_close, price) -> bool:
        symbol = pos["symbol"]
        try:
            step, min_q = self._get_step(symbol)
            q = _round_step(qty_close, step)
            if q < min_q:
                logger.debug(f"Kısmi çıkış atlandı {symbol}: miktar {q} < {min_q}")
                return False
            close_side = "SELL" if pos["side"] == "BUY" else "BUY"
            self.client.futures_create_order(
                symbol=symbol, side=close_side, type="MARKET",
                quantity=q, reduceOnly=True,
            )
            return True
        except BinanceAPIException as e:
            logger.error(f"Kısmi çıkış hatası {symbol}: {e}")
            return False

    def close_position(self, symbol, price, reason=EXIT_MANUAL):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        try:
            close_side = "SELL" if pos["side"] == "BUY" else "BUY"
            step, min_q = self._get_step(symbol)
            qty = _round_step(pos["qty"], step)
            if qty >= min_q:
                # DENETİM BULGUSU: kapanış emri gönderilip SONUCU HİÇ
                # DOĞRULANMIYORDU. Timeout'ta veya kısmi dolumda Binance'de
                # pozisyon açık kalırken bot onu kapanmış sayıp yerel kayıttan
                # düşürüyordu — canlı pozisyon sahipsiz kalıyordu.
                coid = self._client_order_id(symbol)
                try:
                    o = self.client.futures_create_order(
                        symbol=symbol, side=close_side, type="MARKET",
                        quantity=qty, reduceOnly=True,
                        newClientOrderId=coid, newOrderRespType="RESULT",
                    )
                except Exception as oe:
                    o = self._recover_order(symbol, coid)
                    if o is None:
                        self.positions[symbol] = pos      # geri koy, kaybetme
                        logger.error(f"Kapama emrinin akıbeti bilinmiyor {symbol}: {oe}")
                        return False, f"Kapama doğrulanamadı: {oe}"

                filled, _ = self._fill_of(o)
                remaining = self._position_amount(symbol)
                # Tek geçerli kanıt: borsada pozisyon kalmamış olması.
                if remaining != 0.0:
                    self.positions[symbol] = pos
                    self.unprotected[symbol] = (
                        f"kapama tamamlanmadı (dolan {filled}, kalan {remaining})"
                    )
                    logger.error(
                        f"{symbol}: kapama emri gönderildi ama pozisyon hâlâ açık "
                        f"(dolan {filled}, kalan {remaining})"
                    )
                    return False, "Kapama tamamlanmadı"
                self.unprotected.pop(symbol, None)
            if pos.get("stop_order_id"):
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=pos["stop_order_id"])
                except BinanceAPIException:
                    pass

            gross = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                     else (pos["entry_price"] - price)) * qty
            comm  = qty * price * COMMISSION_RATE
            net   = gross - comm

            self.balance += pos["margin"] + net
            self.total_pnl += net
            self.total_commission += comm

            total_net  = net + pos.get("realized_pnl", 0.0)
            total_comm = comm + pos.get("realized_commission", 0.0) + pos.get("open_commission", 0.0)
            total_gross= total_net + total_comm

            if total_net >= 0: self.wins += 1
            else:              self.losses += 1

            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"CLOSE ({reason})", price=price, pnl=net,
            ))
            self._record_trade(pos, price, reason, total_gross, total_comm, total_net)
            self._persist_positions()
            return True, total_net
        except BinanceAPIException as e:
            # DENETİM: burada -1111, -2022, -4061 ve -2011 "pozisyon zaten
            # kapalı" sayılıyordu. HİÇBİRİ bunu kanıtlamaz:
            #   -1111 fiyat/miktar biçimi   -2022 reduceOnly reddedildi
            #   -4061 positionSide uyuşmadı -2011 iptal reddedildi
            # Hepsi pozisyon AÇIKKEN de dönebilir. Kapanmış saymak, canlı bir
            # pozisyonu yerel takipten düşürüp sahipsiz bırakıyordu.
            # Tek geçerli kanıt borsadaki positionAmt == 0.
            amt = self._position_amount(symbol)
            if amt == 0.0:
                logger.info(f"Pozisyon Binance'de kapalı doğrulandı ({symbol}), sync yapılıyor")
                self.sync_balance()
                self._persist_positions()
                return True, 0.0
            self.positions[symbol] = pos
            logger.error(f"Futures kapama hatası {symbol}: {e}")
            return False, str(e)

    _record_trade = PaperPortfolio._record_trade

    def restore_positions(self, saved: dict):
        """
        Restart sonrası pozisyonları geri yükle — ve BORSADAN DOĞRULA.

        DENETİM BULGUSU: temel sınıf yalnızca veritabanını okuyordu. Bot kapalıyken
        pozisyon kapanmış olabilir, kısmen kapanmış olabilir veya koruyucu stop
        tetiklenip yok olmuş olabilir. Doğrulamadan devam etmek, botun var olmayan
        bir pozisyonu yönetmesi ya da korumasız gerçek bir pozisyonu korumalı
        sanması demek. `Restart=always` altında bu her çökmede tekrarlanır.
        """
        restored = super().restore_positions(saved)
        if not self.positions:
            self._detect_orphans()
            return restored

        try:
            raw = self.client.futures_position_information()
        except BinanceAPIException as e:
            # Borsayı okuyamıyorsak hiçbir şeyi doğrulanmış sayamayız.
            logger.error(f"Restart doğrulaması yapılamadı: {e} — tüm pozisyonlar şüpheli")
            for sym in self.positions:
                self.unprotected[sym] = "restart doğrulanamadı"
            return restored

        # DENETİM BULGUSU: burada `abs()` kullanılıyordu, yani YÖN kayboluyordu.
        # Kayıtlı bir LONG, borsadaki gerçek bir SHORT ile eşleşebiliyordu — bot
        # short'u long sanıp stop'u YANLIŞ TARAFA koyardı. İşareti koruyoruz.
        live = {}
        for p in raw:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            live[p["symbol"]] = {
                "qty": abs(amt),
                "side": "BUY" if amt > 0 else "SELL",
                "position_side": p.get("positionSide", "BOTH"),
            }

        # Hedge mode desteklenmiyor: LONG/SHORT ayrı bacaklar bu kodun
        # varsayımlarını bozar. Tespit edilirse yönetmeye ÇALIŞMIYORUZ.
        hedge = [s for s, v in live.items() if v["position_side"] not in ("BOTH", None)]
        if hedge:
            for s in hedge:
                self.unprotected[s] = "hedge mode — bu bot one-way mode varsayar"
            logger.error(f"HEDGE MODE tespit edildi: {hedge} — elle müdahale gerekiyor")

        for sym in list(self.positions):
            pos = self.positions[sym]
            if sym not in live:
                logger.info(f"RESTART: {sym} bot kapalıyken kapanmış, kayıttan düşürüldü")
                self.positions.pop(sym)
                continue

            # Miktar değişmişse (kısmi çıkış veya elle müdahale) borsayı esas al.
            # YÖN kontrolü miktardan ÖNCE: yanlış yöne stop koymak, korumasız
            # kalmaktan daha kötüdür — zararı hızlandırır.
            if live[sym]["side"] != pos["side"]:
                logger.error(
                    f"RESTART: {sym} YÖN UYUŞMUYOR — kayıt {pos['side']}, "
                    f"borsa {live[sym]['side']}. Pozisyon yönetilmiyor."
                )
                self.unprotected[sym] = (
                    f"yön uyuşmazlığı (kayıt {pos['side']}, borsa {live[sym]['side']})"
                )
                self.positions.pop(sym)
                continue

            if abs(live[sym]["qty"] - pos["qty"]) / max(pos["qty"], 1e-9) > 0.01:
                logger.warning(
                    f"RESTART: {sym} miktar uyuşmuyor — kayıt {pos['qty']}, "
                    f"borsa {live[sym]['qty']}. Borsa esas alındı."
                )
                pos["qty"] = live[sym]["qty"]

            # Koruyucu stop hâlâ duruyor mu?
            if self._verify_stop_exists(sym, pos.get("stop_order_id")):
                continue
            logger.warning(f"RESTART: {sym} koruyucu stop'u yok, yeniden kuruluyor")
            sl_side = "SELL" if pos["side"] == "BUY" else "BUY"
            new_id = self._place_stop(sym, sl_side, pos["stop_loss"])
            if new_id is not None:
                pos["stop_order_id"] = new_id
                pos["_exchange_stop"] = pos["stop_loss"]
            else:
                self._mark_unprotected(pos, "restart sonrası stop kurulamadı")

        self._persist_positions()
        self._detect_orphans(live)
        return len(self.positions)

    def _detect_orphans(self, live: dict | None = None):
        """
        Binance'de açık olup yerel kaydı olmayan pozisyonları yakala.

        DENETİM BULGUSU: `sync_positions` yalnız tek yöne bakıyordu (yerelde olup
        borsada olmayan). Ters yön hiç yönetilmiyordu. Bu, `open_position`
        piyasa emrini gönderdikten sonra ama `_persist_positions()` çalışmadan
        önce süreç ölürse gerçekleşir: pozisyon borsada açık, bot habersiz.
        """
        if live is None:
            try:
                raw = self.client.futures_position_information()
                live = {}
                for p in raw:
                    amt = float(p.get("positionAmt", 0))
                    if amt != 0:
                        live[p["symbol"]] = {"qty": abs(amt),
                                             "side": "BUY" if amt > 0 else "SELL"}
            except BinanceAPIException as e:
                logger.warning(f"Sahipsiz pozisyon taraması yapılamadı: {e}")
                return

        for sym in live:
            if sym in self.positions:
                self.orphans.pop(sym, None)
                continue
            if sym in self.orphans:
                continue
            info = live[sym]
            desc = info["qty"] if isinstance(info, dict) else info
            side = info.get("side", "?") if isinstance(info, dict) else "?"
            self.orphans[sym] = f"borsada {side} {desc} açık, yerel kaydı yok"
            logger.error(f"SAHİPSİZ POZİSYON {sym}: {self.orphans[sym]}")

    def sync_positions(self) -> set[str]:
        """
        Binance'deki gerçek pozisyonlarla local state'i eşitle.

        Koruyucu stop emri tetiklenirse pozisyon bot'un haberi olmadan kapanır;
        bu metod onu yakalar ve gerçekleşen PnL ile kaydeder.
        """
        try:
            raw = self.client.futures_position_information()
            binance_open = {
                p["symbol"] for p in raw
                if abs(float(p.get("positionAmt", 0))) > 0
            }

            for sym in list(self.positions.keys()):
                if sym not in binance_open:
                    pos = self.positions.pop(sym)
                    pnl = 0.0
                    close_px = pos["entry_price"]
                    try:
                        since = int(pos["open_time"].timestamp() * 1000)
                        income = self.client.futures_income_history(
                            symbol=sym, incomeType="REALIZED_PNL",
                            startTime=since, limit=50,
                        )
                        pnl = sum(float(i["income"]) for i in income)
                    except Exception:
                        pass
                    try:
                        tk = self.client.futures_symbol_ticker(symbol=sym)
                        close_px = float(tk["price"])
                    except Exception:
                        pass

                    self.total_pnl += pnl
                    total_net = pnl + pos.get("realized_pnl", 0.0)
                    if total_net >= 0: self.wins += 1
                    else:              self.losses += 1

                    comm = pos.get("open_commission", 0.0) + pos.get("realized_commission", 0.0) \
                           + pos["qty"] * close_px * COMMISSION_RATE
                    logger.info(f"SYNC: {sym} Binance'de kapandı | PnL:{total_net:+.2f}$")
                    # DENETİM BULGUSU: bu kapanış DB'ye yazılıyor ama kill
                    # switch'e HİÇ ulaşmıyordu. Gerçek modda borsa stopu en sık
                    # kapanış yoludur; günlük zarar, ardışık kayıp, cooldown ve
                    # risk bütçesi bu işlemler için hiç güncellenmiyordu — yani
                    # kill switch gerçek zararı göremiyordu.
                    risk = pos.get("risk_amount", 0.0)
                    self.exchange_closed.append((
                        sym, "EXCHANGE_STOP", total_net,
                        round(total_net / risk, 3) if risk > 0 else 0.0,
                        pos.get("allocated_r", 1.0),
                    ))
                    self._record_trade(
                        pos, close_px, "EXCHANGE_STOP",
                        total_net + comm, comm, total_net,
                    )

            if binance_open != set(self.positions):
                self.sync_balance()

            self._detect_orphans({s: {"qty": 1.0, "side": "?"} for s in binance_open})
            self._persist_positions()
            return binance_open
        except BinanceAPIException as e:
            logger.error(f"Pozisyon sync hatası: {e}")
            return set()

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Factory ───────────────────────────────────────────────────────────────────

def create_trader():
    if PAPER_TRADING:
        logger.info("PAPER TRADING modu")
        return PaperPortfolio(PAPER_BALANCE)
    if SPOT_MODE:
        logger.info("GERÇEK SPOT işlem modu")
        return SpotTrader()
    logger.info("GERÇEK FUTURES işlem modu — kaldıraç + long/short")
    return FuturesTrader()
