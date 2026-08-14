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

import atexit
import signal
import subprocess
import sys
from pathlib import Path

# Script yarıda kesilirse (SIGINT/SIGTERM) mutasyon dosyada KALMAMALI.
# Bu koruma olmadan bir kill, kaynak dosyayı bozuk hâlde bırakır.
_ORIGINALS: dict[Path, str] = {}


def _restore_all(*_):
    for path, text in list(_ORIGINALS.items()):
        try:
            path.write_text(text)
        except Exception:
            pass
    _ORIGINALS.clear()


atexit.register(_restore_all)
for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, lambda *a: (_restore_all(), sys.exit(130)))

ROOT = Path(__file__).resolve().parent.parent
PYTEST = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "tests/", "-q", "-x"]

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
]


def run() -> int:
    wanted = sys.argv[1:] or None
    caught, survived, missing = [], [], []

    for mid, fname, desc, old, new in MUTATIONS:
        if wanted and mid not in wanted:
            continue
        path = ROOT / fname
        original = path.read_text()
        if original.count(old) != 1:
            missing.append((mid, desc, original.count(old)))
            print(f"  ??  {mid:5} HEDEF BULUNAMADI ({original.count(old)} eşleşme) — {desc}")
            continue
        _ORIGINALS[path] = original
        try:
            path.write_text(original.replace(old, new))
            r = subprocess.run(PYTEST, cwd=ROOT, capture_output=True, text=True)
        finally:
            path.write_text(original)          # her hâlükârda geri al
            _ORIGINALS.pop(path, None)

        if r.returncode != 0:
            caught.append(mid)
            print(f"  ✅  {mid:5} yakalandı        — {desc}")
        else:
            survived.append((mid, desc))
            print(f"  ❌  {mid:5} SAĞ KALDI        — {desc}")

    total = len(caught) + len(survived) + len(missing)
    print(f"\n  {len(caught)}/{total} mutasyon yakalandı")
    for mid, desc in survived:
        print(f"    SAĞ KALAN: {mid} — {desc}  (bu düzeltme test tarafından korunmuyor)")
    for mid, desc, n in missing:
        print(f"    HEDEFSİZ:  {mid} — {desc}  ({n} eşleşme; kod değişmiş olabilir)")
    return 0 if not survived and not missing else 1


if __name__ == "__main__":
    sys.exit(run())
