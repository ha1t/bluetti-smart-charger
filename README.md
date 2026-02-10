# bluetti-battery

BLUETTI ポータブル電源のバッテリー残量をコマンドラインから取得するツール。

BLUETTI 公式 Home Assistant 連携が使用しているクラウド API を直接利用しています。Home Assistant のインストールは不要です。

## 対応デバイス

BLUETTI アプリに登録済みの WiFi 接続デバイス全般。動作確認済み:

- AORA 100 V2

## セットアップ

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 初回認証

```bash
.venv/bin/python bluetti_battery.py setup
```

1. 表示される URL をブラウザで開く
2. BLUETTI アカウントでログイン
3. リダイレクト先の URL（ページは読み込めない）をアドレスバーからコピー
4. ターミナルにペースト

認証トークンは `.env` に保存されます。トークンは自動リフレッシュされるため、再認証は通常不要です。

## 使い方

```bash
# バッテリー残量を表示
.venv/bin/python bluetti_battery.py status

# JSON 形式で出力
.venv/bin/python bluetti_battery.py status --json

# デバイス一覧
.venv/bin/python bluetti_battery.py devices
```

### 出力例

```
$ .venv/bin/python bluetti_battery.py status
  AORA100V2 (AORA100V2-AORA100V2)  SN: AORA100V2...  Battery: 100%  [online]

$ .venv/bin/python bluetti_battery.py status --json
[
  {
    "sn": "AORA100V2...",
    "model": "AORA100V2-AORA100V2",
    "name": "AORA100V2",
    "online": true,
    "battery_percent": 100
  }
]
```

## 別マシンへの移行

```bash
# コピー元
scp -r bluetti-battery/ user@remote:~/

# コピー先
cd ~/bluetti-battery
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bluetti_battery.py status
```

`.env` にトークンが含まれているため、移行先で再認証は不要です。
