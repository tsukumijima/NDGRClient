
# NDGRClient

**NDGRClient**: Nicolive NDGR Server Client

## About

[ニコニコ生放送 (Re:仮)](https://live.nicovideo.jp/rekari/kl1) で運用されている NDGR サーバーのクライアントライブラリと、ライブラリを活用したコマンドラインツールです。  
主に [ニコニコ実況 (Re:仮)](https://originalnews.nico/464285) のコメントを取得する用途での利用を想定しています。

> [!NOTE]
> **NDGR サーバーとは？**  
> 
> 2024/07/19 時点で `https://mpn.live.nicovideo.jp` 以下で運用されている API サーバーのことを便宜的にそう呼称しています。  
> `NDGR` が何を意味しているかは分かりませんが、ニコニコ生放送 (Re:仮) で運用されているものと同一設計のサーバーのクライアント実装が、05/21 時点で実際に [N Air (ニコ生配信者向けに公開されている Streamlabs OBS のフォーク) のソースコード](https://github.com/n-air-app/n-air-app/blob/n-air_development/app/services/nicolive-program/NdgrClient.ts) に `NdgrClient` として追加されていることが確認できます。
>
> NDGR サーバーの最大の特徴は、サイバー攻撃以前のニコ生では視聴開始リクエスト・統計情報・コメントなどを全て WebSocket API で送受信していたところ、**Protobuf 形式のバイナリを連結してストリームとして返す HTTP ストリーミング API** (定期的なポーリングが前提) と、**コメント投稿用の REST API** の組み合わせに変更されていることです。  
> 
> AWS 上で稼働していること、またこのような高度な仕組みは数週間で実装できるようなものではないことから、以前から進行していたニコ生の AWS 載せ替え & フルリニューアル時に、従来の WebSocket API を新規開発でクラウドネイティブな NDGR サーバーで置き換え予定だったと考えられます。  
> 今回急遽 AWS 上にニコニコ生放送 (Re:仮) 用の生放送配信サーバーを構築する必要に迫られ、当時開発途上だった NDGR サーバーを急遽ニコニコ生放送 (Re:仮) の基幹システムとして流用した、と考えるのが自然です。
>
> …つまり、**ニコニコ生放送 (Re:仮) は単なる使い捨ての仮説システムではなく、まだ作りかけだった「新ニコ生」を既存の開発資産と合わせながら間に合わせで動かしている代物と考えられます。**  
> **そして現在運用されている NDGR サーバー経由でのコメント受信ロジックは、「新ニコ生」公開後も継続される可能性が高いです。**  
> その証拠に、従来は WebSocket API から受信していたアンケートや視聴統計情報が、[NDGR サーバーから送られてくる Protobuf メッセージとして定義](https://github.com/n-air-app/nicolive-comment-protobuf/blob/main/proto/dwango/nicolive/chat/data/atoms.proto) されていることが確認できます。  
> 
> …要は「NDGR サーバーのクライアント実装はニコニコ生放送 (Re:仮) 終了後も多少手直しすれば引き続き使える (無駄にはならない) だろう」と賭けて、実装してみた次第です。

## Usage

### Streaming

```bash
poetry run python -m ndgr_client stream <jikkyo_id>
```

指定した実況チャンネル ID (jk1, jk9, jk211 など) に投稿されたコメントを、リアルタイムにコンソールに表示します。

> [!TIP]
> `jk0` を指定すると、（ニコニコ実況ではありませんが）特別に「サイバー攻撃からのニコニコ復旧を見守る場所」のコメントを表示できます。

### Download Kakolog

```bash
poetry run python -m ndgr_client download <jikkyo_id>
```

指定した実況チャンネル ID (jk1, jk9, jk211 など) に投稿された、過去すべてのコメントを一括でダウンロードします。  
コメントは最新のものから過去に遡るようにまとめて取得され、取得されたコメントは随時コンソールに表示されます。

> [!TIP]
> `jk0` を指定すると、（ニコニコ実況ではありませんが）特別に「サイバー攻撃からのニコニコ復旧を見守る場所」のコメントをダウンロードできます。

## Special Thanks

[@rinsuki](https://github.com/rinsuki) (https://github.com/rinsuki-lab/ndgr-reader)

## License

> [proto/](/proto/) 以下の .proto ファイルは、[n-air-app/nicolive-comment-protobuf](https://github.com/n-air-app/nicolive-comment-protobuf) からインポートしたものです。

[MIT License](License.txt)
