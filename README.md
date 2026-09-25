# QnapSelfCare

QNAPでOMRON HEM-6232T（血圧計）とHBF-228T（体重体組成計）の測定履歴を管理します。0.3.0は利用者・機器管理、記録の保存・編集、グラフ、CSV、バックアップとUSB Bluetooth収集を実装しています。0.2.6のWeb接続は利用者のNASで確認済みです。**今回追加したBLEペアリング・履歴同期は実機での検証が必要です。** ESP32は使用しません。

## 機能

- 利用者別の血圧・脈拍・体重・体組成履歴、期間フィルター、グラフ。
- 手入力・修正・削除。削除・修正済みの元記録はBluetoothで再取得しても復活させません。
- HEM-6232Tの2人分・HBF-228Tの4人分の機器スロットを利用者へ割当。
- USB BLEのスキャン、明示的なペアリング、保存履歴読取、自動探索と同期。
- SQLiteへの永続保存、測定時刻のUTC正規化と重複排除。
- QnapSelfCare形式のCSV取込・出力、JSONバックアップ・空の保存先への復元。
- GitHub Releaseの更新確認、SHA-256を確認したQPKG取得。適用はApp Centerから行います。

| サービス | 待ち受け | 役割 |
| --- | --- | --- |
| QnapSelfCare | `17863` | 健康記録の管理・表示・収集依頼 |
| QnapHomeHub | `8787` | SwitchBot・Matter連携の管理・操作依頼 |
| 共通radio | TCPポートなし | NAS内Unixソケットで依頼を受け、USB BLEを直列制御 |

QnapHomeHub 0.3.0のradioサービスとの共用が既定です。通常はSelfCare用BlueZを維持し、SwitchBot操作時だけNobleへ切り替えます。別のドングルを使う場合、またはHomeHubを使わない場合は直接接続も選べます。[USB Bluetooth導入・共存手順](docs/bluetooth-homehub.md)を参照してください。

## Web画面と起動

QPKGをApp Centerでインストール・有効化し、`http://<NASのIP>:17863/` を開きます。QPKGはIPv4全インターフェースで待ち受け、接続元サブネットを限定しません。TailscaleでNASのIPv4へ到達できる場合も同じポートです。認証を設けない構成のため、到達できる人は健康記録の閲覧・編集ができます。NASのアクセス設定で利用範囲を管理してください。

Python 3.8以降と既存の `/share/Container` 共有フォルダを使います。`python3-path` がPython3 QPKG、`/opt/bin/python3.11`、`/opt/bin/python3`などを探索します。特殊な場所は `SELFCARE_PYTHON` で指定できます。起動に `nohup` は不要です。Web・保存・CSV機能はPython標準ライブラリだけで動作します。共通radioを利用する場合、NAS本体へのBleakやBlueZの追加は不要です。

ログは `/share/Container/QnapSelfCare/logs/selfcare.log`、インストール先は次で確認できます。

```sh
/sbin/getcfg QnapSelfCare Install_Path -f /etc/config/qpkg.conf
```

最初に「設定・バックアップ」で利用者を追加します。手入力・CSV取込はすぐに利用できます。Bluetoothを使う場合は、共通radioの導入後に機器を登録し、利用者スロットを割当、ペアリング、手動同期の順で確認してから自動同期を有効にします。

## 保存とバックアップ

永続データの既定ルートは `/share/Container/QnapSelfCare` です。QPKG本体の更新とは別に保持します。

| パス | 内容 |
| --- | --- |
| `data/measurements.sqlite3` | 利用者、機器、記録、重複防止、操作結果、機器のアプリ側ペアリングキー |
| `config/` | ペアリング中の復旧用キー（非公開） |
| `logs/selfcare.log` | サービスログ |
| HomeHub側 `data/bluetooth/` | 共通radioのBlueZ bonding情報 |

DB・設定は管理者用の権限で保存します。WebのJSONバックアップには利用者・機器・全記録・削除履歴を含めますが、ペアリングキーは含めません。復元後は再ペアリングし、自動同期を再度有効にします。復元時は既存データへ上書きせず、全内容の検証と保存を1トランザクションで実行します。ファイル上限32MB・記録100,000件です。完全な運用バックアップには、SelfCareとradioを停止して上記ディレクトリを権限を保持してコピーしてください。

CSVは画面からテンプレートを取得できます。`measured_at` は `2026-09-25T07:30:00+09:00` のように時差を含めます。`kind` は `blood_pressure` または `body_composition`、利用者は画面で選択またはCSV内の `user_id` を使います。1回8MB・10,000件まで全行を検証し、エラーがあれば保存しません。OMRON connect独自形式のCSVをそのまま読む機能ではありません。表計算での数式実行を避けるため、出力CSVの該当文字列には先頭のアポストロフィを付けます。

## BLEの範囲

HEM-6232Tは血圧・脈拍、HBF-228Tは体重・体脂肪率・骨格筋率・BMI・基礎代謝・体年齢・内臓脂肪レベルを読み取ります。機器内の有効な記録のみを扱い、未設定スロットは収集しません。機器時計は設定したUTC時差で解釈します。機器の時計や測定履歴を消去・書換えする操作は実装していません。ペアリング時は接続用キーを登録します。

自動同期は、未待機の機器があると約10秒スキャンし、検出した登録機器だけに接続します。成功・失敗にかかわらず機器ごとの同期間隔を設けます。広告停止中やスマートフォンとの接続中は収集できません。保存可能な履歴件数、電波、旧QNAPカーネルとUSBドングルの互換性は実機に依存します。

## 開発・更新

```sh
python3 -m unittest discover -s tests -v
npm ci
npm test
python3 webapp.py --port 17863 --data-dir /tmp/selfcare-dev
```

開発起動はloopback待ち受けです。`--lan` で全IPv4インターフェースを使用します。CIはPython・DOM操作テストとQDKのx86_64 / arm_64ビルドを行います。実機BLE・NASのコンテナ起動はCIの対象外です。

```sh
./selfcare-update check --current-version 0.3.0 --arch x86_64
./selfcare-update download --current-version 0.3.0 --arch x86_64 --dest /path/to/private/staging
```

配布名は `QnapSelfCare_X.Y.Z_<arch>.qpkg` です。GitHubが返すSHA-256 digestとダウンロードを照合し、不一致・digest欠落時は取得を完了しません。

BLEのプロトコル実装・第三者コードの出典と配布条件は [THIRD_PARTY.md](THIRD_PARTY.md) に記載しています。
