#!/usr/bin/env python3
"""
Mutasyon taraması — testlerin GERÇEKTEN ölçtüğünü doğrular.

NEDEN VAR
Bir testin geçmesi, düzeltmenin çalıştığını kanıtlamaz. Test yanlış şeyi
ölçüyorsa düzeltmeyi geri aldığınızda da geçer. Bu denetim serisinde tam
olarak bu oldu: "koruma var" diye yazılmış testler, korumanın varlığını
değil komşu bir davranışı ölçüyordu.

NASIL ÇALIŞIR
Her mutasyon bir düzeltmeyi tek satırda geri alır ve testler çalıştırılır.
Beklenen sonuç: EN AZ BİR TEST KIRILIR. Kırılmıyorsa o düzeltme test
tarafından korunmuyor demektir — mutasyon "sağ kaldı" (SURVIVED).

KULLANIM
    .venv/bin/python scripts/mutation_check.py          # hepsi
    .venv/bin/python scripts/mutation_check.py M14      # tek mutasyon

Çıkış kodu 0 = tüm mutasyonlar yakalandı.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import queue
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ÇALIŞMA AĞACINA ASLA YAZILMAZ.
#
# DENETİM BULGUSU (2026-08-16): eski sürüm mutasyonu doğrudan depodaki dosyaya
# yazıp `finally` ile geri alıyordu. Sinyal yakalayıcıları SIGINT/SIGTERM'i
# kurtarıyordu ama SIGKILL'i kimse yakalayamaz — otomatik test koşucusu 300
# saniyede zaman aşımıyla süreci SIGKILL'lediğinde `trader.py` MUTASYONA
# UĞRAMIŞ hâlde kaldı. Aynı gün ikinci bir olayda, tarama sürerken alınan bir
# rsync mutasyona uğramış kodu sunucuya taşıdı.
#
# Çözüm yamalamak değil, tasarımı değiştirmek: mutasyon yalnız geçici bir
# kopyaya uygulanır. Süreç nasıl ölürse ölsün depo bozulmaz ve tarama, başka
# işlerle (deploy, rsync, commit) aynı anda güvenle çalışabilir.

ROOT = Path(__file__).resolve().parent.parent
_PYTEST_BASE = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-x"]
PYTEST = _PYTEST_BASE + ["tests/"]          # geriye dönük uyumluluk

# (kimlik, dosya, açıklama, aranan, yerine konan)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("M1", "trader.py", "Kısmi dolan emir karantinaya alınmaz",
     'TERMINAL_STATUS = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")',
     'TERMINAL_STATUS = ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "PARTIALLY_FILLED", "NEW")'),

    ("M2a", "trader.py", "Eşleştirmede yön kontrolü yok",
     'if rec.get("side") != side:\n            return False',
     'if False:\n            return False'),

    ("M2b", "trader.py", "Eşleştirmede reduceOnly kontrolü yok",
     'if bool(rec.get("reduce_only")) != bool(reduce_only):\n            return False',
     'if False:\n            return False'),

    ("M2c", "trader.py", "Eşleştirmede miktar kontrolü yok",
     'return abs(want - q) <= max(step, want * 0.01)',
     'return True'),

    ("M2d", "trader.py", "Miktarsız kayıt sahiplenilebilir",
     'if want <= 0:\n            return False                       # bağlamsız eski kayıt',
     'if False:\n            return False'),

    ("M2e", "trader.py", "Eşleştirmede purpose kontrolü yok",
     'if (rec.get("purpose") or "") != (purpose or ""):\n            return False',
     'if False:\n            return False'),

    ("M3", "trader.py", "Borsa-stop ortak PnL doğrulayıcısını atlar",
     'measured_net, pnl_verified = self._exchange_net_pnl(sym, since)',
     'measured_net, pnl_verified = 0.0, True'),

    ("M4", "trader.py", "Equity crossUnPnl yedek yoluna düşer",
     'self.equity_stale = True\n            logger.error(f"Equity ölçülemedi — yeni giriş durduruldu: {e}")',
     'logger.error(f"Equity: {e}")\n            for bb in self.client.futures_account_balance():\n'
     '                if bb.get("asset") == "USDT":\n'
     '                    return float(bb["balance"]) + float(bb.get("crossUnPnl", 0) or 0)'),

    ("M5", "trader.py", "Karantina yazma hatası yutulur",
     'if not ok:\n            self.pending_state_ok = False',
     'if False:\n            self.pending_state_ok = False'),

    ("M6", "trader.py", "Karantina kaydı yalnız clientOrderId",
     'self.pending_unknown[symbol] = rec\n                self._save_pending()\n'
     '                return OrderResult(0.0, 0.0, None, "UNKNOWN")',
     'self.pending_unknown[symbol] = coid\n                self._save_pending()\n'
     '                return OrderResult(0.0, 0.0, None, "UNKNOWN")'),

    ("M7", "trader.py", "Uzlaştırma raporlanmaz",
     'self.reconcile_needed[symbol] = note',
     '_ignored = note'),

    ("M8", "database.py", "state_get hatayı yutar (strict etkisiz)",
     'if strict:\n            raise\n        return default',
     'return default'),

    ("M9", "trader.py", "Terminal olmayan emirde karantina kalkar",
     'if st not in self.TERMINAL_STATUS:\n            logger.error(f"{symbol}: bekleyen emir hâlâ {st} — yeni emir yok")',
     'if False:\n            logger.error(f"{symbol}: bekleyen emir hâlâ {st} — yeni emir yok")'),

    ("M10", "trader.py", "Equity ölçülemezken giriş serbest",
     'if self.equity_stale:\n            return False, "Equity ölçülemiyor, yeni pozisyon açılmıyor"',
     'if False:\n            return False, "Equity ölçülemiyor, yeni pozisyon açılmıyor"'),

    ("M11", "trader.py", "Karantina durumu bilinmezken giriş serbest",
     'if not self.pending_state_ok:\n            return False, "Karantina durumu okunamıyor, yeni pozisyon açılmıyor"',
     'if False:\n            return False, "Karantina durumu okunamıyor, yeni pozisyon açılmıyor"'),

    ("M12", "trader.py", "Doğrulanmamış PnL'de iyimser değer kullanılır",
     'total_net = min(measured_net, est_total)',
     'total_net = measured_net'),

    ("M13", "trader.py", "ORDER_ABSENT karantinaya alınır",
     'return o, filled, avg, True\n            # None: sorgulayamadık',
     'return o, filled, avg, False\n            # None: sorgulayamadık'),

    ("M14", "trader.py", "OrderResult.ok yalnız filled>0 bakar",
     'return self.filled > 0 and self.status in ("FILLED", "PARTIAL")',
     'return self.filled > 0'),

    ("M15", "trader.py", "Emir oturması beklenmez (tek sorgu)",
     'for i in range(self.SETTLE_ATTEMPTS):',
     'for i in range(1):'),

    ("M16", "trader.py", "Oturmayan emir terminal sayılır",
     'return o, filled, avg, False',
     'return o, filled, avg, True'),

    ("M17", "trader.py", "PnL uyarısı kalıcı işlem duruşuna çevrilir",
     'self.pnl_unverified[sym] = "kapanış PnL\'i eksik bileşenli (muhafazakâr değer)"',
     'self.reconcile_needed[sym] = "kapanış PnL\'i eksik bileşenli (muhafazakâr değer)"'),

    ("M18", "database.py", "state_set_many başarısızlığı gizler",
     'logger.error(f"state_set_many hatası: {e}")\n        return False',
     'logger.error(f"state_set_many hatası: {e}")\n        return True'),

    ("M19", "trader.py", "Uzlaştırma diske yazılmadan karantina silinir",
     'if self._save_reconcile():\n                self.pending_unknown.pop(symbol, None)\n'
     '                self._save_pending()\n            else:',
     'if True:\n                self.pending_unknown.pop(symbol, None)\n'
     '                self._save_pending()\n            else:'),

    ("M20", "trader.py", "Uzlaştırma restart'ta yüklenmez",
     'self.reconcile_needed, self.reconcile_state_ok = _load_reconcile()',
     'self.reconcile_needed, self.reconcile_state_ok = {}, True'),

    ("M23", "trader.py", "Uzlaştırma okuma hatası güvenilir sayılır",
     'logger.error(f"UZLAŞTIRMA LİSTESİ OKUNAMADI: {e}")\n        return {}, False',
     'logger.error(f"UZLAŞTIRMA LİSTESİ OKUNAMADI: {e}")\n        return {}, True'),

    ("M21", "trader.py", "Doğrulanmamış PnL DB'ye doğrulanmış yazılır",
     'pnl_verified=pnl_verified,\n            )',
     'pnl_verified=True,\n            )'),

    ("M22", "go_live_check.py", "Doğrulanmamış işlemler örnekleme katılır",
     'trades = [t for t in all_rows if _verified(t)]',
     'trades = list(all_rows)'),

    # ── 9. tur ──
    ("M24", "trader.py", "Tek bayrak: karantina yazması uzlaştırma kilidini açar",
     'if not self.reconcile_state_ok:\n            return False, "Uzlaştırma durumu okunamıyor, yeni pozisyon açılmıyor"',
     'if False:\n            return False, "Uzlaştırma durumu okunamıyor, yeni pozisyon açılmıyor"'),

    ("M25", "trader.py", "Uzlaştırma yazma hatası karantina bayrağına yazılır",
     'self.reconcile_state_ok = False\n        logger.error(f"UZLAŞTIRMA KAYDI YAZILAMADI {self.reconcile_needed}")',
     'self.pending_state_ok = False\n        logger.error(f"UZLAŞTIRMA KAYDI YAZILAMADI {self.reconcile_needed}")'),

    ("M26", "trader.py", "Normal kapanış her hâlükârda doğrulanmış sayılır",
     'pnl_verified=bool(verified and ex_net is not None))\n            self._persist_positions()',
     'pnl_verified=True)\n            self._persist_positions()'),

    ("M27", "trader.py", "Acil kapanış her hâlükârda doğrulanmış sayılır",
     'net + total_comm, total_comm, net,\n                               pnl_verified=bool(verified and ex_net is not None))',
     'net + total_comm, total_comm, net,\n                               pnl_verified=True)'),

    ("M28", "trader.py", "Boş gelir geçmişi 'doğrulanmış sıfır' sayılır",
     'return 0.0, (not required)',
     'return 0.0, True'),

    ("M29", "trader.py", "Gelir sorgusu hatası doğrulamayı düşürmez",
     'rows = None\n                logger.error(f"{symbol}: {kind} okunamadı: {e}")',
     'rows = [{"income": "0"}]\n                logger.error(f"{symbol}: {kind} okunamadı: {e}")'),

    ("M30", "database.py", "Eski işlemler doğrulanmış varsayılır",
     '("pnl_verified",   "INTEGER", "0"),',
     '("pnl_verified",   "INTEGER", "1"),'),

    # ── 10. tur ──
    ("M31", "trader.py", "Gelir penceresi emirden SONRA başlar",
     'v = pos.get("income_start_ms")\n        if v:\n            return int(v)',
     'v = None\n        if v:\n            return int(v)'),

    ("M32", "trader.py", "Gelir penceresi pozisyona hiç yazılmaz",
     'pos["income_start_ms"] = income_start_ms',
     'pos["_unused_income_start_ms"] = income_start_ms'),

    ("M33", "trader.py", "Boş komisyon doğrulanmış sayılır",
     'comm, ok_c = self._income_sum(symbol, "COMMISSION", since_ms, required=True)',
     'comm, ok_c = self._income_sum(symbol, "COMMISSION", since_ms, required=False)'),

    ("M34", "trader.py", "Eksik komisyon tekrar sorgulanmaz",
     'if not required:\n                break                       # boş olması normal, bekleme',
     'if True:\n                break                       # boş olması normal, bekleme'),

    ("M35", "trader.py", "Boş funding doğrulamayı düşürür (aşırı katı)",
     'fund, ok_f = self._income_sum(symbol, "FUNDING_FEE", since_ms, required=False)',
     'fund, ok_f = self._income_sum(symbol, "FUNDING_FEE", since_ms, required=True)'),

    ("M36", "trader.py", "Dolu karantina yeni girişi durdurmaz",
     'if self.pending_unknown:\n            return False, (f"Akıbeti bilinmeyen emir var',
     'if False:\n            return False, (f"Akıbeti bilinmeyen emir var'),

    ("M37", "trader.py", "Dolu uzlaştırma listesi yeni girişi durdurmaz",
     'if self.reconcile_needed:\n            return False, (f"Uzlaştırma bekleyen sembol var',
     'if False:\n            return False, (f"Uzlaştırma bekleyen sembol var'),

    ("M38", "trader.py", "Korumasız pozisyon yeni girişi durdurmaz",
     'if self.unprotected:\n            return False, (f"Korumasız pozisyon var',
     'if False:\n            return False, (f"Korumasız pozisyon var'),

    ("M39", "trader.py", "Başarılı uzlaştırma yazması kilidi açmaz",
     'self.reconcile_state_ok = True\n                return True',
     'return True'),

    ("M40", "database.py", "Doğrulanmamış kâr risk hesabına girer",
     '"WHERE COALESCE(pnl_verified, 0) = 1 OR net_pnl < 0 "',
     '"WHERE 1 = 1 "'),

    ("M41", "database.py", "Doğrulanmamış işlem günlük raporda işaretlenmez",
     '(row["unverified"] or 0) + (0 if verified else 1),',
     '(row["unverified"] or 0),'),

    # ── 11. tur ──
    ("M42", "trader.py", "Pozisyon senkronu bilinmezken giriş serbest",
     'if not self.position_sync_ok:\n            return False, "Borsa pozisyon durumu doğrulanamıyor, yeni pozisyon açılmıyor"',
     'if False:\n            return False, "Borsa pozisyon durumu doğrulanamıyor, yeni pozisyon açılmıyor"'),

    ("M43", "trader.py", "Pozisyon API hatası senkron kilidini düşürmez",
     'self.position_sync_ok = False\n            for sym in self.positions:',
     'self.position_sync_ok = True\n            for sym in self.positions:'),

    ("M44", "trader.py", "Başarılı senkron giriş kilidini açmaz",
     'self.position_sync_ok = True\n            return binance_open',
     'return binance_open'),

    ("M45", "trader.py", "Stop doğrulama belirsizliği girişe engel olmaz",
     'if self.protection_unknown:\n            return False, (f"Stop koruması doğrulanamayan pozisyon var "',
     'if False:\n            return False, (f"Stop koruması doğrulanamayan pozisyon var "'),

    ("M46", "trader.py", "Okunamayan stop belirsizlik listesine yazılmaz",
     'self.protection_unknown[sym] = "koruyucu stop durumu okunamıyor"',
     '_ignored_protection_unknown = sym'),

    ("M47", "trader.py", "Giriş sonrası acil kapanış muhasebe yolunu atlar",
     'self._mark_unprotected(pos, "giriş sonrası koruyucu stop kurulamadı")',
     'self.positions.pop(symbol, None)'),

    ("M48", "trader.py", "Borsa-stop kısmi PnL'i ikinci kez sayar",
     'self.total_pnl += total_net - pos.get("realized_pnl", 0.0)',
     'self.total_pnl += total_net'),

    ("M49", "database.py", "Profit factor yalnız son zararı kullanır",
     'gl  = (row["gross_loss"] or 0) + (abs(gross_pnl) if gross_pnl < 0 else 0)',
     'gl  = abs(gross_pnl) if gross_pnl < 0 else 0'),

    ("M50", "trader.py", "Çözülmüş orphan kalıcı giriş kilidi bırakır",
     'for sym in list(self.orphans):\n            if sym not in live:\n                self.orphans.pop(sym, None)',
     'for sym in ():\n            if sym not in live:\n                self.orphans.pop(sym, None)'),

    ("M51", "trader.py", "Bakiye senkronu genel timeout'u dışarı taşır",
     'except Exception as e:\n            logger.error(f"Futures bakiye hatası: {e}")',
     'except BinanceAPIException as e:\n            logger.error(f"Futures bakiye hatası: {e}")'),

    ("M52", "trader.py", "Restart doğrulaması genel timeout'u dışarı taşır",
     'except Exception as e:\n            # Borsayı okuyamıyorsak hiçbir şeyi doğrulanmış sayamayız.',
     'except BinanceAPIException as e:\n            # Borsayı okuyamıyorsak hiçbir şeyi doğrulanmış sayamayız.'),

    # ── 2026-08-16: sessiz kilitler ──────────────────────────────────────────
    ("M53", "risk_manager.py", "Haftalık limit ölçüm yerine bozuk sayaca bakar",
     'wpnl = self.effective_weekly_pnl()',
     'wpnl = self.weekly_pnl'),

    ("M54", "risk_manager.py", "Haftalık eşik hafta başı yerine gün başı bakiyeye bakar",
     'wref = self.week_start_balance if self.week_start_balance > 0 else ref',
     'wref = ref'),

    ("M55", "risk_manager.py", "Ölçüm kaynağı yok sayılır (sayaç geri döner)",
     'if self._weekly_source is not None:',
     'if False:'),

    ("M56", "risk_manager.py", "Kaynak patlayınca haftalık limit sessizce devre dışı kalır",
     'except Exception:\n                pass  # kaynak okunamadıysa sessizce sayaca düş\n        return self.weekly_pnl',
     'except Exception:\n                pass  # kaynak okunamadıysa sessizce sayaca düş\n        return 0.0'),

    ("M57", "risk_manager.py", "Rapor yolu durum değiştirir (durdurmayı sessizce kaldırır)",
     'return self._evaluate(balance, strategy, mutate=False)',
     'return self._evaluate(balance, strategy, mutate=True)'),

    ("M58", "risk_manager.py", "Drawdown kapısı yine kalıcı ölüm olur",
     'if elapsed_h >= DRAWDOWN_COOLDOWN_HOURS and auto_ok:',
     'if False:'),

    ("M59", "risk_manager.py", "Drawdown beklemesi hiç başlatılmaz (soğuma asla dolmaz)",
     'if mutate:\n                    self._dd_halted_at = datetime.now()',
     'if False:\n                    self._dd_halted_at = datetime.now()'),

    ("M60", "risk_manager.py", "Gerçek modda drawdown otomatik kurtarılır",
     'auto_ok = PAPER_TRADING and DRAWDOWN_AUTO_REBASE_PAPER',
     'auto_ok = True'),

    ("M61", "database.py", "Haftalık PnL penceresi hafta yerine tüm geçmişi sayar",
     'AND close_time >= ? AND close_time < ?")',
     'AND close_time >= ? AND close_time < ? OR 1=1")'),

    # 2026-08-18: stop kararının dayandığı fiyatın YAŞI. Ölçüm: INITIAL_STOP ile
    # kapanan 32 işlemin 32'si de planlanan -1R'yi aştı, fazla zarar 119.50$.
    ("M62", "market_data.py", "Stop kontrolü yine 90 saniyelik bayat fiyatı kabul eder",
     'self, symbols: list[str], max_age_seconds: float = MAX_PRICE_AGE_SEC',
     'self, symbols: list[str], max_age_seconds: float = 90'),

    ("M63", "market_data.py", "REST veremeyince bayat fiyat yine sonuca sızar",
     '''            price = self._fetch_price_rest(sym)
            if price is None:
                continue''',
     '''            price = self._fetch_price_rest(sym)
            if price is None:
                fresh[sym] = self._prices.get(sym, 0.0)
                continue'''),

    ("M64", "config.py", "Stop kontrolü yine sinyal taramasının hızına bağlanır",
     'POSITION_CHECK_INTERVAL  = 3',
     'POSITION_CHECK_INTERVAL  = 30'),

    ("M65", "main.py", "Bayat fiyatlı sembol yine stop motoruna geçer",
     'if sym not in fresh:',
     'if False:'),

    # 2026-08-18: kâr tarafı. TP1 (+1R) ile TP2 (+2R) arası "ölü bölge"ydi;
    # stop başabaşta donuyor, +1.9R'lik hareketler +0.3R'ye dönüyordu.
    ("M66", "position.py", "TP1 sonrası trailing yine açılmaz (ölü bölge geri gelir)",
     '''        if TRAIL_AFTER_TP:
            pos["trailing_active"] = True
    elif reason == EXIT_TP2:''',
     '''        if False:
            pos["trailing_active"] = True
    elif reason == EXIT_TP2:'''),

    ("M67", "position.py", "Trailing stop'u geriye de taşıyabilir (koruma gevşer)",
     '''    if pos["side"] == "BUY":
        if new_stop > pos["stop_loss"]:''',
     '''    if pos["side"] == "BUY":
        if True:'''),

    # 2026-08-18: bakiye sıfırlaması, sıfırlamadan ÖNCEKİ zararı yeni dönemin
    # haftalık risk bütçesine yazarsa bot temiz başlangıçta kendini durdurur.
    ("M68", "database.py", "Sıfırlama öncesi zarar yine yeni döneme yazılır",
     'if baslangic and str(baslangic) > alt_sinir:',
     'if False:'),

    # 2026-08-18: çoklu strateji deneyi. Buradaki hatalar sessizdir — kapı
    # gevşerse "kazanan" ilan edilir ve gerçek para en şanslıya gider.
    ("M69", "strategy_lab.py", "Seçim eşiği yine beklenen-max (kurayı kenar sanır)",
     '    return NormalDist().inv_cdf(p)',
     '    return expected_max_of_n(n)'),

    ("M70", "strategy_lab.py", "Etkin örneklem yerine ham işlem sayısı kullanılır",
     '        sc.std_error = sc.stdev_r / math.sqrt(sc.effective_n)',
     '        sc.std_error = sc.stdev_r / math.sqrt(sc.trades)'),

    ("M71", "strategy_lab.py", "Çakışan işlemler ayrı gözlem sayılır",
     '        if z - son > pencere:\n            kume += 1',
     '        if True:\n            kume += 1'),

    ("M72", "strategy_lab.py", "Kâğıt modda risk ihlali botu durdurur (veri kesilir)",
     "    return RISK_ACTION_RECORD_ONLY if mode == _MODE_PAPER else RISK_ACTION_HALT",
     "    return RISK_ACTION_HALT"),

    ("M73", "main.py", "Döngü hatasında stop kontrolü çalışmaz",
     '''                try:
                    _watch_positions()
                except Exception:
                    logger.exception("Stop kontrolü de başarısız")''',
     '                pass'),

    ("M74", "main.py", "Döngü hatası yeniden fırlatılır (bot ölür)",
     '                time.sleep(min(2.0 * _cycle_errors, float(SIGNAL_INTERVAL)))',
     '                time.sleep(min(2.0 * _cycle_errors, float(SIGNAL_INTERVAL)))\n                raise'),

    # 2026-08-18: strateji başına portföy izolasyonu. Buradaki hatalar SESSİZ
    # veri kaybıdır: pozisyon diskten yok olur, kimse fark etmez.
    ("M75", "database.py", "Kayıt yine TÜM stratejilerin pozisyonlarını siler",
     'con.execute("DELETE FROM open_positions WHERE strategy = ?", (strategy,))',
     'con.execute("DELETE FROM open_positions")'),

    ("M76", "database.py", "Yükleme strateji süzmez (portföyler karışır)",
     '''            rows = con.execute(
                "SELECT symbol, payload FROM open_positions WHERE strategy = ?",
                (strategy,),
            ).fetchall()''',
     '''            rows = con.execute(
                "SELECT symbol, payload FROM open_positions"
            ).fetchall()'''),

    ("M77", "portfolios.py", "Bir portföyün kayıt hatası diğerlerini de durdurur",
     '''            try:
                kitap.save()
            except Exception:
                logger.exception(f"{kitap.name}: portföy kaydedilemedi")''',
     '            kitap.save()'),

    ("M78", "portfolios.py", "TREND state anahtarı da ön eklenir (bakiye sıfırlanır)",
     '    return field if strategy == _LEGACY_STRATEGY else f"{strategy}:{field}"',
     '    return f"{strategy}:{field}"'),

    ("M79", "portfolios.py", "Equity kilitli marjı saymaz (pozisyonlu strateji fakir görünür)",
     '''        return self.portfolio.balance + sum(
            float(pos.get("margin", 0.0)) for pos in self.portfolio.positions.values()
        )''',
     '        return self.portfolio.balance'),

    # 2026-08-18: MOMENTUM gölgeden çıkıp kendi portföyüne bağlandı.
    # Buradaki hatalar sessizdir: portföyler karışır, sayı yanlış çıkar.
    ("M80", "main.py", "MOMENTUM yine TREND'in kasasından harcar",
     '                    ok, msg = pf.open_position(',
     '                    ok, msg = trader.open_position('),

    ("M81", "main.py", "Pozisyon limiti ortak sayılır (MOMENTUM işlem açamaz)",
     '                    positions = getattr(pf, "positions", {})',
     '                    positions = getattr(trader, "positions", {})'),

    ("M82", "main.py", "Risk tahsisi anahtarı strateji taşımaz (tahsis ezilir)",
     '                        _alloc_of[f"{strategy}:{sig.symbol}"] = alloc_r',
     '                        _alloc_of[sig.symbol] = alloc_r'),

    ("M83", "main.py", "Bir defterin hatası diğerini de yönetimsiz bırakır",
     '''                logger.exception(f"{ad}: check_positions başarısız")
                continue''',
     '''                logger.exception(f"{ad}: check_positions başarısız")
                return'''),

    ("M84", "config.py", "MOMENTUM sermayesi TREND'den farklı (kıyas bozulur)",
     'MOMENTUM_BALANCE       = 1000.0',
     'MOMENTUM_BALANCE       = 250.0'),

    ("M85", "momentum.py", "Sinyal strateji adini tasimaz (sessizce TREND kasasindan harcar)",
     'STRATEGY_NAME = "MOMENTUM_FAST"',
     'STRATEGY_NAME = "TREND"'),

    # 2026-08-18: BREAKOUT / PULLBACK / MEANREV deney stratejileri.
    ("M86", "strategies.py", "MEANREV trend icinde de calisir (klasik olum bicimi)",
     '''    trend_gucu = abs(ema_m - ema_s) / max(atr, 1e-12)
    if trend_gucu > 1.0:''',
     '''    trend_gucu = abs(ema_m - ema_s) / max(atr, 1e-12)
    if False:'''),

    ("M87", "strategies.py", "BREAKOUT kanala SON bari da katar (her bar kirilim gorunur)",
     '    onceki = df.iloc[:-1]',
     '    onceki = df'),

    ("M88", "strategies.py", "BREAKOUT hacim teyidi aranmaz (gurultuyu kirilim sanar)",
     '    if hacim_x < VOLUME_CONFIRM_X:',
     '    if False:'),

    ("M89", "strategies.py", "Bir stratejinin hatasi digerlerinin akisini keser",
     '''            except Exception as exc:  # noqa: BLE001 — bir strateji diğerini durduramaz
                out.append(_red(sym, price, ad, f"hata: {type(exc).__name__}"))''',
     '''            except Exception as exc:
                raise'''),

    ("M90", "main.py", "Deney defterleri pozisyon izlemeye dahil edilmez (stoplar kontrolsuz)",
     '        defterler.extend((ad, k.portfolio) for ad, k in lab_books.items())',
     '        pass'),

    ("M91", "main.py", "Deney stratejileri yine TREND defterine acar",
     '''        if strategy in lab_books:
            return lab_books[strategy].portfolio''',
     '        pass'),

    ("M92", "config.py", "Bar sayisi stratejilerin ihtiyacindan az (sessiz olum)",
     'CANDLE_LIMIT = 250',
     'CANDLE_LIMIT = 120'),

    ("M93", "main.py", "Kill switch kagit modda da tum stratejileri durdurur",
     'if not can_trade and PAPER_TRADING:',
     'if False:'),

    ("M94", "main.py", "Risk butcesi kagit modda da baglar (deney bulasir)",
     'if PAPER_TRADING and (not ok_budget or _butce_yetersiz):',
     'if False:'),

    # 2026-08-19: acik pozisyonlara adanmis fiyat akisi. Stop asimi -1.28R'de
    # takili kalmisti; kalan bosluk 20 saniyelik tazelik penceresiydi.
    ("M95", "market_data.py", "Kapanan pozisyonun akisi kapatilmaz (soket sizintisi)",
     '''            for sym in mevcut - istenen:
                anahtar = self._prio_streams.pop(sym, None)''',
     '''            for sym in []:
                anahtar = self._prio_streams.pop(sym, None)'''),

    ("M96", "market_data.py", "WS yeniden baslayinca abonelikler temizlenmez (adanmis akis olur)",
     '''        with self._prio_lock:
            self._prio_streams.clear()''',
     '        pass'),

    ("M97", "main.py", "Adanmis akis hic guncellenmez (toplu akisa geri duser)",
     '            mdm.set_priority_symbols(semboller)',
     '            pass'),
]


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l]


def _make_snapshot(dest: Path) -> None:
    """Depoyu izole bir dizine kopyala (yalnız git'in bildiği dosyalar)."""
    for rel in _tracked_files():
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


# Hangi kaynak dosyayı hangi testler ölçüyor (import ilişkisinden çıkarıldı).
# Yalnız HIZLANDIRMA amaçlıdır; doğruluğu etkilemez, çünkü hedefli koşu
# mutasyonu yakalayamazsa TAM suite ile teyit edilir (bkz. _run_one).
_TARGETED_TESTS = {
    "risk_manager.py":  ["tests/test_risk.py", "tests/test_stop_safety.py",
                         "tests/test_persistence.py"],
    "database.py":      ["tests/test_weekly_pnl.py", "tests/test_persistence.py",
                         "tests/test_momentum.py", "tests/test_parity.py"],
    "trader.py":        ["tests/test_stop_safety.py", "tests/test_parity.py",
                         "tests/test_persistence.py"],
    "go_live_check.py": ["tests/test_stop_safety.py"],
    "market_data.py":   ["tests/test_ws_ingest.py"],
}


def _pytest_cmd(paths: list[str] | None) -> list[str]:
    return _PYTEST_BASE + (paths if paths else ["tests/"])


def _run_one(work: Path, mid: str, fname: str, desc: str, old: str, new: str):
    """Tek mutasyonu KOPYADA dener. Gerçek çalışma ağacına dokunmaz.

    İki aşama:
      1. Yalnız o dosyayı ölçen testler koşar (saniyeler).
      2. Orada yakalanmadıysa TAM suite koşar. "Sağ kaldı" kararı ASLA
         hedefli koşuya dayanmaz — eksik bir eşleme sahte alarm üretirdi.
    """
    path = work / fname
    original = path.read_text()
    if original.count(old) != 1:
        return ("missing", mid, desc, original.count(old))

    env = dict(os.environ)
    env.pop("CRYPTO_BOT_DB", None)          # conftest süreç-özel yol üretsin
    try:
        path.write_text(original.replace(old, new))

        targeted = _TARGETED_TESTS.get(fname)
        if targeted:
            r = subprocess.run(_pytest_cmd(targeted), cwd=work,
                               capture_output=True, text=True, env=env)
            if r.returncode != 0:
                return ("caught", mid, desc, None)

        r = subprocess.run(_pytest_cmd(None), cwd=work,
                           capture_output=True, text=True, env=env)
    finally:
        path.write_text(original)           # kopyayı sonraki iş için temizle
    return ("caught" if r.returncode != 0 else "survived", mid, desc, None)


def run() -> int:
    wanted = sys.argv[1:] or None
    jobs = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    if not jobs:
        print("Eşleşen mutasyon yok.")
        return 1

    # SIGKILL'lenen bir koşu kendi geçici dizinini silemez (kimse yakalayamaz).
    # Depoya zarar vermez ama zamanla disk sızdırır; başlangıçta süpürülür.
    for stale in Path(tempfile.gettempdir()).glob("mutcheck-*"):
        shutil.rmtree(stale, ignore_errors=True)

    workers = max(1, min(8, (os.cpu_count() or 2) - 1, len(jobs)))
    tmp = Path(tempfile.mkdtemp(prefix="mutcheck-"))
    caught, survived, missing = [], [], []

    try:
        # Her işçi kendi kopyasında çalışır. Böylece (a) mutasyon asla gerçek
        # depoya yazılmaz, (b) işçiler birbirinin dosyasını bozmaz.
        pool = [tmp / f"w{i}" for i in range(workers)]
        for w in pool:
            _make_snapshot(w)
        print(f"  {len(jobs)} mutasyon, {workers} işçi, izole kopyalarda çalışıyor...\n",
              flush=True)

        free: "queue.Queue[Path]" = queue.Queue()
        for w in pool:
            free.put(w)

        def task(job):
            w = free.get()
            try:
                return _run_one(w, *job)
            finally:
                free.put(w)

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for kind, mid, desc, extra in ex.map(task, jobs):   # sıra korunur
                if kind == "caught":
                    caught.append(mid)
                    print(f"  ✅  {mid:5} yakalandı        — {desc}", flush=True)
                elif kind == "survived":
                    survived.append((mid, desc))
                    print(f"  ❌  {mid:5} SAĞ KALDI        — {desc}", flush=True)
                else:
                    missing.append((mid, desc, extra))
                    print(f"  ??  {mid:5} HEDEF BULUNAMADI ({extra} eşleşme) — {desc}",
                          flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(caught) + len(survived) + len(missing)
    print(f"\n  {len(caught)}/{total} mutasyon yakalandı")
    for mid, desc in survived:
        print(f"    SAĞ KALAN: {mid} — {desc}  (bu düzeltme test tarafından korunmuyor)")
    for mid, desc, n in missing:
        print(f"    HEDEFSİZ:  {mid} — {desc}  ({n} eşleşme; kod değişmiş olabilir)")
    return 0 if not survived and not missing else 1


if __name__ == "__main__":
    sys.exit(run())
