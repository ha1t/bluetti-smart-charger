# CLAUDE.md

## ルール

### sys.exit() 禁止
- `sys.exit()` を絶対に使わないこと
- 代わりに `raise RuntimeError(...)` を使う
- 理由: `cmd_run` のフェイルセーフ (`except Exception`) が `sys.exit()` (`SystemExit`) を捕捉できず、充電器をONにする安全処理が実行されないため

## 開発

- Python スクリプト。仮想環境やビルドツールは不使用

## テスト

```bash
.venv/bin/python -m unittest test_charge_controller -v
```

対象: `calculate_slots_needed`, `get_cheapest_slots`, `decide_charge` の純粋関数
