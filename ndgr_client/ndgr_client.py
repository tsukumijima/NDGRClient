
from __future__ import annotations

import asyncio
import json
import httpx
import lxml.etree as ET
import re
import traceback
import websockets
from bs4 import BeautifulSoup, Tag
from datetime import datetime
from rich import print
from rich.rule import Rule
from rich.style import Style
from typing import Any, AsyncGenerator, cast, Type, TypeVar

from ndgr_client.protobuf_stream_reader import ProtobufStreamReader
from ndgr_client.proto.dwango.nicolive.chat.data import atoms_pb2 as atoms
from ndgr_client.proto.dwango.nicolive.chat.service.edge import payload_pb2 as chat
from ndgr_client.constants import (
    NDGRComment,
    NDGRCommentFullColor,
    NicoLiveProgramInfo,
    XMLCompatibleComment,
)


class NDGRClient:
    """
    NDGR サーバーのクライアント実装
    実装にあたり、下記リポジトリが大変参考になった
    ref: https://github.com/rinsuki-lab/ndgr-reader

    下記コードでは、便宜的に NDGR サーバーの各 API を下記のように呼称する
    ・NDGR View API : https://mpn.live.nicovideo.jp/api/view/v4/...
    ・NDGR Segment API : https://mpn.live.nicovideo.jp/data/segment/v4/...
    ・NDGR Backward API : https://mpn.live.nicovideo.jp/data/backward/v4/...
    """

    # User-Agent と Sec-CH-UA を Chrome 126 に偽装
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    SEC_CH_UA = '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"'

    # 旧来の実況 ID とニコニコ生放送の ID のマッピング
    ## 08/05 時点の値、今後変更される可能性もある
    JIKKYO_ID_TO_REKARI_ID_MAP: dict[str, str] = {
        'jk1': 'lv345479988',
        'jk2': 'lv345479989',
        'jk4': 'lv345479991',
        'jk5': 'lv345479993',
        'jk6': 'lv345479994',
        'jk7': 'lv345479995',
        'jk8': 'lv345479996',
        'jk9': 'lv345479997',
        'jk101': 'lv345479990',
        'jk211': 'lv345479998',
    }


    def __init__(self, nicolive_program_id: str, show_log: bool = False) -> None:
        """
        NDGRClient のコンストラクタ

        Args:
            nicolive_program_id (str): ニコニコ生放送の番組 ID (ex: lv345479988) or ニコニコ実況のチャンネル ID (ex: jk1, jk211)
            show_log (bool, default=False): グラフィカルなログを出力するかどうか
        """

        if nicolive_program_id.startswith('jk'):
            # nicolive_program_id が jk から始まる場合、ニコニコ実況 ID として扱う
            if nicolive_program_id not in self.JIKKYO_ID_TO_REKARI_ID_MAP:
                raise ValueError(f'Invalid jikkyo_id: {nicolive_program_id}')
            self.nicolive_program_id = self.JIKKYO_ID_TO_REKARI_ID_MAP[nicolive_program_id]
        else:
            # それ以外の場合は lv から始まる通常のニコニコ生放送 ID として扱う
            if not nicolive_program_id.startswith('lv'):
                raise ValueError(f'Invalid nicolive_program_id: {nicolive_program_id}')
            self.nicolive_program_id = nicolive_program_id

        self.show_log = show_log

        # httpx の非同期 HTTP クライアントのインスタンスを作成
        self.httpx_client = httpx.AsyncClient(
            ## リクエストヘッダーを設定 (Chrome に偽装)
            headers = {
                'accept': '*/*',
                'accept-encoding': 'gzip, deflate, br',
                'accept-language': 'ja',
                'origin': 'https://live.nicovideo.jp',
                'referer': 'https://live.nicovideo.jp/',
                'sec-ch-ua': self.SEC_CH_UA,
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'user-agent': self.USER_AGENT,
            },
            ## リダイレクトを追跡する
            follow_redirects = True,
        )


    async def streamComments(self) -> AsyncGenerator[NDGRComment, None]:
        """
        NDGR サーバーからリアルタイムコメントを随時ストリーミングする非同期ジェネレータ
        このメソッドは例外発生時か放送終了時を除き、基本的に終了しない

        Yields:
            NDGRComment: NDGR サーバーから受信したコメント

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        # 視聴ページから NDGR View API の URI を取得する
        embedded_data = await self.parseWatchPage()
        view_api_uri = await self.getNDGRViewAPIUri(embedded_data.webSocketUrl)

        # NDGR View API への初回アクセスかどうかを表すフラグ
        is_first_time: bool = True
        # NDGR View API への次回アクセス時に ?at= に渡すタイムスタンプ (が格納された ChunkedEntry.ReadyForNext)
        ready_for_next: chat.ChunkedEntry.ReadyForNext | None = None
        # 既知のメッセージ ID を格納する集合
        known_message_ids: set[str] = set()

        while True:
            # 状態次第で NDGR View API の ?at= に渡すタイムスタンプを決定する
            at: str | None = None
            if ready_for_next is not None:
                at = str(ready_for_next.at)
            elif is_first_time:
                at = 'now'
                is_first_time = False

            ready_for_next = None

            retry_count = 0
            while retry_count < 3:
                try:
                    async for chunked_entry in self.fetchChunkedEntries(view_api_uri, at):

                        # NDGR Segment API への接続情報を取得
                        if chunked_entry.HasField('segment'):

                            # Segment の開始時刻と終了時刻の UNIX タイムスタンプを取得
                            segment = chunked_entry.segment
                            segment_from = segment.from_.seconds + segment.from_.nanos / 1e9
                            segment_until = segment.until.seconds + segment.until.nanos / 1e9
                            print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] '
                                  f'Segment From: {datetime.fromtimestamp(segment_from).strftime("%H:%M:%S")} / '
                                  f'Segment Until: {datetime.fromtimestamp(segment_until).strftime("%H:%M:%S")}')
                            print(Rule(characters='-', style=Style(color='#E33157')))

                            # NDGR Segment API から 16 秒間メッセージをリアルタイムストリーミングし、随時ジェネレータに返す
                            async for comment in self.fetchChunkedMessages(segment.uri):
                                if comment.id not in known_message_ids:
                                    known_message_ids.add(comment.id)
                                    yield comment

                        # 次回の NDGR View API アクセス用タイムスタンプを取得
                        elif chunked_entry.HasField('next'):
                            ready_for_next = chunked_entry.next

                    break  # 成功したらループを抜ける

                except Exception:
                    if self.show_log:
                        print(f'Error during fetch:')
                        print(traceback.format_exc())
                        print(Rule(characters='-', style=Style(color='#E33157')))
                    retry_count += 1
                    if retry_count >= 3:
                        raise  # 3回リトライしても失敗したら例外を投げる
                    await asyncio.sleep(1)  # 1秒待ってリトライ

            if ready_for_next is None:
                # next が設定されていない場合は放送が終了したとみなす
                break


    async def downloadBackwardComments(self) -> list[NDGRComment]:
        """
        NDGR サーバーから過去に投稿されたコメントを遡ってダウンロードする

        Returns:
            list[NDGRComment]: 過去に投稿されたコメントのリスト (時系列昇順)

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        # 視聴ページから NDGR View API の URI を取得する
        embedded_data = await self.parseWatchPage()
        view_api_uri = await self.getNDGRViewAPIUri(embedded_data.webSocketUrl)

        # NDGR View API への初回アクセスかどうかを表すフラグ
        is_first_time: bool = True
        # NDGR View API への次回アクセス時に ?at= に渡すタイムスタンプ (が格納された ChunkedEntry.ReadyForNext)
        ready_for_next: chat.ChunkedEntry.ReadyForNext | None = None
        # 過去のコメントを取得するための NDGR Backward API の URI
        backward_api_uri: str | None = None
        # コメントリスト
        comments: list[NDGRComment] = []

        # NDGR View API の持続期間は一定期間ごとに区切られているらしく、
        # 一定期間が経過すると next フィールドに設定されている次の NDGR View API への再接続を求められる
        while True:

            # 状態次第で NDGR View API の ?at= に渡すタイムスタンプを決定する
            # 初回アクセス時は ?at=now を指定する
            # 次回アクセス時は ?at= に ChunkedEntry.ReadyForNext.at に設定されている Unix タイムスタンプを指定する
            at: str | None = None
            if ready_for_next is not None:
                at = str(ready_for_next.at)
            elif is_first_time:
                at = 'now'
                is_first_time = False

            ready_for_next = None
            backward_api_uri = None

            # イベントを作成して、backward_api_uri が見つかったら設定する
            backward_api_uri_found = asyncio.Event()

            async def process_chunked_entries():
                """
                ChunkedEntry の受信を処理する非同期ジェネレーター関数
                ChunkedEntry には、NDGR Segment API / NDGR Backward API など複数の API のアクセス先 URI が含まれる
                """

                nonlocal ready_for_next, backward_api_uri
                async for chunked_entry in self.fetchProtobufStream(f'{view_api_uri}?at={at}', chat.ChunkedEntry):

                    # next フィールドがが設定されているとき、NDGR View API への次回アクセス時に ?at= に指定するタイムスタンプ
                    # (が格納された ChunkedEntry.ReadyForNext) を更新する
                    if chunked_entry.HasField('next'):
                        assert ready_for_next is None, 'Duplicated ReadyForNext'
                        ready_for_next = chunked_entry.next

                    # backward フィールドがが設定されているとき、BackwardSegment.segment.uri から NDGR Backward API の URI を取得する
                    elif chunked_entry.HasField('backward'):
                        backward_api_uri = chunked_entry.backward.segment.uri
                        backward_api_uri_found.set()  # イベントを設定して、ループを終了させる
                        break

            # NDGR View API から ChunkedEntry の受信を開始する非同期タスクを作成
            read_task = asyncio.create_task(process_chunked_entries())

            # backward_api_uri が見つかるか、self.readProtobufStream() が完了するまで待機
            _, pending = await asyncio.wait(
                [read_task, asyncio.create_task(backward_api_uri_found.wait())],
                return_when = asyncio.FIRST_COMPLETED,
            )

            # 完了していないタスクをキャンセル
            for task in pending:
                task.cancel()

            # backward_api_uri が取得できたらループを抜ける
            if backward_api_uri is not None:
                break

        # backward_api_uri が取得できなかった場合は空のリストを返す
        if not backward_api_uri:
            return []

        # NDGR Backward API から過去のコメントを PackedSegment 型で取得
        while True:
            if self.show_log:
                print(f'Retrieving {backward_api_uri} ...')
                print(Rule(characters='-', style=Style(color='#E33157')))
            response = await self.httpx_client.get(backward_api_uri)
            response.raise_for_status()
            packed_segment = chat.PackedSegment()
            packed_segment.ParseFromString(response.content)

            # PackedSegment.messages には複数の ChunkedMessage が格納されている
            ## この ChunkedMessage は取得時点でコメント投稿時刻昇順でソートされている
            ## このメソッドでもレスポンスはコメント投稿時刻昇順で返したいので、comments への追加方法を工夫している
            temp_comments: list[NDGRComment] = []
            for chunked_message in packed_segment.messages:

                # meta または message が存在しない場合は空の ChunkedMessage なので無視する
                if not chunked_message.HasField('meta') or not chunked_message.HasField('message'):
                    continue
                # NicoLiveMessage の中に chat がない場合は運営コメントや市場などコメント以外のメッセージなので無視する
                if not chunked_message.message.HasField('chat'):
                    continue
                # Chat の中に Modifier がない場合 (存在するのか？) はコメントの位置や色などの情報が取れないのでとりあえず無視する
                if not chunked_message.message.chat.HasField('modifier'):
                    continue

                # 取り回しやすいように NDGRComment Pydantic モデルに変換
                comment = self.convertToNDGRComment(chunked_message)
                temp_comments.append(comment)
                if self.show_log:
                    print(str(comment))
                    print(Rule(characters='-', style=Style(color='#E33157')))

            # 現在の comments の前側に temp_comments の内容を連結
            comments = temp_comments + comments

            # next フィールドが設定されていれば、続けて過去のコメントを取得
            if packed_segment.HasField('next'):
                # NDGR Backward API の URI を次のコメント取得用に更新
                backward_api_uri = packed_segment.next.uri
            else:
                break

            # 短時間に大量アクセスすると 403 を返されるので、1秒待つ
            await asyncio.sleep(1.0)

        return comments


    async def parseWatchPage(self) -> NicoLiveProgramInfo:
        """
        視聴ページを解析し、埋め込みデータを取得する

        Returns:
            NicoLiveProgramInfo: 解析された埋め込みデータ

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        watch_page_url = f'https://live.nicovideo.jp/watch/{self.nicolive_program_id}'
        response = await self.httpx_client.get(watch_page_url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        embedded_data_elm = soup.find(id='embedded-data')
        assert isinstance(embedded_data_elm, Tag)
        props = embedded_data_elm.get('data-props')
        assert isinstance(props, str)
        embedded_data = json.loads(props)
        assert isinstance(embedded_data, dict)
        assert 'program' in embedded_data
        assert 'site' in embedded_data
        assert 'relive' in embedded_data['site']

        program_info = NicoLiveProgramInfo(
            title = embedded_data['program']['title'],
            description = embedded_data['program']['description'],
            status = embedded_data['program']['status'],
            releaseTime = embedded_data['program']['releaseTime'],
            openTime = embedded_data['program']['openTime'],
            beginTime = embedded_data['program']['beginTime'],
            vposBaseTime = embedded_data['program']['vposBaseTime'],
            endTime = embedded_data['program']['endTime'],
            scheduledEndTime = embedded_data['program']['scheduledEndTime'],
            webSocketUrl = embedded_data['site']['relive']['webSocketUrl'],
        )
        if self.show_log:
            print(f'Title:  {program_info.title} [{program_info.status}]')
            print(f'Period: {datetime.fromtimestamp(program_info.openTime).strftime("%Y-%m-%d %H:%M:%S")} ~ '
              f'{datetime.fromtimestamp(program_info.scheduledEndTime).strftime("%Y-%m-%d %H:%M:%S")} '
              f'({datetime.fromtimestamp(program_info.scheduledEndTime) - datetime.fromtimestamp(program_info.openTime)}h)')
            print(Rule(characters='-', style=Style(color='#E33157')))

        return program_info


    async def getNDGRViewAPIUri(self, webSocketUrl: str) -> str:
        """
        ニコニコ生放送の視聴ページから取得した webSocketUrl に接続し、NDGR View API の URI を取得する
        Protobuf ストリームが返ることからして、NDGR サーバーは大災害前のニコ生の WebSocket API とは仕様が大きく異なる
        この API を叩くことで NDGR サーバー内部でどこまでリソース確保が行われているのかはよくわからない…
        (レスポンスヘッダーを見る限り CloudFront のキャッシュがヒットしてそうなので、多くの同時接続を捌けるようキャッシュされている？)

        Args:
            webSocketUrl (str): 視聴ページから取得した webSocketUrl

        Returns:
            str: 当該番組に対応する NDGR View API の URI

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        # ニコニコ生放送の視聴ページから取得した webSocketUrl に接続
        async with websockets.connect(webSocketUrl) as websocket:

            # 接続が確立したら、視聴開始リクエストを送る
            await websocket.send(json.dumps({
                'type': 'startWatching',
                'data': {
                    'reconnect': False,
                },
            }))

            # メッセージを受信し、NDGR View API の URI を取得する
            while True:
                message = await websocket.recv()
                data = json.loads(message)

                # NDGR View API の URI を伝えるメッセージ
                if data['type'] == 'messageServer':
                    """
                    "messageServer" メッセージのデータ構造は下記の通り
                    {
                        "type": "messageServer",
                        "data": {
                            "viewUri": "https://mpn.live.nicovideo.jp/api/view/v4/BBzJJgRIsDCeD3aiMwvVzh4SZ74yuKz5RRtRX9usHCw9075mEEV-GYMO8d0_RScEN9vzJ4zBMbHXSVY",
                            "vposBaseTime": "2024-08-05T15:00:00+09:00",
                            "hashedUserId": "a:XXXXXXXXXXXXXXX"  # ログイン時のみ設定される
                        }
                    }
                    """

                    # NDGR View API の URI を取得
                    view_uri = data['data']['viewUri']
                    if self.show_log:
                        print(f'NDGR View API URI: {view_uri}')
                        print(Rule(characters='-', style=Style(color='#E33157')))

                    # WebSocket接続を閉じて NDGR View API の URI を返す
                    await websocket.close()
                    return view_uri


    async def fetchChunkedEntries(self, view_api_uri: str, at: str | None) -> AsyncGenerator[chat.ChunkedEntry, None]:
        """
        NDGR View API から ChunkedEntry を受信する

        Args:
            view_api_uri (str): NDGR View API の URI
            at (str | None): NDGR View API へのアクセス時に ?at= に指定するタイムスタンプ

        Yields:
            chunked_entry (chat.ChunkedEntry): NDGR View API から受信した ChunkedEntry
        """

        url = f'{view_api_uri}?at={at}' if at else view_api_uri
        async for chunked_entry in self.fetchProtobufStream(url, chat.ChunkedEntry):
            yield chunked_entry


    async def fetchChunkedMessages(self, segment_uri: str) -> AsyncGenerator[NDGRComment, None]:
        """
        NDGR Segment API から ChunkedMessage を取得し、NDGRComment に変換して返す
        NDGRComment に変換できない ChunkedMessage は無視される

        Args:
            segment_uri (str): NDGR Segment API の URI

        Yields:
            NDGRComment: 変換された NDGRComment
        """

        async for chunked_message in self.fetchProtobufStream(segment_uri, chat.ChunkedMessage):
            # meta または message が存在しない場合は空の ChunkedMessage なので無視する
            if not chunked_message.HasField('meta') or not chunked_message.HasField('message'):
                continue
            # NicoLiveMessage の中に chat がない場合は運営コメントや市場などコメント以外のメッセージなので無視する
            if not chunked_message.message.HasField('chat'):
                continue
            # Chat の中に Modifier がない場合 (存在するのか？) はコメントの位置や色などの情報が取れないのでとりあえず無視する
            if not chunked_message.message.chat.HasField('modifier'):
                continue
            yield self.convertToNDGRComment(chunked_message)


    ProtobufType = TypeVar('ProtobufType', chat.ChunkedEntry, chat.ChunkedMessage, chat.PackedSegment)
    async def fetchProtobufStream(self, uri: str, protobuf_class: Type[ProtobufType]) -> AsyncGenerator[ProtobufType, None]:
        """
        Protobuf ストリームを読み込み、読み取った Protobuf チャンクをジェネレーターで返す
        Protobuf ストリームを最後まで読み切ったら None を返す
        エラー発生時は 5 回までリトライしてから例外を送出する

        Args:
            uri (str): 読み込む Protobuf ストリームの URI
            protobuf_class (Type[ProtobufType]): 読み込む Protobuf の型

        Yields:
            ProtobufType: Protobuf チャンク (protobuf_class で指定した型)
        """

        if self.show_log:
            api_name = ''
            if '/view/' in uri:
                api_name = 'NDGR View API'
            elif '/segment/' in uri:
                api_name = 'NDGR Segment API'
            print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] Fetching {api_name} ...')
            print(uri)
            print(Rule(characters='-', style=Style(color='#E33157')))

        max_retries = 5  # 5回までリトライ
        retry_delay = 3  # 3秒待ってリトライ

        for attempt in range(max_retries):
            try:
                protobuf_reader = ProtobufStreamReader()

                # Protobuf ストリームを取得
                # HTTP エラー発生時は例外を送出してリトライさせる
                async with self.httpx_client.stream('GET', uri, timeout=httpx.Timeout(5.0, read=None)) as response:
                    response.raise_for_status()

                    # Protobuf チャンクを読み取る
                    async for chunk in response.aiter_bytes():
                        protobuf_reader.addNewChunk(chunk)
                        while True:
                            message = protobuf_reader.unshiftChunk()
                            if message is None:
                                break
                            protobuf = protobuf_class()
                            protobuf.ParseFromString(message)

                            # ジェネレーターとして読み取った Protobuf を返す
                            yield protobuf

                # Protobuf ストリームを最後まで読み切ったら、ループを抜ける
                break

            # HTTP 接続エラー発生時、しばらく待ってからリトライを試みる
            except (httpx.HTTPError, httpx.StreamError) as e:
                if attempt < max_retries - 1:
                    if self.show_log:
                        print(f'HTTP error occurred: {e}. Retrying in {retry_delay} seconds...')
                    await asyncio.sleep(retry_delay)

                # 最後の試行でも失敗した場合、例外を再発生させる
                else:
                    if self.show_log:
                        print(f'Max retries reached. Error: {e}')
                    raise


    @staticmethod
    def convertToNDGRComment(chunked_message: chat.ChunkedMessage) -> NDGRComment:
        """
        ChunkedMessage を取り回しやすいように NDGRComment Pydantic モデルに変換する

        Args:
            chunked_message (chat.ChunkedMessage): ChunkedMessage

        Returns:
            NDGRComment: NDGRComment
        """

        assert chunked_message.HasField('message')
        assert chunked_message.message.HasField('chat')
        assert chunked_message.message.chat.HasField('modifier')

        # 色は named_color または full_color のどちらかで指定されている
        # 万が一どちらも指定されている場合は、full_color を優先する
        color = 'white'
        if chunked_message.message.chat.modifier.HasField('full_color'):
            color = NDGRCommentFullColor(
                r = chunked_message.message.chat.modifier.full_color.r,
                g = chunked_message.message.chat.modifier.full_color.g,
                b = chunked_message.message.chat.modifier.full_color.b,
            )
        elif chunked_message.message.chat.modifier.HasField('named_color'):
            color = cast(Any, atoms.Chat.Modifier.ColorName.Name(chunked_message.message.chat.modifier.named_color).lower())

        comment = NDGRComment(
            id = chunked_message.meta.id,
            at = datetime.fromtimestamp(float(f'{chunked_message.meta.at.seconds}.{chunked_message.meta.at.nanos}')),
            live_id = chunked_message.meta.origin.chat.live_id,
            raw_user_id = chunked_message.message.chat.raw_user_id,
            hashed_user_id = chunked_message.message.chat.hashed_user_id,
            account_status = cast(Any, atoms.Chat.AccountStatus.Name(chunked_message.message.chat.account_status)),
            no = chunked_message.message.chat.no,
            vpos = chunked_message.message.chat.vpos,
            position = cast(Any, atoms.Chat.Modifier.Pos.Name(chunked_message.message.chat.modifier.position).lower()),
            size = cast(Any, atoms.Chat.Modifier.Size.Name(chunked_message.message.chat.modifier.size).lower()),
            color = color,
            font = cast(Any, atoms.Chat.Modifier.Font.Name(chunked_message.message.chat.modifier.font).lower()),
            opacity = cast(Any, atoms.Chat.Modifier.Opacity.Name(chunked_message.message.chat.modifier.opacity)),
            content = chunked_message.message.chat.content,
        )

        return comment


    @staticmethod
    def convertToXMLCompatibleComment(comment: NDGRComment) -> XMLCompatibleComment:
        """
        NDGRComment を XML 互換コメントデータに変換する

        Args:
            comment (NDGRComment): NDGRComment

        Returns:
            XMLCompatibleComment: XMLCompatibleComment
        """

        # "mail" フィールドに入るコメントコマンドを生成
        command = []
        # raw_user_id が 0 の場合はユーザー ID が匿名化されているため、"184" コマンドを付与する
        if comment.raw_user_id == 0:
            command.append('184')
        if comment.position != 'naka':
            command.append(comment.position)
        if comment.size != 'medium':
            command.append(comment.size)
        if isinstance(comment.color, str) and comment.color != 'white':
            command.append(comment.color)
        elif isinstance(comment.color, NDGRCommentFullColor):
            command.append(f'#{comment.color.r:02x}{comment.color.g:02x}{comment.color.b:02x}')
        if comment.font != 'defont':
            command.append(comment.font)
        # コメントの不透明度を表すコメントコマンドは従来存在しないが、
        ## コメントの装飾情報は原則保存しておきたいので、特別に opacity が "Translucent" の場合のみ "translucent" コマンドを付与する
        if comment.opacity == 'Translucent':
            command.append('translucent')

        # raw_user_id が 0 より上だったら生のユーザー ID を採用し、なければ hashed_user_id (匿名化されたユーザー ID) を採用
        ## ユーザー ID にはニコニコ生放送からのコメントだと識別できる "nicolive:" の prefix を付与
        if comment.raw_user_id > 0:
            user_id = f'nicolive:{comment.raw_user_id}'
        else:
            user_id = f'nicolive:{comment.hashed_user_id}'

        # XMLCompatibleComment オブジェクトを生成
        xml_compatible_comment = XMLCompatibleComment(
            thread = str(comment.live_id),
            no = comment.no,
            vpos = comment.vpos,
            date = int(comment.at.timestamp()),
            date_usec = int((comment.at.timestamp() % 1) * 1000000),
            user_id = user_id,
            mail = ' '.join(command),
            premium = 1 if comment.account_status == 'Premium' else None,
            # raw_user_id が 0 の場合は anonymity フィールドに 1 を設定している
            anonymity = 1 if comment.raw_user_id == 0 else None,
            content = comment.content,
        )

        return xml_compatible_comment


    @staticmethod
    def convertToXMLString(comments: list[NDGRComment]) -> str:
        """
        NDGRComment のリストをヘッダーなし XML 文字列 (.nicojk) に変換する

        Args:
            comments (list[NDGRComment]): NDGRComment のリスト

        Returns:
            str: XML 文字列
        """

        def sanitize_for_xml(text: str) -> str:
            # XML と互換性のない制御文字を除去
            # 有効な XML 制御文字 (タブ、改行、復帰) は保持
            return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

        # XML のエレメントツリー
        elem_tree = ET.Element('packet')

        # コメント投稿時刻昇順でソート
        comments.sort(key=lambda x: x.at)

        # コメントごとに
        for comment in comments:

            # コメントを XMLCompatibleComment に変換したあと、さらに辞書に変換
            comment = NDGRClient.convertToXMLCompatibleComment(comment)
            comment_dict = comment.model_dump()

            # コメント本文を取得して消す（ XML ではタグ内の値として入るため）
            chat_content = comment_dict['content']
            del comment_dict['content']

            # 属性を XML エレメントに追加
            sanitized_attrs = {key: sanitize_for_xml(str(value)) for key, value in comment_dict.items() if value is not None}
            chat_elem_tree = ET.SubElement(elem_tree, 'chat', sanitized_attrs)

            # XML エレメント内の値に以前取得した本文を指定
            ## 制御文字が入ってると ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters と
            ## lxml からエラーを吐かれるので sanitize してから設定している
            chat_elem_tree.text = sanitize_for_xml(chat_content)

        # 素の XML を .nicojk 形式向けにフォーマットする
        # lxml.etree を使うことで属性の順序を保持できる
        # ref: https://banatech.net/blog/view/19
        xml_string = ET.tostring(elem_tree, encoding='utf-8', pretty_print=True).decode('utf-8').replace('>\n  ', '>\n')  # インデントを除去
        xml_string = xml_string.replace('<packet>\n', '').replace('</packet>', '').replace('<packet/>', '')  # <packet> タグを除去
        return xml_string.rstrip()
