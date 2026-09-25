# QnapHomeHubとのBluetooth共存

## 現行機の確認事項

QnapHomeHubのmainにある[README](https://github.com/souten-yd/QnapHomeHub/blob/main/README.md)では、QNAP TS-253BeでUSB `0a12:0001`（CSR Bluetooth 4.0）が `hci0` / `UP RUNNING` として確認されています。[compose.yaml](https://github.com/souten-yd/QnapHomeHub/blob/main/compose.yaml)のHomeHubコンテナはhost networkと `privileged: true` を用い、[SwitchBotManager](https://github.com/souten-yd/QnapHomeHub/blob/main/server/src/switchbot-manager.ts)は選択されたHCI番号を `NOBLE_HCI_DEVICE_ID` に設定してから `node-switchbot` を読み込みます。スキャン・コマンドは同一キューで直列化されています。

このため同じドングルの物理的なBLE機能をOmron向けにも使う余地はありますが、**現在のHomeHub APIはOmronの履歴読取・ペアリングを提供していません**。QnapSelfCareから同じ `hci0` を別プロセスで直接開き、BlueZやHome Assistantと同時にスキャン・接続することを動作保証しません。Bluetooth 4.0ドングルで各機種のbonding/GATTが成立するかも未検証です。

## 採用する構成と検証順

1. HomeHubが現在のHCI所有者である状態を保つ。SelfCare 0.2.1の `/api/status` は `/sys/class/bluetooth` のアダプター名を**読むだけ**で、スキャン・ペアリング・初期化・電源操作を行わない。
2. 同じドングルを使う場合はHomeHub内の単一HCI所有者にOmronアダプターを追加し、既存のコマンドキューと接続管理を共有する。機器ごとのbondingキー、利用者スロット、日時、履歴の重複排除を設計してから、SelfCareへ認証された内部APIで測定レコードを渡す。既存のHomeHubのSwitchBot動作を回帰確認する。
3. HEM-6232Tの `hass-omron` + Home Assistant経路を使う場合はHomeHubのraw HCIとの競合を避けるため、別のESPHome Bluetooth Proxy（または別のUSBアダプター）を割り当てる。
4. HBF-228TはopenScaleのWLC方式を参考に、BLE接続方式・ペアリング・取得項目を実機で検証する。現時点でHomeHubのドングルへの直接接続可否は断定しない。

HomeHubの `NOBLE_HCI_DEVICE_ID` やコンテナの権限をSelfCareから書き換えません。共有可否の結論はOmron実機とHomeHubの同時運転試験で決定します。
