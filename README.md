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

## スマート充電制御

`charge_controller.py` は Looop でんきの30分ごとの電力単価に基づいて、SwitchBot Plug Mini 経由で充電器の電源を自動制御します。

### 仕組み

cron で30分ごとに実行され、以下の優先順位で充電の ON/OFF を判定します:

1. **SOC <= SOC_MIN (20%)** → 強制充電（バッテリー保護）
2. **SOC >= SOC_MAX (80%)** → 充電停止
3. **現在の電力単価 <= 24h先平均単価** → 充電開始（安い時間帯）
4. **現在の電力単価 > 24h先平均単価** → 充電停止（高い時間帯）

電力単価は Looop でんき API から当日・翌日の価格を取得し、現在時刻から24時間先までの48スロット（30分単位）の平均と比較します。当日のみの平均ではなく先読みすることで、夜間に翌日の安い時間帯を考慮した判断ができます。

### 必要な設定

`.env` に以下を追加:

```
SWITCHBOT_TOKEN=<SwitchBot API トークン>
SWITCHBOT_SECRET=<SwitchBot API シークレット>
SWITCHBOT_DEVICE_ID=<Plug Mini のデバイス ID>
LOOOP_AREA=01          # 電力エリア (01=北海道)
SOC_MIN=20             # 強制充電する下限 (%)
SOC_MAX=80             # 充電停止する上限 (%)
```

SwitchBot のデバイス ID は以下で確認できます:

```bash
.venv/bin/python charge_controller.py list-devices
```

### cron 設定例

```bash
*/30 * * * * /home/ha1t/src/bluetti-battery/.venv/bin/python /home/ha1t/src/bluetti-battery/charge_controller.py run >> /home/ha1t/src/bluetti-battery/charge.log 2>&1
```

### コマンド

```bash
# 充電制御を実行（プラグ操作あり）
.venv/bin/python charge_controller.py run

# dry-run（判定結果の表示のみ、プラグ操作なし）
.venv/bin/python charge_controller.py run --dry-run

# 今日の電力単価一覧
.venv/bin/python charge_controller.py prices

# SOC 履歴と平均消費速度
.venv/bin/python charge_controller.py history [--hours 24]
```

### SOC 履歴

各実行時のバッテリー残量が SQLite (`soc_history.db`) に自動保存されます。蓄積されたデータから放電期間の平均消費速度（%/hour）を算出できます。

```
$ .venv/bin/python charge_controller.py history
SOC history (last 24h): 48 records
Avg consumption rate: 6.2 %/hour (3.1 %/slot)

Time              SOC   Status
----------------------------------------
  02-11 00:00   82%       |################
  02-11 00:30   78%  CHG  |###############
  02-11 01:00  100%       |####################
  ...
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
