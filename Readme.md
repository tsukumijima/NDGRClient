
# NDGRClient

**NDGRClient**: Nicolive NDGR Message Server Client Library & Command Line Tool

![Screenshot](https://github.com/user-attachments/assets/16b092dd-419a-421a-8eae-46a20b106298)

## About

[ニコニコ生放送](https://live.nicovideo.jp/) で運用されている NDGR サーバーのクライアントライブラリと、ライブラリを活用したコマンドラインツールです。  
主に [ニコニコ実況](https://originalnews.nico/464285) のコメントを取得する用途での利用を想定しています。

## NDGR サーバーとは？

`https://mpn.live.nicovideo.jp` 以下で運用されている API サーバーのことを便宜的にそう呼称しています。`NDGR` は「のどぐろ」を表すコードネームのようです。  

> [!TIP]
> ニコニコ生放送・ニコニコ生放送 (Re:仮) で運用されているものと同一設計のサーバーのクライアント実装が、05/21 時点で実際に [N Air (ニコ生配信者向けに公開されている Streamlabs OBS のフォーク) のソースコード](https://github.com/n-air-app/n-air-app/blob/n-air_development/app/services/nicolive-program/NdgrClient.ts) に `NdgrClient` として追加されていることが確認できます。

サイバー攻撃以前のニコ生では、視聴リクエスト・統計情報・コメントの送受信などをすべて WebSocket API で送受信していました。  
具体的には、視聴開始リクエスト・コメント送信・統計情報の受信・座席の維持などを行う「視聴セッション維持用 WebSocket API」と、(運営コマンドやニコニ広告などの特殊コメントを含む)「コメント受信用 WebSocket API」の両方に WebSocket 接続を行う必要がありました。
 
NDGR サーバーは従来の「コメント受信用 WebSocket API」(niwavided) の代替で、**通信プロトコルが従来の WebSocket での JSON 送受信から「Protobuf 形式のバイナリを Length-Delimited Protobuf Stream として返す HTTP ストリーミング API の定期的なポーリング」に変更されている点が最大の特徴です。**  

> [!TIP]
> 従来のコメントサーバーの通信プロトコルは、Flash 時代の XMLSocket のデータ構造をそのまま WebSocket + JSON に置き換えただけの代物で、極めてレガシーな仕様を長年引き摺っていました。  
> 今回刷新された新コメントサーバーでは Protobuf によりメッセージが構造化されており、今まで `/nicoad` などの特殊コメントとして実装されていた生放送制御用メッセージが明確に分離されたりなどのモダン化を果たしています。

AWS 上で稼働していること、またこのような高度な仕組みは数週間で実装できるようなものではないことから、以前から進行していたニコ生の AWS 載せ替え & フルリニューアル時に、従来の WebSocket API を新規開発でクラウドネイティブな NDGR サーバーで置き換え予定だったと考えられます。  
今回急遽 AWS 上にニコニコ生放送 (Re:仮) 用の生放送配信サーバーを構築する必要に迫られ、当時開発途上だった NDGR サーバーを急遽ニコニコ生放送 (Re:仮) の基幹システムとして流用した、と考えるのが自然です。
 
**その後 2024/08/05 に復旧したニコニコ生放送では、かつてのサービス開始当初から使われてきた旧設計のコメントサーバーに代わり、NDGR 新コメントサーバーの本運用が開始されました。**  
これに伴い旧コメントサーバーにはアクセスできなくなり (タイムシフト用の過去ログは後日新コメントサーバーにインポート予定とのこと) 、各コメントビューアーでも新コメントサーバーへの対応が必須となっています。

## Usage

### Streaming

![Screenshot](https://github.com/user-attachments/assets/16b092dd-419a-421a-8eae-46a20b106298)

```bash
poetry run python -m ndgr_client stream <nicolive_program_id or jikkyo_id>
```

指定したニコニコ生放送 ID (ex: lv345479988) または実況チャンネル ID (ex: jk1, jk9, jk211) に対応する番組に投稿されたコメントを、リアルタイムにコンソールに表示します。

### Download Kakolog

![Screenshot](https://github.com/user-attachments/assets/31c4ccc5-feb8-47cd-8665-451a180f471a)

```bash
poetry run python -m ndgr_client download <nicolive_program_id or jikkyo_id> --output-dir <output_dir>
```

指定したニコニコ生放送 ID (ex: lv345479988) または実況チャンネル ID (ex: jk1, jk9, jk211) に対応する番組に投稿された、過去すべてのコメントを一括でダウンロードし、`(output_dir)/(nicolive_program_id or jikkyo_id).nicojk` に保存します。  
`--output-dir` を指定しない場合、カレントディレクトリに保存されます。

コメントは最新のものから過去に遡るようにまとめて取得され、取得されたコメントは随時コンソールに表示されます。

> [!TIP]
> `jikkyo_id` に `all` を指定すると、すべてのニコニコ実況チャンネルのコメントを一括でダウンロードできます。

## Special Thanks

[@rinsuki](https://github.com/rinsuki) (https://github.com/rinsuki-lab/ndgr-reader)

## License

> [proto/](/proto/) 以下の .proto ファイルは、[n-air-app/nicolive-comment-protobuf](https://github.com/n-air-app/nicolive-comment-protobuf) からインポートしたものです。

[MIT License](License.txt)
