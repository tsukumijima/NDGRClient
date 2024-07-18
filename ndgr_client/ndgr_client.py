
from __future__ import annotations

import json
import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel
from typing import Awaitable, Callable, Type, TypeVar

from ndgr_client.protobuf_stream_reader import ProtobufStreamReader
from ndgr_client.proto.dwango.nicolive.chat.service.edge import payload_pb2 as chat


class WatchPageTemporaryMeasure(BaseModel):
    streamContentUri: str | None
    ndgrProgramCommentViewUri: str
    ndgrProgramCommentPostUri: str


class NDGRClient:
    """
    NDGR コメントサーバーのクライアント実装
    """

    # User-Agent と Sec-CH-UA を Chrome 126 に偽装
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    SEC_CH_UA = '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"'

    # 旧来の実況 ID とニコニコ生放送 (Re:仮) の ID のマッピング
    JIKKYO_ID_TO_REKARI_ID_MAP: dict[str, str] = {
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


    def __init__(self, jikkyo_id: str) -> None:
        """
        NDGRClient のコンストラクタ

        Args:
            jikkyo_id (str): 旧来の実況 ID
        """

        self.jikkyo_id = jikkyo_id
        self.rekari_id = self.JIKKYO_ID_TO_REKARI_ID_MAP[jikkyo_id]

        # httpx の非同期 HTTP クライアントのインスタンスを作成
        self.httpx_client = httpx.AsyncClient(
            ## リダイレクトを追跡する
            follow_redirects = True,
        )


    async def parseWatchPage(self) -> WatchPageTemporaryMeasure:
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

        return WatchPageTemporaryMeasure(**embedded_data['temporaryMeasure'])


    async def acquireViewUri(self, ndgrProgramCommentViewUri: str) -> str:
        """
        視聴ページから取得した ndgrProgramCommentViewUri を使って、視聴セッションを確保する
        実際にどこまでセッションになっているのかはよくわからないが、ともかくランダムな ID が付与された
        Protobuf 形式のレスポンスを返す URL が返る

        Args:
            ndgrProgramCommentViewUri (str): 視聴ページから取得した ndgrProgramCommentViewUri

        Returns:
            str: NDGR コメントサーバーへの接続用 URL

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
        url: str,
        protobuf_class: Type[ProtobufType],
        chunk_callback: Callable[[ProtobufType], Awaitable[None]],
    ) -> None:
        """
        Protobuf ストリームを読み込み、チャンクごとにコールバック関数を呼び出す

        Args:
            url (str): 読み込む Protobuf ストリームの URL
            protobuf_class (Type[ProtobufType]): 読み込む Protobuf の型
            chunk_callback (Callable[[ProtobufType], Awaitable[None]]): 各チャンクに対して呼び出されるコールバック関数

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
        """

        protobuf_reader = ProtobufStreamReader()

        async with self.httpx_client.stream('GET', url, timeout=httpx.Timeout(5.0, read=None)) as response:
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
