#!/bin/bash
BOT_DIR="/Users/kadirugur/ClaudeWork/crypto-bot"
clear
echo "=== CRYPTO BOT PANELİ ==="
echo ""
if pgrep -f "caffeinate -i -s python3 main.py" > /dev/null 2>&1; then
    echo "  Durum   : CALISIYOR"
else
    echo "  Durum   : DURMUS"
fi
TOTAL=$(grep -c "KAPANDI" "$BOT_DIR/bot.log" 2>/dev/null || echo 0)
WIN=$(grep "KAPANDI" "$BOT_DIR/bot.log" 2>/dev/null | grep -c "TAKE_PROFIT" || echo 0)
LOSS=$(grep "KAPANDI" "$BOT_DIR/bot.log" 2>/dev/null | grep -c "STOP_LOSS" || echo 0)
TOTAL_PNL=$(grep "KAPANDI" "$BOT_DIR/bot.log" 2>/dev/null | grep -oE 'PnL:[+-][0-9]+\.[0-9]+' | awk -F: '{sum+=$2} END {printf "%.2f", sum}')
echo "  Islemler : $TOTAL toplam | Karli: $WIN | Zararli: $LOSS"
echo "  Toplam PnL: $TOTAL_PNL (paper)"
echo ""
echo "  Son islemler:"
grep -E "ACILDI|KAPANDI" "$BOT_DIR/bot.log" 2>/dev/null | tail -8 | sed 's/^.*\[main\] INFO: /  /'
echo ""
echo "  [B] Baslat  [D] Durdur  [L] Canli Log  [Q] Cikis"
read -p "  Secim: " c
case $c in
    b|B) cd "$BOT_DIR"; pkill -f "caffeinate -i -s python3 main.py" 2>/dev/null; rm -f crypto-bot.lock; nohup caffeinate -i -s python3 main.py >> paper_run.log 2>&1 & echo "  Bot baslatildi (PID: $!)" ;;
    d|D) pkill -f "caffeinate -i -s python3 main.py" 2>/dev/null; rm -f "$BOT_DIR/crypto-bot.lock"; echo "  Bot durduruldu" ;;
    l|L) tail -f "$BOT_DIR/bot.log" ;;
    q|Q) exit 0 ;;
esac
