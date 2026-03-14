#!/bin/bash
# sys.exit() の使用を検出するlintスクリプト
# フェイルセーフが正しく動作するために sys.exit は禁止。raise RuntimeError を使うこと。

found=0
for f in *.py; do
    if grep -nP '\bsys\.exit\b' "$f" 2>/dev/null; then
        echo "  ^^^ $f: sys.exit() を検出。raise RuntimeError(...) を使ってください。"
        found=1
    fi
done

if [ "$found" -eq 1 ]; then
    exit 1
else
    echo "OK: sys.exit() の使用はありません。"
fi
