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
    API_KEY, API_SECRET, PAPER_TRADING, PAPER_BALANCE, SPOT_MODE,
    COMMISSION_RATE,
)
from position import (
    new_position, evaluate, position_summary, realized_r,
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

    def _apply_partial(self, pos, fraction, price, reason):
        """Pozisyonun bir kısmını realize et; kalan koşmaya devam eder."""
        qty_close = pos["initial_qty"] * fraction
        qty_close = min(qty_close, pos["qty"])
        if qty_close <= 0:
            return
        ok = self._do_partial(pos, qty_close, price)
        if not ok:
            return

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

        gross = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                 else (pos["entry_price"] - price)) * qty
        comm  = qty * price * COMMISSION_RATE
        net   = gross - comm

        self.balance += pos["margin"] + net
        self.total_pnl += net
        self.total_commission += comm

        total_net   = net + pos.get("realized_pnl", 0.0)
        total_comm  = comm + pos.get("realized_commission", 0.0) + pos.get("open_commission", 0.0)
        total_gross = total_net + total_comm

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

    @property
    def equity(self):
        """Bakiye + açık pozisyonların bloke teminatı."""
        return self.balance + sum(p.get("margin", 0.0) for p in self.positions.values())

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Spot Trader ───────────────────────────────────────────────────────────────

class SpotTrader(ExitEngineMixin):

    def __init__(self):
        self.client    = Client(API_KEY, API_SECRET)
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
        self.client    = Client(API_KEY, API_SECRET)
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.total_commission = 0.0
        self.wins = self.losses = 0
        self.balance = 0.0
        self._step_cache: dict[str, tuple[float, float]] = {}
        self._load_step_cache()

    def _load_step_cache(self):
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_cache[s["symbol"]] = (float(f["stepSize"]), float(f["minQty"]))
                        break
            logger.info(f"Step cache yüklendi: {len(self._step_cache)} sembol")
        except Exception as e:
            logger.error(f"Step cache yüklenemedi: {e}")

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

    def _set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
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

            order = self.client.futures_create_order(
                symbol=symbol, side=side, type="MARKET", quantity=q,
            )
            ap = float(order.get("avgPrice") or price)

            # Koruyucu stop — bot çökerse pozisyon korumasız kalmasın.
            # Bot yaşadığı sürece stop'u kendisi yönetir ve bu emri günceller.
            sl_side = "SELL" if side == "BUY" else "BUY"
            stop_order_id = self._place_stop(symbol, sl_side, sl)
            if stop_order_id is None:
                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=sl_side, type="MARKET",
                        quantity=q, reduceOnly=True,
                    )
                except BinanceAPIException as ce:
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
        except BinanceAPIException as e:
            logger.error(f"Futures açma hatası {symbol}: {e}")
            return False, str(e)

    def _place_stop(self, symbol, sl_side, stop_price):
        try:
            o = self.client.futures_create_order(
                symbol=symbol, side=sl_side, type="STOP_MARKET",
                stopPrice=round(stop_price, 6), closePosition=True,
            )
            return o.get("orderId")
        except BinanceAPIException as e:
            logger.error(f"SL order başarısız {symbol}: {e}")
            return None

    def _sync_protective_stop(self, pos):
        """Bot stop'u taşıdığında borsadaki koruyucu emri de taşı."""
        symbol = pos["symbol"]
        new_stop = pos["stop_loss"]
        last = pos.get("_exchange_stop", pos.get("initial_stop"))
        if last and abs(new_stop - last) / max(last, 1e-9) < self.STOP_UPDATE_THRESHOLD:
            return
        old_id = pos.get("stop_order_id")
        if old_id:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=old_id)
            except BinanceAPIException:
                pass
        sl_side = "SELL" if pos["side"] == "BUY" else "BUY"
        new_id = self._place_stop(symbol, sl_side, new_stop)
        pos["stop_order_id"] = new_id
        pos["_exchange_stop"] = new_stop

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
                self.client.futures_create_order(
                    symbol=symbol, side=close_side,
                    type="MARKET", quantity=qty, reduceOnly=True,
                )
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
            already_closed_codes = {-2022, -4061, -2011, -1111}
            if hasattr(e, "code") and e.code in already_closed_codes:
                logger.info(f"Pozisyon Binance'de zaten kapanmış ({symbol}), sync yapılıyor")
                self.sync_balance()
                self._persist_positions()
                return True, 0.0
            self.positions[symbol] = pos
            logger.error(f"Futures kapama hatası {symbol}: {e}")
            return False, str(e)

    _record_trade = PaperPortfolio._record_trade

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
                    self._record_trade(
                        pos, close_px, "EXCHANGE_STOP",
                        total_net + comm, comm, total_net,
                    )

            if binance_open != set(self.positions):
                self.sync_balance()

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
