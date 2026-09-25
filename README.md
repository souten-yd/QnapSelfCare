# QnapSelfCare

QNAP上でOMRON HEM-6232T（血圧計）とHBF-228T（体重体組成計）の測定履歴を管理するためのQPKG設計です。0.1.0は**更新取得コマンドのみのプレビュー**です。測定データの自動収集とNAS実機動作は未検証です。

## 構成と段階

| 機能 | 設計 | 現状 |
| --- | --- | --- |
| HEM-6232T | QNAPのHome Assistant Container + ESPHome Bluetooth Proxy + `hass-omron` を利用し、測定イベントをローカル保存層へ取り込む | 設計のみ。実機ペアリング・履歴同期を検証する |
| HBF-228T | 端末内の履歴をBLE GATTで読み、利用者スロットと測定日時を保持して保存する専用アダプター | 設計のみ。ESPHome Bluetooth Proxy経由の任意GATT操作は未確認。USB BLEまたは専用ESP32ファームウェアを実機で選定する |
| データ | SQLite（測定時刻・受信時刻・機器ID・利用者スロット・単位・元データの識別子）をQPKG外の永続領域に置き、重複を防ぐ | 未実装 |
| 更新 | GitHubの最新安定版Releaseを照会し、機種別QPKGをSHA-256検証後に取得。App Centerで手動適用 | `updater.py` の照会と取得を実装。0.1.0 QPKGに同梱。Python 3が別途必要 |

この設計ではQPKGは設定・保存・更新の入口を担当します。Home AssistantとBluetooth Proxyは別コンポーネントとして利用し、既存Container Stationの設定や他のコンテナを自動で変更しません。いずれの測定器もBluetoothペアリング、通信可能な時間帯、履歴の保持数に依存するため「測るだけで常に即時保存」を保証する段階ではありません。OMRON connect側の履歴は移行前にエクスポート・バックアップしてください。ペアリング先を変えると従来の同期が使えなくなる可能性があります。

## QPKGの構成案

```text
QnapSelfCare QPKG
  service.sh             起動・停止、構成と保存先の検証
  updater.py             GitHub Releaseの確認とQPKG取得
  adapters/hem6232t      Home Assistant側の受信・バックフィル（将来）
  adapters/hbf228t       BLE GATT履歴読取（実機検証後）
  store/                 SQLite保存・重複排除・CSVエクスポート（将来）
  web/                   ローカル表示・同期状態・更新通知（将来）
```

QPKGはQDKでビルドし、NASのCPUアーキテクチャごとにRelease assetを用意します。Container Station、Home Assistant、Bluetooth Proxyは明示的なセットアップ手順に従って導入します。QNAPの管理画面やNASの外部公開を前提としません。健康データはローカルに保管し、バックアップ対象・アクセス権・削除方法を実装段階で定義します。

## 更新の使い方（開発者向け）

`python3 updater.py check --current-version 0.1.0 --arch x86_64` で公開中の最新安定版を確認します。新しいQPKGがある場合、`python3 updater.py download --current-version 0.1.0 --arch x86_64 --dest /path/to/private/staging` で取得します。`--arch` はNASに対応するRelease asset名の値を指定してください。0.1.0は更新確認コマンドのみを同梱するプレビューリリースです。QPKG内にPython 3は同梱していないため、NASで更新確認を使う場合はPython 3が必要です。QPKGは `x86_64` と `arm_64` 向けにQDKでビルドします。NASのCPU種別とQTSのバージョンを確認して該当するファイルをApp Centerから手動インストールしてください。実機インストールの検証は未実施です。

Releaseのタグは `vX.Y.Z`、assetは `QnapSelfCare_X.Y.Z_<arch>.qpkg` とします。GitHubのRelease APIが返すassetの `digest`（`sha256:...`）がない場合は取得しません。取得後もSHA-256を照合し、失敗時はファイルを残しません。ドラフト・プレリリース・同一/旧バージョンは対象外です。QPKGの**適用はQNAP App Centerの手動インストール**で行います。更新確認や取得によってサービス・DB・コンテナは変更されません。

## 実装マイルストーン

1. QPKG設計とRelease照会・検証付き取得（この段階）。
2. QDKビルドとQNAP実機でのインストール・更新・アンインストール、データ永続化と復旧を検証。
3. HEM-6232TをHome Assistant経由で取り込み、日時・利用者・重複・再接続を検証。
4. HBF-228TのBLE経路と読める項目を実機で確定し、履歴・複数ユーザーを検証。
5. ローカル表示、CSV移行、更新通知とバックアップを仕上げる。

既存のopenScale実装はHBF-228TのWLCプロトコルの参考資料ですが、GPLコードをそのまま取り込む場合は配布ライセンスと依存関係を確認します。対応項目を推測で増やしません。

## 参考資料

- [hass-omron](https://github.com/eigger/hass-omron)（HEM-6232T対応とペアリング条件）
- [openScale HBF-228T実装](https://github.com/oliexdev/openScale/blob/master/android_app/app/src/main/java/com/health/openscale/core/bluetooth/scales/OmronWlcHandler.kt)（WLC履歴転送）
- [QNAP QPKG開発ガイド](https://www.qnap.com/en/how-to/tutorial/article/qpkg-development-guidelines)
- [QDK](https://github.com/qnap-dev/QDK)
- [GitHub Releases API](https://docs.github.com/en/rest/releases/releases)
