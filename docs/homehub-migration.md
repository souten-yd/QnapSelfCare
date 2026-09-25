# 画面から共通Bluetooth構成へ移行（0.3.4）

SelfCareを0.3.4へ更新後、「設定・バックアップ → 共通Bluetoothのセットアップ → 共通Bluetooth構成へ更新」を押します。NAS上でHomeHubの構成確認・イメージ取得・設定退避・適用・起動確認を実行します。SSHでComposeを手作業で編集する必要はありません。ページを閉じても処理は続きます。

対象はx86_64 NASの `/share/Container/QnapHomeHub/compose.yaml` に置かれた標準の旧構成または0.3.0の共有構成です。HomeHubとupdaterが起動している必要があります。Docker Composeで解釈した独自の設定は可能な範囲で保持し、必要なBluetooth構成だけを変更します。追加override、別プロジェクト・別データ保存先、別イメージ、Bluetooth機器を直接使う独自設定、0.3.0より新しいHomeHubは上書きしません。専用 `qnapselfcare-bluetooth` が動いている場合も中止します。元のComposeは必ず退避し、設定中の環境変数参照は解釈済みの値になります。既存の利用者設定・機器設定・secrets・`.env`は保持します。

HomeHub 0.3.0のコミット `aed424cf572295ac2636d1cebae6b69c98606498` に対応する公式GHCRのserver/updaterイメージを使用します。テンプレートの出典も同コミットで、旧テンプレートは `6d0d942` です。MITライセンスを `homehub/LICENSE` に同梱しています。

実行順序:

1. 管理者権限・CPU・Docker・Compose・稼働コンテナのプロジェクトと保存先・HomeHub更新処理を確認します。
2. イメージを取得し、移行準備中に既存構成が変わっていないことを再確認します。
3. updaterを停止し、HomeHubと既存radioを停止します。元Compose・イメージID・HomeHub設定・updater設定・secrets・`.env`を管理者用の退避先へ保存します。
4. 共通radioを含むComposeへ切り替え、radioとHomeHubを起動します。Web応答と共通ソケット・BlueZ状態を確認してupdaterを起動します。
5. 完了したら「診断を更新」。共通Bluetoothサービスに接続済みとなったら、利用者・機器を登録してペアリング、手動同期、自動同期の順で確認してください。

HomeHubのWebポート8787、SelfCareの17863を維持します。Matterbridgeコンテナは再作成せず、そのデータも変更しません。移行中はHomeHubのBluetooth操作が一時的に利用できなくなります。NAS・SelfCareを停止しないでください。SelfCare自身の更新と移行は同時実行できません。

退避先はSelfCare保存先の `homehub-migration/<ID>/`、状態は `homehub-migration/status.json` です。鍵を含むため外部公開しないでください。通常の失敗ではradioを止めてから元のCompose・イメージへ戻します。データやsecretsを過去のコピーへ自動上書きすることはありません。

電源断や復旧失敗の場合は中断を保持し、移行の再実行を止めます。画面の「元の構成に戻す」を使用してください。退避Composeはチェックサムを検証してから復旧します。設定退避はSelfCareの週次バックアップとは別管理で、自動削除しません。

この処理は任意URL・パス・シェルコマンドをAPIで受け付けません。既存のHomeHub初回移行を、限定した構成で実行する機能です。実NASでの移行・機器通信は画面の結果と実際の同期で確認してください。

0.3.4では、SelfCare更新中にバックアップが競合した場合はエラーではなく「更新・移行処理の完了待ち」として約1分後に再試行します。バックアップは引き続き週1回・14世代が初期設定です。
