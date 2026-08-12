"""
Trader modülleri:
- PaperPortfolio : simülasyon
- SpotTrader     : kaldıraçsız spot
- FuturesTrader  : kaldıraçlı long/short (aktif)
"""

import logging
import math
import time
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import TRAIL_ATR_MULT, TRAIL_MIN_LOCK_PCT, API_KEY, API_SECRET, PAPER_TRADING, PAPER_BALANCE, SPOT_MODE, COMMISSION_RATE
from risk_manager import should_close_position
from database import save_trade

logger = logging.getLogger("trader")


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _round_step(qty: float, step: float) -> float:
    if step == 0:
        return qty
    precision = int(round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)


# ── Paper Portfolio ───────────────────────────────────────────────────────────

class PaperPortfolio:

    def __init__(self, initial_balance: float):
        self.balance   = initial_balance
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.wins = self.losses = 0

    def open_position(self, symbol, side, price, margin, leverage, sl, tp,
                      reason="", score=0, btc_trend="SIDEWAYS",
                      market_regime="UNKNOWN", atr_pct=0.0, fear_greed=50):
        if symbol in self.positions:
            return False, "Zaten açık"
        qty = (margin * leverage) / price
        commission_open = (margin * leverage) * COMMISSION_RATE
        self.balance -= margin + commission_open
        self.total_commission = getattr(self, "total_commission", 0.0) + commission_open
        self.positions[symbol] = dict(
            symbol=symbol, side=side, entry_price=price,
            qty=qty, margin=margin, leverage=leverage,
            stop_loss=sl, take_profit=tp, open_time=datetime.now(),
            reason_open=reason, score=score, btc_trend=btc_trend,
            market_regime=market_regime, atr_pct=atr_pct, fear_greed=fear_greed,
        )
        self.trade_log.append(dict(
            time=datetime.now(), symbol=symbol,
            action=f"OPEN {side}", price=price, margin=margin, leverage=leverage,
        ))
        return True, "OK"

    def close_position(self, symbol, price, reason="MANUAL"):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        gross_pnl = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                     else (pos["entry_price"] - price)) * pos["qty"]
        commission_close = (pos["margin"] * pos["leverage"]) * COMMISSION_RATE
        self.total_commission = getattr(self, "total_commission", 0.0) + commission_close
        net_pnl = gross_pnl - commission_close
        self.balance += pos["margin"] + net_pnl
        self.total_pnl += net_pnl
        if net_pnl >= 0: self.wins   += 1
        else:            self.losses += 1
        self.trade_log.append(dict(
            time=datetime.now(), symbol=symbol,
            action=f"CLOSE ({reason})", price=price, pnl=net_pnl,
        ))
        # Veritabanına kaydet
        try:
            save_trade(
                symbol=symbol, side=pos["side"],
                entry_price=pos["entry_price"], close_price=price,
                margin=pos["margin"], leverage=pos["leverage"],
                gross_pnl=gross_pnl,
                commission=commission_close + (pos["margin"] * pos["leverage"]) * COMMISSION_RATE,
                net_pnl=net_pnl,
                reason_open=pos.get("reason_open", ""),
                reason_close=reason,
                open_time=pos["open_time"], close_time=datetime.now(),
                score=pos.get("score", 0),
                btc_trend=pos.get("btc_trend", "SIDEWAYS"),
                market_regime=pos.get("market_regime", "UNKNOWN"),
                atr_pct=pos.get("atr_pct", 0.0),
                fear_greed=pos.get("fear_greed", 50),
            )
        except Exception:
            pass
        return True, net_pnl

    def check_positions(self, prices):
        closed = []
        for sym, pos in list(self.positions.items()):
            cur = prices.get(sym)
            if cur is None: continue

            # Trailing stop: TP'nin %50'sine ulaşınca SL → breakeven,
            # %75'inde SL → TP yönünde yarı mesafede kilitle
            entry = pos["entry_price"]
            sl    = pos["stop_loss"]
            tp    = pos["take_profit"]
            if pos["side"] == "BUY":
                profit_dist = tp - entry
                cur_profit  = cur - entry
                if profit_dist > 0:
                    if cur_profit >= profit_dist * 0.75:
                        new_sl = cur - profit_dist * 0.50
                        if new_sl > sl:
                            pos["stop_loss"] = new_sl
                    elif cur_profit >= profit_dist * 0.50 and sl < entry:
                        pos["stop_loss"] = entry
            else:
                profit_dist = entry - tp
                cur_profit  = entry - cur
                if profit_dist > 0:
                    if cur_profit >= profit_dist * 0.75:
                        new_sl = cur + profit_dist * 0.50
                        if new_sl < sl:
                            pos["stop_loss"] = new_sl
                    elif cur_profit >= profit_dist * 0.50 and sl > entry:
                        pos["stop_loss"] = entry

            # TP aşıldıysa (trailing modu): stop'u en iyi fiyatın ATR kadar
            # gerisinden sürükle. Böylece hareket devam ettikçe kâr büyür,
            # dönüş başlayınca stop'a değip çıkılır.
            if pos.get("trailing"):
                atr_abs = pos.get("atr_pct", 0.01) * entry
                pad = max(atr_abs * TRAIL_ATR_MULT, entry * TRAIL_MIN_LOCK_PCT)
                if pos["side"] == "BUY":
                    pos["peak"] = max(pos.get("peak", cur), cur)
                    # En az TRAIL_MIN_LOCK_PCT kâr her hâlükârda kilitli kalır.
                    floor_sl = entry * (1 + TRAIL_MIN_LOCK_PCT)
                    new_sl = max(pos["peak"] - pad, floor_sl)
                    if new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                else:
                    pos["peak"] = min(pos.get("peak", cur), cur)
                    floor_sl = entry * (1 - TRAIL_MIN_LOCK_PCT)
                    new_sl = min(pos["peak"] + pad, floor_sl)
                    if new_sl < pos["stop_loss"]:
                        pos["stop_loss"] = new_sl

            hit, reason = should_close_position(pos, cur)
            if hit:
                ok, pnl = self.close_position(sym, cur, reason)
                if ok: closed.append((sym, reason, pnl))
        return closed

    def get_balance(self): return self.balance
    def sync_balance(self): return self.balance

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Spot Trader ───────────────────────────────────────────────────────────────

