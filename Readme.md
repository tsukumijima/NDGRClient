
# NDGRClient

**NDGRClient**: Nicolive NDGR Message Server Client Library & Command Line Tool

![Screenshot](https://github.com/user-attachments/assets/d0d8765d-3f77-4bf2-a149-b979979e1104)

## About

2024/08/05 から [ニコニコ生放送](https://live.nicovideo.jp/) で運用されている NDGR メッセージサーバーのクライアントライブラリと、ライブラリを活用したコマンドラインツールです。  
主に [ニコニコ実況](https://jk.nicovideo.jp/) のコメントを取得する用途での利用を想定しています。

> [!TIP]
> このライブラリは、[JKCommentCrawler (ニコニコ実況の過去ログ収集ツール)](https://github.com/tsukumijima/JKCommentCrawler) において、NDGR メッセージサーバーからの過去ログ取得処理に活用されています。

## NDGR メッセージサーバーとは？

`https://mpn.live.nicovideo.jp` 以下で運用されている API サーバーのことを便宜的にそう呼称しています。`NDGR` は「のどぐろ」を意味するコードネームのようです。  

> [!TIP]
> ニコニコ生放送・ニコニコ生放送 (Re:仮) で運用されているものと同一設計のサーバーのクライアント実装が、05/21 時点で実際に [N Air (ニコ生配信者向けに公開されている Streamlabs OBS のフォーク) のソースコード](https://github.com/n-air-app/n-air-app/blob/n-air_development/app/services/nicolive-program/NdgrClient.ts) に `NdgrClient` として追加されていることが確認できます。

サイバー攻撃以前のニコ生では、視聴リクエスト・統計情報・コメントの送受信などをすべて WebSocket API で送受信していました。  
具体的には、視聴開始リクエスト・コメント送信・統計情報の受信・座席の維持などを行う「視聴セッション維持用 WebSocket API」と、(運営コマンドやニコニ広告などの特殊コメントを含む)「コメント受信用 WebSocket API」の両方に WebSocket 接続を行う必要がありました。
 
NDGR メッセージサーバーは従来の「コメント受信用 WebSocket API」(niwavided メッセージサーバー) の代替で、**通信プロトコルが従来の WebSocket での JSON 送受信から「Protobuf 形式のバイナリを Length-Delimited Protobuf Stream として返す HTTP ストリーミング API の定期的なポーリング」に変更されている点が最大の特徴です。**  

> [!TIP]
> 従来のメッセージサーバーの通信プロトコルは、Flash 時代の XMLSocket のデータ構造をそのまま WebSocket + JSON に置き換えただけの代物で、極めてレガシーな仕様を長年引き摺っていました。  
> 今回刷新された新メッセージサーバーでは Protobuf によりメッセージが構造化されており、今まで `/nicoad` などの特殊コメントとして実装されていた生放送制御用メッセージが明確に分離されたりなどのモダン化を果たしています。

> [!TIP]
> エンドユーザーからすると「コメントサーバー」の方が自然な呼び方に思えますが、従来から通常のコメントに混じって生放送の進行状況やニコニ広告などの制御用メッセージも送られていることから、ドワンゴ内部では「コメント」と「制御用メッセージなど」をまとめて「メッセージ」と呼び、それを届けるサーバーなので「メッセージサーバー」と呼んでいるようです。

AWS 上で稼働していること、またこのような高度な仕組みは数週間で実装できるようなものではないことから、以前から進行していたニコ生の AWS 載せ替え & フルリニューアル時に、従来のメッセージサーバーを新規開発でクラウドネイティブな NDGR メッセージサーバーで置き換え予定だったと考えられます。  
今回急遽 AWS 上にニコニコ生放送 (Re:仮) 用のメッセージサーバーを構築する必要に迫られ、当時開発途上だった NDGR メッセージサーバーを急遽ニコニコ生放送 (Re:仮) にて前倒して運用開始した、と考えるのが自然です。
 
**その後 2024/08/05 に復旧したニコニコ生放送では、サービス開始当初から使われてきた従来のメッセージサーバーに代わり、NDGR メッセージサーバーの本運用が開始されました。**  
これに伴い旧メッセージサーバーにはアクセスできなくなり (タイムシフト用の過去ログは後日新メッセージサーバーにインポート予定とのこと) 、各コメントビューアーでも新メッセージサーバーへの対応が必須となっています。

## Usage

### Authentication

#### Auth with OAuth

継続収集には OAuth 認証を利用できます。  
初回だけブラウザでニコニコアカウントを認可すると、以降は NDGRClient がトークンを自動更新して保存します。

> [!NOTE]  
> 初回認可には、ニコニコから発行された OAuth クライアントの ID とシークレットが必要です。  
> `authorize` サブコマンドを使うには、そのクライアントのコールバック URI として `https://app.konomi.tv/api/redirect/niconico` が登録済みである必要があります。  
> 登録されていない場合は、ご自身の認可サーバーで `oauth.json` を作成する処理を用意してください。  
> なお、発行されたアクセストークンとリフレッシュトークンの更新は、ライブラリ側で自動的に行われます。

```bash
poetry run python -m ndgr_client authorize --oauth-file /absolute/path/oauth.json
```

上記コマンドを実行後、クライアント ID 、クライアントシークレットの順に入力し、表示された URL をブラウザで開いて認可を完了してください。  
環境変数 `NICONICO_CLIENT_ID` と `NICONICO_CLIENT_SECRET` に指定した値も使用できます。  

> [!TIP]
> 中継が戻すコールバック URL にブラウザから届かない場合は、その URL をコピーし、コマンドを実行しているターミナルで `curl` してください。  
> 認可の待ち時間は10分です。

取得した OAuth 認証情報を使うには、 `stream` / `download` サブコマンドに `--oauth-file /absolute/path/oauth.json` のように oauth.json のパスを指定します。  

> [!IMPORTANT]  
> 実行中にアクセストークン・リフレッシュトークンが更新される可能性があるため、同一の認証情報を使うプロセスには同一の oauth.json を指定してください。  
> 更新処理はファイルロックで直列化されるため、ファイルを複製して別々に更新する運用は避けてください。

#### Auth with Cookie

Cookie で認証する場合は、Cookie 名と値の JSON 辞書を保存したファイルを `--cookies-file /absolute/path/cookies.json` で指定します。  
Cookie の期限切れはエラーになり、ブラウザで取得した新しい Cookie への差し替えが必要です。  
OAuth と Cookie はいずれか一方を指定し、どちらも省略した場合は未認証で接続します。  

> [!WARNING]  
> NDGRClient 1.x にはメールアドレスとパスワードによるログイン API `login()` がありましたが、バックエンドのログイン API が廃止されたため、2.x では削除されています。  
> ニコニコ側がログイン時に Cloudflare Turnstile を要求するように仕様変更したことで、2026年8月現在ではメアドとパスワードを投げるだけではログイン用 Cookie を取得できなくなっています。  
> 今後も安定的にコメントを収集し続けるには OAuth 認証の方が望ましいです。

> [!WARNING]
> OAuth 認証情報ファイルにはクライアントのシークレットとトークンが含まれます。  
> Cookie ファイルとともに Git や共有ストレージへ公開しないでください。  

> [!IMPORTANT]
> 保存先のフォルダには収集プロセスからの書き込み権限が必要です。  
> Linux では OAuth 認証情報ファイルを所有者だけが読み書きできる権限で保存します。

### Streaming comments

![Screenshot](https://github.com/user-attachments/assets/d0d8765d-3f77-4bf2-a149-b979979e1104)

```bash
poetry run python -m ndgr_client stream <nicolive_program_id or jikkyo_id>
```

指定したニコニコ生放送 ID (ex: lv345479988) または実況チャンネル ID (ex: jk1, jk9, jk211) に対応する番組に投稿されたコメントを、リアルタイムにコンソールに表示します。  
終了するにはコンソールで `Ctrl+C` を入力してください。

> [!TIP]
> `--oauth-file` または `--cookies-file` を付けると、認証済みの状態でコメントを取得できます。

> [!TIP]
> `--verbose` オプションを付けると、詳細な動作ログが出力されます。

### Download Kakolog

![Screenshot](https://github.com/user-attachments/assets/8d354d3a-8b24-4c0d-ad6d-db83a0e7695a)

```bash
poetry run python -m ndgr_client download <nicolive_program_id or jikkyo_id> --output-dir <output_dir>
```

指定したニコニコ生放送 ID (ex: lv345479988) または実況チャンネル ID (ex: jk1, jk9, jk211) に対応する番組に投稿された、過去すべてのコメントを一括でダウンロードし、`(output_dir)/(nicolive_program_id or jikkyo_id).nicojk` に保存します。  
`--output-dir` を指定しない場合、カレントディレクトリに保存されます。

コメントは最新のものから過去に遡るようにまとめて取得され、取得されたコメントは随時コンソールに表示されます。

> [!TIP]
> `jikkyo_id` に `all` を指定すると、すべてのニコニコ実況チャンネルのコメントを一括でダウンロードできます。  
> OAuth 認証で視聴 API が `PROGRAM_ENDED` を返したチャンネルは、スキップしたことを表示して次へ進みます。  
> 認証失敗や通信障害はエラーとして扱い、処理を停止します。

> [!TIP]
> `--oauth-file` または `--cookies-file` を付けると、認証済みの状態でコメントを取得できます。  
> 放送が終了した番組のコメントをダウンロードするには、プレミアム会員アカウントへのログインが必要です。

> [!TIP]
> `--verbose` オプションを付けると、詳細な動作ログが出力されます。

### Use as Library

今のところ PyPI には公開していませんが、GitHub の URL を指定してインストールできます。

```bash
pip install git+https://github.com/tsukumijima/NDGRClient
```

## Special Thanks

[@rinsuki](https://github.com/rinsuki) (https://github.com/rinsuki-lab/ndgr-reader)

## License

> [!NOTE]
> [proto/](/proto/) 以下の .proto ファイルは、[n-air-app/nicolive-comment-protobuf](https://github.com/n-air-app/nicolive-comment-protobuf) からインポートしたものです。

[MIT License](License.txt)
