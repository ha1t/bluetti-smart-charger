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
3. **最安Nスロット戦略** → SOCギャップと消費速度から必要な充電スロット数Nを算出し、24h先の価格ウィンドウで最も安いNスロットでのみ充電

消費速度は SOC 履歴の放電期間から自動算出されます。履歴が不足している場合は `DEFAULT_CONSUMPTION_RATE` のフォールバック値を使用します。

全ての API コール（Looop / SwitchBot / BLUETTI）には指数バックオフ付きリトライ（最大3回）が組み込まれており、一時的なネットワーク障害による誤動作を防ぎます。

### 必要な設定

`.env` に以下を追加:

```
SWITCHBOT_TOKEN=<SwitchBot API トークン>
SWITCHBOT_SECRET=<SwitchBot API シークレット>
SWITCHBOT_DEVICE_ID=<Plug Mini のデバイス ID>
LOOOP_AREA=01          # 電力エリア (01=北海道)
SOC_MIN=20             # 強制充電する下限 (%)
SOC_MAX=80             # 充電停止する上限 (%)
CHARGE_RATE_PCT_PER_SLOT=10   # 30分の充電で増える SOC% (デフォルト: 10)
DEFAULT_CONSUMPTION_RATE=3.0  # 履歴不足時のフォールバック消費率 %/h (デフォルト: 3.0)
PUSHBULLET_TOKEN=<Pushbullet API トークン>  # 通知用 (任意)
```

### Pushbullet 通知

`PUSHBULLET_TOKEN` を設定すると、以下のイベント時に Pushbullet でプッシュ通知が送られます:

- **エラー発生時** — フェイルセーフが発動した場合
- **プラグ制御失敗時** — フェイルセーフで充電器を ON にできなかった場合
- **トークン期限切れ間近** — BLUETTI API トークンの有効期限が残り7日以内の場合

設定しない場合、通知なしで動作します。

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
