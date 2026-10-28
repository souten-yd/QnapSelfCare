# QnapAssistant 非同期健康相談

## 必要バージョン
- QnapAssistant v0.5.9以降
- QnapSelfCare v0.3.45以降
- QnapAssistantの相談先は **Local Qwen / llama.cpp** を選択すること。
  OpenRouterに切り替えている場合、非同期ジョブAPIは外部転送せず拒否します。

## 通信方式
1. 健康相談の入力は従来どおりSelfCare側で集約（最近5件の食事メモや活動等）。
2. SelfCareのサーバーから同一NASのQnapAssistantへ
   `POST /api/llm/jobs`。受付ID `req_<48文字の16進数>` が返ります。
3. iPhoneのブラウザはSelfCare自身の
   `GET /api/ai/consult/status?request_id=...` を数秒ごとに取得します。
4. 状態 `queued` → `loading_model` → `ready` → `generating` →
   `completed`。完了後 `GET /api/ai/consult/result?request_id=...` で回答を表示。
5. 中止ボタンでSelfCareからQnapAssistantへキャンセル要求します。

状態画面には受付ID、経過時間、現在の段階の経過時間、システムの
`MemAvailable`（GiB）と障害理由を表示します。`backend_process_alive`
だけではモデルの準備完了を断定しません。通信切断中は元の受付IDを
保持し、再接続・再取得を試みます。QnapAssistantが再起動するとRAM
上のジョブは失われるため、IDは見つからなくなります。相談内容を
無断で再送しません。

## プライバシーとNASディスクI/O
SelfCareのsessionStorageに保存するのは一時的なランダム受付IDと利用者ID
のみです。質問・健康データ・回答は保存しません。サーバーのHTTPアクセス
ログも従来どおり抑制し、ポーリングでHDDを頻繁に起こしません。
QnapAssistant側もジョブと結果をRAMに保持し、完了後15分で破棄します。

NAS管理画面では自動アンロードを有効化し、アイドル時間を調整してください。
初期設定はOFFであり、有効化しなければロード済みモデルは常駐します。

## 互換性
QnapSelfCareの `POST /api/ai/consult` は従来どおり同期APIです。
OpenRouter / ControlDeck / local 等を選んだ場合も同期処理のままです。
QnapAssistant以外のLLMへの自動転送、失敗時の自動クラウドフォールバック
はありません。
