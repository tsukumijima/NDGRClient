
import json
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import Any



class WatchPageEmbeddedData(BaseModel):
    temporary_measure: dict[str, Any] = Field(alias='temporaryMeasure')
    ndgr_program_comment_view_uri: str = Field(..., alias='ndgrProgramCommentViewUri')


class NDGRClient:

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
            ## 可能な限り Chrome からのリクエストに偽装するため、HTTP/1.1 ではなく明示的に HTTP/2 で接続する
            http2 = True,
        )


    async def parseWatchPage(self) -> WatchPageEmbeddedData:
        """
        視聴ページを解析し、埋め込みデータを取得します。

        Returns:
            WatchPageEmbeddedData: 解析された埋め込みデータ

        Raises:
            ValueError: 無効な live_id が指定された場合
            httpx.HTTPStatusError: HTTPリクエストが失敗した場合
            ValueError: 埋め込みデータの解析に失敗した場合
        """

        watch_page_url = f'https://live.nicovideo.jp/rekari/{self.rekari_id}'
        response = await self.httpx_client.get(watch_page_url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        embedded_data_elm = soup.find(id='embedded-data')
        if embedded_data_elm is None:
            raise ValueError('視聴ページから embedded-data が見つかりませんでした')

        props = embedded_data_elm.get('data-props')
        if props is None:
            raise ValueError('embedded-data から props が見つかりませんでした')

        embedded_data = json.loads(props)
        return WatchPageEmbeddedData(**embedded_data['temporaryMeasure'])

