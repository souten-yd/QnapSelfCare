# QnapSelfCare 低ディスク I/O（v0.3.64）

## NASでの観測

2026-10-30のNAS測定では、QnapSelfCareの60秒間のwrite_bytesとcancelled_write_bytesがともに294912 B、wchar=72 B、syscw=72回。SQLite WALとSHMがそれぞれ9回CREATE/DELETE、SHM変更20回。DB本体、selfcare.log、service.lockのサイズ・更新時刻は計測前後で変化しなかった。プロセスcwdは .tmp-QnapSelfCare (deleted)。物理ディスクの実書き込み量はこの情報だけでは確定できない。

## 実装

storage.Store.keep_wal_alive() はmode=rw、isolation_level=None、query_only=ONの維持用SQLite接続を1つ使用する。利用者のデータ処理に使用せず、長期間の読み取りトランザクションを保持しない。HTTPサーバーと同期・バックアップ・AIワーカーの稼働期間にだけ維持し、停止時に閉じる。既存の通常Store.connect()のトランザクション確定、PRAGMA synchronous=FULL、WALモード、バックアップの保存保証を変えない。データはNAS内に永続保存し、RAMへ移動しない。WALとSHMがサービス稼働中に存在し続けることは許容する。DB消失時に新規DBを作ることはない。

qpkg/shared/selfcare_pid.pyはQPKGの開始・停止時のみ/procを検査する。正確なPython起動引数（ROOT/webapp.py --lan --port 17863 --data-dir DATA）、実行ファイル、rootまたはQTSの一時cwd（deletedを含む）、PID開始時刻が一致する場合に限りSIGTERM。停止待ちは最大14秒、失敗したら二重起動せずエラーとする。PID番号だけでSIGTERMしない。別アプリのプロセスを停止しない。起動をroot cwdで行い、Python -Bでバイトコード書込みを抑制する。起動時のnpmビルド、監視デーモン、強制SIGKILLは追加しない。

## テストと実機での評価

1. Pythonテストで連続読取時のWAL/SHM inode安定性、書込みのFULL保証とチェックポイント、トランザクションロールバック、再起動後のデータ保全を検証する。
2. QTSの削除済み.tmp-QnapSelfCare cwdを実プロセスで模擬し、孤立プロセスの安全な復旧、誤ったPIDファイルと無関係プロセスを保護する。
3. NAS導入後に同じinotify計測を60秒間実行。WAL/SHMのCREATE/DELETEが従来の9回/分から減少したか比較。QnapControlCenterの5分計測でディスク別書込みとjbd2の差も比較する。
4. 健康記録の作成、同期、整合性検査、バックアップと必要に応じた隔離環境の復元を確認する。

禁止：SQLite synchronous OFF、WAL手動削除、健康記録のRAM化、ファイルシステムjournal無効化、許可外PIDへのkill、QTS設定の改変。