class SpotTrader:

    def __init__(self):
        self.client    = Client(API_KEY, API_SECRET)
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
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

    def sell_asset_to_usdt(self, asset: str) -> float:
        try:
            acc = self.client.get_account()
            qty = next((float(b["free"]) for b in acc["balances"] if b["asset"] == asset), 0.0)
            if qty <= 0: return 0.0
            symbol = f"{asset}USDT"
            info   = self.client.get_symbol_info(symbol)
            step   = next((float(f["stepSize"]) for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.001)
            min_q  = next((float(f["minQty"])   for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            qty    = _round_step(qty, step)
            if qty < min_q:
                logger.warning(f"{asset} miktarı çok küçük: {qty} < {min_q}")
                return 0.0
            order = self.client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
            usdt  = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            self.balance += usdt
            logger.info(f"{asset} satıldı: {qty} → {usdt:.4f} USDT")
            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"SELL {asset}→USDT", price=usdt/qty if qty else 0, margin=usdt,
            ))
            return usdt
        except Exception as e:
            logger.error(f"{asset} satış hatası: {e}")
            return 0.0

    def open_position(self, symbol, side, price, margin, leverage, sl, tp, **kwargs):
        if side != "BUY" or symbol in self.positions:
            return False, "Sadece BUY / zaten açık"
        try:
            info  = self.client.get_symbol_info(symbol)
            step  = next((float(f["stepSize"]) for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.001)
            min_q = next((float(f["minQty"])   for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            min_n = next((float(f.get("minNotional", f.get("minVal", 5.0)))
                          for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL","NOTIONAL")), 5.0)
            spend = min(margin, self.balance * 0.995)
            if spend < max(min_n, 1.0):
                return False, f"Yetersiz ({spend:.2f}$ < {min_n}$)"
            qty = _round_step(spend / price, step)
            if qty < min_q:
                return False, f"Miktar küçük"
            order = self.client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
            ap    = float(order["fills"][0]["price"]) if order.get("fills") else price
            aq    = float(order["executedQty"])
            asp   = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            self.balance -= asp
            self.positions[symbol] = dict(
                symbol=symbol, side="BUY", entry_price=ap, qty=aq,
                margin=asp, leverage=1, stop_loss=sl, take_profit=tp,
                open_time=datetime.now(),
            )
            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action="OPEN BUY", price=ap, margin=asp, leverage=1,
            ))
            return True, "OK"
        except BinanceAPIException as e:
            return False, str(e)

    def close_position(self, symbol, price, reason="MANUAL"):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        try:
            info  = self.client.get_symbol_info(symbol)
            step  = next((float(f["stepSize"]) for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.001)
            min_q = next((float(f["minQty"])   for f in info["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            qty   = _round_step(pos["qty"], step)
            if qty < min_q:
                self.positions[symbol] = pos
                return False, "Miktar küçük"
            order = self.client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
            back  = sum(float(f["qty"]) * float(f["price"]) for f in order.get("fills", []))
            pnl   = back - pos["margin"]
            self.balance += back
            self.total_pnl += pnl
            if pnl >= 0: self.wins   += 1
            else:        self.losses += 1
            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"CLOSE ({reason})", price=price, pnl=pnl,
            ))
            return True, pnl
        except BinanceAPIException as e:
            self.positions[symbol] = pos
            return False, str(e)

    def check_positions(self, prices):
        closed = []
        for sym, pos in list(self.positions.items()):
            cur = prices.get(sym)
            if cur is None: continue
            hit, reason = should_close_position(pos, cur)
            if hit:
                ok, pnl = self.close_position(sym, cur, reason)
                if ok: closed.append((sym, reason, pnl))
        return closed

    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0


# ── Futures Trader ────────────────────────────────────────────────────────────

class FuturesTrader:

    def __init__(self):
        self.client    = Client(API_KEY, API_SECRET)
        self.positions = {}
        self.trade_log = []
        self.total_pnl = 0.0
        self.wins = self.losses = 0
        self.balance = 0.0
        self._step_cache: dict[str, tuple[float, float]] = {}
        self._load_step_cache()

    def _load_step_cache(self):
        """Tüm Futures sembollerinin lot size bilgisini başlangıçta yükle."""
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                sym = s["symbol"]
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_cache[sym] = (float(f["stepSize"]), float(f["minQty"]))
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

    def transfer_spot_to_futures(self, amount: float) -> bool:
        """Spot cüzdandan Futures cüzdana USDT transfer et."""
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
            pass  # zaten ISOLATED ise Binance hata döner — normal
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except BinanceAPIException as e:
            logger.error(f"Kaldıraç ayarlanamadı {symbol} ({leverage}x): {e}")
            return False

    def _get_step(self, symbol: str) -> tuple[float, float]:
        if symbol in self._step_cache:
            return self._step_cache[symbol]
        # Cache miss — tek sembolü yeniden dene ve cache'e ekle
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                sym = s["symbol"]
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_cache[sym] = (float(f["stepSize"]), float(f["minQty"]))
                        break
            if symbol in self._step_cache:
                return self._step_cache[symbol]
        except Exception:
            pass
        return 0.001, 0.001

    def open_position(self, symbol, side, price, margin, leverage, sl, tp, **kwargs):
        if symbol in self.positions:
            return False, "Zaten açık"
        try:
            # Kaldıraç ayarlanamıyorsa pozisyon açma — yanlış kaldıraçla işlem riski
            if not self._set_leverage(symbol, leverage):
                return False, f"Kaldıraç ayarlanamadı ({leverage}x), pozisyon açılmadı"

            step, min_q = self._get_step(symbol)
            qty = _round_step((margin * leverage) / price, step)
            if qty < min_q:
                return False, f"Miktar küçük: {qty} < {min_q}"

            order = self.client.futures_create_order(
                symbol=symbol, side=side, type="MARKET", quantity=qty,
            )
            ap = float(order.get("avgPrice") or price)

            # Stop-loss order — başarısız olursa pozisyonu derhal kapat
            sl_side = "SELL" if side == "BUY" else "BUY"
            sl_placed = False
            try:
                self.client.futures_create_order(
                    symbol=symbol, side=sl_side, type="STOP_MARKET",
                    stopPrice=round(sl, 6), closePosition=True,
                )
                sl_placed = True
            except BinanceAPIException as e:
                logger.error(f"SL order başarısız {symbol}: {e} — pozisyon acil kapatılıyor")

            if not sl_placed:
                # SL koruması yok → derhal market ile kapat
                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=sl_side, type="MARKET",
                        quantity=qty, reduceOnly=True,
                    )
                except BinanceAPIException as ce:
                    logger.error(f"Acil kapama da başarısız {symbol}: {ce}")
                return False, "SL kurulamadı, pozisyon koruma amaçlı kapatıldı"

            # Take-profit order — başarısız olsa da pozisyon SL korumalı, devam et
            try:
                self.client.futures_create_order(
                    symbol=symbol, side=sl_side, type="TAKE_PROFIT_MARKET",
                    stopPrice=round(tp, 6), closePosition=True,
                )
            except BinanceAPIException as e:
                logger.warning(f"TP order hatası {symbol}: {e} — SL aktif, pozisyon korunuyor")

            self.balance -= margin
            self.positions[symbol] = dict(
                symbol=symbol, side=side, entry_price=ap, qty=qty,
                margin=margin, leverage=leverage,
                stop_loss=sl, take_profit=tp, open_time=datetime.now(),
            )
            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"OPEN {side}", price=ap, margin=margin, leverage=leverage,
            ))
            return True, "OK"
        except BinanceAPIException as e:
            logger.error(f"Futures açma hatası {symbol}: {e}")
            return False, str(e)

    def close_position(self, symbol, price, reason="MANUAL"):
        if symbol not in self.positions:
            return False, "Yok"
        pos = self.positions.pop(symbol)
        try:
            close_side = "SELL" if pos["side"] == "BUY" else "BUY"
            step, min_q = self._get_step(symbol)
            qty = _round_step(pos["qty"], step)
            self.client.futures_create_order(
                symbol=symbol, side=close_side,
                type="MARKET", quantity=qty, reduceOnly=True,
            )
            pnl = ((price - pos["entry_price"]) if pos["side"] == "BUY"
                   else (pos["entry_price"] - price)) * pos["qty"]
            self.balance += pos["margin"] + pnl
            self.total_pnl += pnl
            if pnl >= 0: self.wins   += 1
            else:        self.losses += 1
            self.trade_log.append(dict(
                time=datetime.now(), symbol=symbol,
                action=f"CLOSE ({reason})", price=price, pnl=pnl,
            ))
            return True, pnl
        except BinanceAPIException as e:
            # -2022 / -4061: pozisyon Binance'de zaten kapalı (SL/TP çalışmış)
            # Bu durumda pozisyonu hafızaya GERİ KOYMA — kapalı kabul et
            already_closed_codes = {-2022, -4061, -2011, -1111}
            if hasattr(e, "code") and e.code in already_closed_codes:
                logger.info(f"Pozisyon Binance'de zaten kapanmış ({symbol}), sync yapılıyor")
                self.sync_balance()
                return True, 0.0
            # Gerçek hata: pozisyonu geri koy, tekrar denensin
            self.positions[symbol] = pos
            logger.error(f"Futures kapama hatası {symbol}: {e}")
            return False, str(e)

    def sync_positions(self) -> set[str]:
        """
        Binance'deki gerçek açık pozisyonlarla local state'i senkronize et.

        Binance SL/TP bir pozisyonu kapattığında bot bunu bilmiyor.
        Bu metod her döngüde çağrılarak kapanmış pozisyonları tespit eder,
        gerçekleşen PnL'i kaydeder ve local state'den siler.
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
                    # Gerçekleşen PnL'i income history'den al
                    pnl = 0.0
                    try:
                        since = int(pos["open_time"].timestamp() * 1000)
                        income = self.client.futures_income_history(
                            symbol=sym, incomeType="REALIZED_PNL",
                            startTime=since, limit=10,
                        )
                        pnl = sum(float(i["income"]) for i in income)
                    except Exception:
                        pass
                    self.total_pnl += pnl
                    if pnl >= 0: self.wins   += 1
                    else:        self.losses += 1
                    close_time = datetime.now()
                    self.trade_log.append(dict(
                        time=close_time, symbol=sym,
                        action="CLOSE (SL/TP@Binance)", price=0, pnl=pnl,
                    ))
                    logger.info(f"SYNC: {sym} Binance'de kapandı | PnL:{pnl:+.2f}$")
                    # Gerçek kapanışı da DB ve kill switch'e yaz
                    try:
                        comm = (pos["margin"] * pos["leverage"]) * 0.0005 * 2
                        save_trade(
                            symbol=sym, side=pos["side"],
                            entry_price=pos["entry_price"], close_price=0,
                            margin=pos["margin"], leverage=pos["leverage"],
                            gross_pnl=pnl + comm, commission=comm, net_pnl=pnl,
                            reason_open=pos.get("reason", ""),
                            reason_close="BINANCE_SL_TP",
                            open_time=pos["open_time"], close_time=close_time,
                            score=pos.get("score", 0),
                            btc_trend=pos.get("btc_trend", "SIDEWAYS"),
                            market_regime=pos.get("market_regime", "UNKNOWN"),
                            atr_pct=pos.get("atr_pct", 0.0),
                            fear_greed=pos.get("fear_greed", 50),
                        )
                    except Exception as db_err:
                        logger.debug(f"DB yazma hatası (sync): {db_err}")

            # Kapanan pozisyon varsa bakiyeyi yenile
            if binance_open != {p for p in self.positions}:
                self.sync_balance()

            return binance_open
        except BinanceAPIException as e:
            logger.error(f"Pozisyon sync hatası: {e}")
            return set()

    def check_positions(self, prices):
        closed = []
        for sym, pos in list(self.positions.items()):
            cur = prices.get(sym)
            if cur is None: continue
            hit, reason = should_close_position(pos, cur)
            if hit:
                ok, pnl = self.close_position(sym, cur, reason)
                if ok: closed.append((sym, reason, pnl))
        return closed

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
