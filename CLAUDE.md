# CLAUDE.md

## ルール

### sys.exit() 禁止
- `sys.exit()` を絶対に使わないこと
- 代わりに `raise RuntimeError(...)` を使う
- 理由: `cmd_run` のフェイルセーフ (`except Exception`) が `sys.exit()` (`SystemExit`) を捕捉できず、充電器をONにする安全処理が実行されないため
- lint: `./lint.sh` で sys.exit の使用を検出できる

## 開発

- Python スクリプト。仮想環境やビルドツールは不使用
- `./lint.sh` で sys.exit() の使用チェック
