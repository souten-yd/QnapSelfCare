# USB BluetoothとQnapHomeHubの共用

## 方針

Web画面・ポート・記録DBは分離し、Bluetoothの制御だけを共通の `qnaphomehub-radio` コンテナへ集約します。ESP32・Home Assistantは不要です。QNAPのUSBドングルを使用します。

SelfCareの利用頻度が高いため、通常はコンテナ内の新しいBlueZを維持します。HomeHub操作ではBlueZの終了を待ち、既存のSwitchBotManagerを専用子プロセスで実行します。操作後にそのプロセスを終了し、BlueZを再開します。進行中の測定同期を強制中断しません。依頼は直列化し、待機が60秒を超えた操作は実行せず失敗させます。古いSwitchBot押下が後から実行されることを避けます。

両Webサービスが停止しても、もう一方はradioを利用できます。radio停止中は両方のBLE操作が失敗しますが、Webと保存済みデータは利用できます。共通化はこれら2アプリ間の競合を防ぐもので、別のホストBluetoothサービスを自動停止するものではありません。

| 項目 | 設定 |
| --- | --- |
| SelfCare Web | `http://NAS:17863/` |
| HomeHub Web | `http://NAS:8787/` |
| radioソケット | `/share/Container/QnapHomeHub/data/radio/ble.sock`（所有者のみアクセス） |
| adapter | HomeHubのHCI番号とSelfCareの `hci0` 等を一致させる |
| BlueZのキー | HomeHubの `data/bluetooth/` で永続化 |
| SelfCare側キー | SelfCare DB・`config/`（API/JSONバックアップへは出さない） |

## HomeHub 0.2.xから初回移行

これは新しいComposeサービスの追加を伴うため、既存のWeb更新でイメージを更新するだけでは完了しません。NASのSSHで実施します。既存の `data/`・`secrets/` と現在の `compose.yaml` をバックアップしてください。

1. `cd /share/Container/QnapHomeHub` で旧HomeHubを停止します。

   ```sh
   docker compose stop homehub
   ```

2. Git導入なら `git pull --ff-only`、ZIP導入なら0.3.0のリリースZIPを別の場所に展開し、新しい `compose.yaml` と `docker/`・`scripts/` を配置します。既存の `data/` と `secrets/` は保持します。カスタムCompose設定がある場合は新旧を比較して反映します。
3. イメージを取得して起動します。

   ```sh
   docker compose pull
   docker compose up -d radio homehub updater
   docker compose ps
   docker compose logs --tail=80 radio
   ```

4. HomeHub管理画面のBluetooth状態に共通サービスが表示されることを確認し、既存Botの状態取得・押下を確認します。次にSelfCareを導入・更新します。

旧Nobleを動かしたままradioを先に使用しないでください。同じドングルを使用する旧SelfCare専用Bluetoothコンテナや他のBLEスキャンも停止します。QNAPホストのBluetoothデーモンが同じドングルで処理している場合も競合対象です。既存のNASサービスを無条件に停止する処理は含めていません。

ロールバックするときはSelfCareの自動同期をOFFにして `docker compose stop radio homehub`、退避した旧Composeを戻し、旧バージョンのserverイメージを指定してHomeHubを再起動します。`data/bluetooth/` やSelfCare DBは削除しません。

## Omron登録

1. SelfCareの設定で利用者を追加します。
2. 機器側のペアリング操作を行い、SelfCareで「共通Bluetoothサービス」を選んでスキャンします。スマートフォンの同時接続は避けます。
3. 型番、MACアドレス、HCI、機器内の利用者番号とSelfCare利用者の対応を保存します。日本時間の機器時計は時差540分です。
4. 機器をペアリングモード（HEMの「-P-」表示）にして「ペアリング」を実行します。
5. 通信可能な状態で「履歴を同期」し、実際の測定日時・値・利用者を比較します。
6. 確認後に自動同期をONにします。HomeHub操作後に次の測定も保存されることを確認します。

既存アプリとのペアリングを変更するので、移行前に必要な履歴を保存してください。初回読取、両機種の全利用者スロット、二重取得の重複排除、再起動後の鍵維持、SwitchBot復帰はNASでの最終確認項目です。実機接続成功はまだ報告されていません。

## HomeHubを使わない場合

同梱の専用BLEコンテナを使う場合:

```sh
cd "$(/sbin/getcfg QnapSelfCare Install_Path -f /etc/config/qpkg.conf)"
docker compose -f bluetooth/compose.yaml up -d --build
```

SelfCareで「直接接続」を選び、ドングルを専用に使う確認をONにします。BlueZ対応USBドングルとContainer Stationが必要です。共通radioと専用コンテナを同時起動しません。BlueZは複数のHCIを認識するため、別ドングルでもBlueZデーモンの二重起動は避けます。QPKGを更新した場合は上記コマンドで専用コンテナも再ビルドします。

NAS本体に対応するBlueZ/D-Busがある場合は `sh scripts/install-ble.sh` でPython依存だけを追加し、ネイティブ直接接続を使えます。古いQNAPのBlueZ 4.xはこの経路の対象外です。共通radio/専用コンテナは新しいユーザー空間BlueZを同梱しますが、NASカーネルの対応も必要です。

## 診断

SelfCareの「設定・バックアップ → 診断」と操作履歴、HomeHubのBluetooth状態、`docker compose logs --tail=100 radio` を確認します。スキャンは完了するがOmronが出ない場合は機器の通信モードを確認します。`SelfCare adapter must match...` はHCI設定の不一致です。ソケット未検出はComposeサービス未起動またはパス相違です。

HomeHubの配置を変更した場合、SelfCare起動時の環境変数 `SELFCARE_HOMEHUB_SOCKET` で実際のソケットを指定します。既定QPKG起動では標準配置を使います。radioの状態応答はサービスの生存確認であり、実機接続の成功を示すものではありません。
