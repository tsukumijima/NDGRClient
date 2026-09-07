# NDGRClient

2024/08/05 からニコニコ生放送で運用されている新メッセージサーバー (NDGR) のクライアントライブラリと、それを使った CLI ツール。主にニコニコ実況のコメント取得用途を想定している。  
NDGR は従来の「WebSocket + JSON」方式のコメント受信を、「Protobuf 形式のバイナリを Length-Delimited Protobuf Stream として返す HTTP ストリーミング API の定期的なポーリング」に置き換えたもの。

## 技術スタック

- Python 3.11 以上 (`pyproject.toml` で `python = ">=3.11,<4.0"`)
- パッケージマネージャは Poetry
- 通信は curl-cffi (Chrome 144 の TLS/HTTP フィンガープリントに偽装、`impersonate='chrome'` + `http_version='v3'` で HTTP/3 を優先しつつフォールバック)
- データ検証は Pydantic (`ndgr_client/constants.py` に `NicoLiveProgramInfo` / `NDGRComment` / `XMLCompatibleComment` を定義)
- Protobuf は [n-air-app/nicolive-comment-protobuf](https://github.com/n-air-app/nicolive-comment-protobuf) の .proto から protoc + mypy-protobuf で生成 (`ndgr_client/proto/`)
- CLI は Typer ベース。Typer 標準では async コマンドに対応していないため、`ndgr_client/utils.py` の `AsyncTyper` で対応させている
- Lint/Format は Ruff (シングルクォート、`line-length = 120`)
- 自動テスト・CI は現状なし (2026-07時点)。動作確認は CLI (`stream` / `download`) を実際に叩いて目視確認するのが基本

## 主要コマンド

```bash
poetry install                                                      # 依存関係のインストール

poetry run python -m ndgr_client stream <nicolive_program_id>       # コメントをリアルタイムにコンソール表示 (ex: jk1, jk211, lv345479988)
poetry run python -m ndgr_client download <id> --output-dir <dir>   # 過去ログを一括ダウンロードして {id}.nicojk に保存
poetry run python -m ndgr_client version

poetry run task lint            # ruff check --fix .
poetry run task format          # ruff format .
poetry run task update-proto    # .proto を取得し直して Python コード/mypy スタブを再生成
```

## ディレクトリ構成

- `ndgr_client/ndgr_client.py`: `NDGRClient` クラス本体 (約1650行)。ログイン、番組情報取得、コメントストリーミング、過去ログダウンロード、XML 互換形式への変換まで全てここに実装されている
- `ndgr_client/constants.py`: Pydantic モデル定義
- `ndgr_client/protobuf_stream_reader.py`: Length-Delimited Protobuf Stream のパーサー
- `ndgr_client/utils.py`: Typer に async コマンドを対応させる `AsyncTyper`
- `ndgr_client/proto/`: .proto から自動生成された Python コード/mypy スタブ。`update-proto` タスクで再生成される生成物なので直接編集しない
- `proto/`: 生成元の .proto ファイル一式
- `update_proto.py`: 上記の取得・生成を行うスクリプト
- `integration-docs/`: ドワンゴ公式のニコニコ生放送連携ドキュメント ([niconama-docs/integration-docs](https://github.com/niconama-docs/integration-docs), プライベートリポジトリ) を参照用にクローンしたディレクトリ。`.gitignore` 対象でリポジトリ本体には含まれていないため、初回セットアップ時は各自でクローンする必要がある。NDGR のプロトコル仕様や WebSocket API 仕様を確認したいときに参照する

## 下流プロジェクトへの影響 (重要)

このライブラリは PyPI 未公開で、以下の2プロジェクトが GitHub の特定 rev を Poetry の git dependency として固定参照している。

- **JKCommentCrawler** (`/home/tsukumi/JKCommentCrawler`): ニコニコ実況の過去ログ収集ツール。実利用箇所は `jkcommentcrawler/__main__.py` (ログイン・番組 ID 解決・ダウンロード・XML 変換の一連の処理)
- **NX-Jikkyo** (`/Develop/NX-Jikkyo/server`): ニコニコ実況の補完・拡張サービス。`server/pyproject.toml` に同じ rev を指定。実利用箇所は `server/app/app.py` と `server/app/utils/Jikkyo.py` で、ニコ生対応チャンネルのコメントをリアルタイムストリーミングして DB に書き込む専用プロセスから使われている

rev 固定のため、NDGRClient 側でコミットやリリースを行っても下流には自動反映されず、下流側で `poetry update ndgr-client` するか rev を書き換えない限り古い実装のまま動き続ける。

**公開 API のシグネチャ変更・`NDGRComment` 等の Pydantic モデルのフィールド変更・CLI 引数の変更のような破壊的変更を行う場合は、上記2プロジェクトの利用箇所を確認し、影響がないか、あるいは下流側での追従が必要かを確認すること。**  
rev 固定なので変更した瞬間に下流が壊れるわけではないが、将来 rev を更新するタイミングで壊れる。

## asyncio 並行処理の勘所 (重要)

`streamComments()` と `downloadBackwardComments()` は asyncio ベースの状態遷移が中心で、コードの見た目以上に複雑な制御になっている。

- `streamComments()` は「NDGR View API からのエントリ受信タスク (`chunked_entries_task`)」「セグメントごとのメッセージ受信タスク (セグメント単位、`active_segments` に格納)」「毎分定期実行の番組情報監視タスク (`program_info_task`)」「無通信監視タスク (`stream_activity_watchdog_task`)」を並行実行し、`asyncio.Queue` 経由のイベント通知 (`StreamTaskEvent`) で状態を1箇所に集約している
- ニコニコ実況番組は毎日 JST 04:00 に番組が切り替わるため、`program_info_task` が新しい番組 ID を検知すると `'RESTART'` を返し、ストリーミング処理全体を再初期化して透過的に後続番組へ引き継ぐ (通常番組の放送終了時は `'ENDED'`)
- `STREAM_ACTIVITY_TIMEOUT_SECONDS` (90秒) は `fetch_program_info()` の約65秒間隔ポーリングに対して意図的に余裕を持たせた値。このような待機時間・タイムアウト値を変更する際は、他の待機処理との相対関係を崩さないよう他の定数も併せて確認すること
- `downloadBackwardComments()` は `asyncio.wait(..., return_when=asyncio.FIRST_COMPLETED)` で `backward_api_uri` 発見イベントと本体タスクの完了を競合させ、目的の URI が見つかり次第もう一方のタスクを cancel している

このような並行処理・状態遷移を扱うコードを変更する際は、既存の日本語コメント (`##` で書かれた詰まった点の補足を含む) を削らずに残し、新規に追加するロジックにも同水準のコメント密度を保つこと。  
状態遷移が絡む箇所ほど、コメントを削ぎ落とした「動くだけのコード」にすると後から読んだときに意図を追えなくなる。

## ログインセッション判定の経緯

- 2026年6月中旬、ニコニコ側の仕様変更でログインページ (`account.nicovideo.jp/login`) が有効な `user_session` を持つ場合でも `x-niconico-id` ヘッダーを返さなくなり、有効な Cookie セッションを再利用しているにもかかわらず毎回ログイン失敗扱いになる不具合が発生した
- 対応として `_validate_login_session()` (`ndgr_client.py`) を新設し、通常の認証済みページ (`https://www.nicovideo.jp/`) にアクセスして `x-niconico-id` ヘッダーの有無で判定する方式に変更した。保存済み Cookie の再利用時・パスワードログイン成功後の両方でこのメソッドを使っている
- ニコニコ側の認証まわりの仕様は今後も変更される可能性があるため、ログイン関連で不可解な挙動が出た場合は、まずレスポンスヘッダーの構造が変わっていないかを疑うこと
