
from __future__ import annotations

import json
import httpx
from bs4 import BeautifulSoup, Tag
from rich import print
from rich.rule import Rule
from rich.style import Style
from typing import Awaitable, Callable, Type, TypeVar

from ndgr_client.protobuf_stream_reader import ProtobufStreamReader
from ndgr_client.proto.dwango.nicolive.chat.service.edge import payload_pb2 as chat
from ndgr_client.schemas import (
    NDGRComment,
    NicoLiveProgramInfo,
)


class NDGRClient:
    """
    NDGR サーバーのクライアント実装
    下記実装を大変参考にした
    ref: https://github.com/rinsuki-lab/ndgr-reader
    """

    # User-Agent と Sec-CH-UA を Chrome 126 に偽装
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    SEC_CH_UA = '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"'

    # 旧来の実況 ID とニコニコ生放送 (Re:仮) の ID のマッピング
    JIKKYO_ID_TO_REKARI_ID_MAP: dict[str, str] = {
        'jk0': 'kl1',  # サイバー攻撃からのニコニコ復旧を見守る場所 (ニコニコ実況ではないがデバッグ用として)
        'jk1': 'kl11',
        'jk2': 'kl12',
        'jk4': 'kl14',
        'jk5': 'kl15',
        'jk6': 'kl16',
        'jk7': 'kl17',
        'jk8': 'kl18',
        'jk9': 'kl19',
        'jk101': 'kl13',
        'jk211': 'kl20',
    }


    def __init__(self, jikkyo_id: str, show_log: bool = False) -> None:
        """
        NDGRClient のコンストラクタ

        Args:
            jikkyo_id (str): 旧来の実況 ID
            show_log (bool, default=False): ログを表示するかどうか
        """

        self.jikkyo_id = jikkyo_id
        self.rekari_id = self.JIKKYO_ID_TO_REKARI_ID_MAP[jikkyo_id]
        self.show_log = show_log

        # httpx の非同期 HTTP クライアントのインスタンスを作成
        self.httpx_client = httpx.AsyncClient(
            ## リダイレクトを追跡する
            follow_redirects = True,
        )


    async def parseWatchPage(self) -> NicoLiveProgramInfo:
        """
        視聴ページを解析し、埋め込みデータを取得する
        大災害前のニコ生と異なり、ページをロードしただけではニコ生側の視聴セッションは初期化されない

        Returns:
            WatchPageTemporaryMeasure: 解析された埋め込みデータ

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        watch_page_url = f'https://live.nicovideo.jp/rekari/{self.rekari_id}'
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
        assert 'temporaryMeasure' in embedded_data

        return NicoLiveProgramInfo(
            title = embedded_data['program']['title'],
            description = embedded_data['program']['description'],
            status = embedded_data['program']['status'],
            releaseTime = embedded_data['program']['releaseTime'],
            openTime = embedded_data['program']['openTime'],
            beginTime = embedded_data['program']['beginTime'],
            vposBaseTime = embedded_data['program']['vposBaseTime'],
            endTime = embedded_data['program']['endTime'],
            scheduledEndTime = embedded_data['program']['scheduledEndTime'],
            streamContentUri = embedded_data['temporaryMeasure']['streamContentUri'],
            ndgrProgramCommentViewUri = embedded_data['temporaryMeasure']['ndgrProgramCommentViewUri'],
            ndgrProgramCommentPostUri = embedded_data['temporaryMeasure']['ndgrProgramCommentPostUri'],
        )


    async def streamComments(self, callback: Callable[[NDGRComment], None | Awaitable[None]]) -> None:
        """
        NDGR サーバーからリアルタイムコメントを随時ストリーミングし、コールバックに渡す

        Args:
            callback (Callable[[NDGRComment], Awaitable[None]]): NDGR サーバーから受信したコメントを受け取るコールバック関数

        Raises:
            AssertionError: 解析に失敗した場合
        """

        embedded_data = await self.parseWatchPage()
        view_uri = await self.acquireViewUri(embedded_data.ndgrProgramCommentViewUri)

        # 下記コードでは、NDGR サーバーの各 API を便宜的に下記の通り呼称する
        # ・https://mpn.live.nicovideo.jp/api/view/v4/...: NDGR View API
        # ・https://mpn.live.nicovideo.jp/data/segment/v4/...: NDGR Segment API

        # NDGR View API への初回アクセスかどうかを表すフラグ
        is_first_time: bool = True
        # NDGR View API への次回アクセス時に ?at= に渡すタイムスタンプ (が格納された ChunkedEntry.ReadyForNext)
        ready_for_next: chat.ChunkedEntry.ReadyForNext | None = None
        # 既知の NDGR Segment API の URI を格納する集合
        already_know_segment_uris: set[str] = set()

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

            async def chunk_callback(chunked_entry: chat.ChunkedEntry) -> None:
                """
                ChunkedEntry の受信を処理するコールバック関数
                ChunkedEntry には、コメント受信用 API など複数の API の URI が含まれる
                """

                nonlocal ready_for_next

                # next フィールドがある場合は、NDGR View API への次回アクセス時に ?at= に指定するタイムスタンプ
                # (が格納された ChunkedEntry.ReadyForNext) を更新する
                if chunked_entry.HasField('next'):
                    assert ready_for_next is None, 'Duplicated ReadyForNext'
                    ready_for_next = chunked_entry.next

                # segment フィールドがある場合は、MessageSegment.uri から NDGR Segment API の URI を取得する
                elif chunked_entry.HasField('segment'):
                    segment = chunked_entry.segment

                    # URI が既知のセグメントでない場合は、その URI を already_know_segments に追加して、
                    # NDGR Segment API の受信を開始する
                    if segment.uri not in already_know_segment_uris:
                        already_know_segment_uris.add(segment.uri)

                        # ChunkedMessage の受信を処理するコールバック関数
                        async def message_callback(message: chat.ChunkedMessage) -> None:

                            # meta または message が存在しない場合は空の ChunkedMessage なので無視する
                            if not message.HasField('meta') or not message.HasField('message'):
                                return

                            # Pydantic モデルに変換してコールバック関数に返す
                            callback(NDGRComment(
                                id = message.meta.id,
                                live_id = message.meta.origin.chat.live_id,
                                content = message.message.chat.content,
                                vpos = message.message.chat.vpos,
                                hashed_user_id = message.message.chat.hashed_user_id,
                            ))

                        # NDGR Segment API から ChunkedMessage の受信を開始 (受信が完了するまで非同期にブロックする)
                        await self.readProtobufStream(segment.uri, chat.ChunkedMessage, message_callback)

            # NDGR View API から ChunkedEntry の受信を開始 (受信が完了するまで非同期にブロックする)
            await self.readProtobufStream(f'{view_uri}?at={at}', chat.ChunkedEntry, chunk_callback)


    async def acquireViewUri(self, ndgrProgramCommentViewUri: str) -> str:
        """
        視聴ページから取得した ndgrProgramCommentViewUri を使って、NDGR サーバーへの接続用 URL を確保する
        Protobuf ストリームが返ることからして、NDGR サーバーは大災害前のニコ生の WebSocket API とは仕様が大きく異なる
        この API を叩くことで NDGR サーバー内部でどこまでリソース確保が行われているのかはよくわからない…

        Args:
            ndgrProgramCommentViewUri (str): 視聴ページから取得した ndgrProgramCommentViewUri

        Returns:
            str: NDGR サーバーへの接続用 URL

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        response = await self.httpx_client.get(ndgrProgramCommentViewUri)
        response.raise_for_status()
        response_json = response.json()

        assert isinstance(response_json, dict)
        assert 'view' in response_json
        assert isinstance(response_json['view'], str)
        return response_json['view']


    ProtobufType = TypeVar('ProtobufType', chat.ChunkedEntry, chat.ChunkedMessage)
    async def readProtobufStream(
        self,
        uri: str,
        protobuf_class: Type[ProtobufType],
        chunk_callback: Callable[[ProtobufType], Awaitable[None]],
    ) -> None:
        """
        Protobuf ストリームを読み込み、チャンクごとにコールバック関数を呼び出す
        Protobuf ストリームを最後まで読み切ったら None を返す

        Args:
            uri (str): 読み込む Protobuf ストリームの URI
            protobuf_class (Type[ProtobufType]): 読み込む Protobuf の型
            chunk_callback (Callable[[ProtobufType], Awaitable[None]]): 各チャンクに対して呼び出されるコールバック関数

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
        """

        if self.show_log:
            print(f'Reading {uri} ...')
            print(Rule(characters='-', style=Style(color='#E33157')))
        protobuf_reader = ProtobufStreamReader()

        async with self.httpx_client.stream('GET', uri, timeout=httpx.Timeout(5.0, read=None)) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                protobuf_reader.addNewChunk(chunk)
                while True:
                    message = protobuf_reader.unshiftChunk()
                    if message is None:
                        break
                    protobuf = protobuf_class()
                    protobuf.ParseFromString(message)
                    await chunk_callback(protobuf)
