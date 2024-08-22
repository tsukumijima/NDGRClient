
from __future__ import annotations

import asyncio
import json
import httpx
import lxml.etree as ET
import re
import traceback
import websockets
from bs4 import BeautifulSoup, Tag
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from rich import print
from rich.rule import Rule
from rich.style import Style
from typing import Any, AsyncGenerator, cast, Literal, Type, TypedDict, TypeVar

from ndgr_client import __version__
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
    NDGR メッセージサーバーのクライアント実装
    実装にあたり、下記リポジトリが大変参考になった
    ref: https://github.com/rinsuki-lab/ndgr-reader

    下記コードでは、便宜的に NDGR メッセージサーバーの各 API を下記のように呼称する
    ・NDGR View API : https://mpn.live.nicovideo.jp/api/view/v4/...
    ・NDGR Segment API : https://mpn.live.nicovideo.jp/data/segment/v4/...
    ・NDGR Backward API : https://mpn.live.nicovideo.jp/data/backward/v4/...
    """

    # HTTP ヘッダー を Chrome 126 に偽装
    HTTP_HEADERS = {
        'accept': '*/*',
        'accept-encoding': 'gzip, deflate, br',
        'accept-language': 'ja',
        'origin': 'https://live.nicovideo.jp',
        'referer': 'https://live.nicovideo.jp/',
        'sec-ch-ua': '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': f'Mozilla/5.0 (Windows NT 15.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NDGRClient/{__version__}',
    }

    # 旧来の実況チャンネル ID とニコニコチャンネル ID のマッピング
    JIKKYO_CHANNEL_ID_MAP: dict[str, str] = {
        'jk1': 'ch2646436',
        'jk2': 'ch2646437',
        'jk4': 'ch2646438',
        'jk5': 'ch2646439',
        'jk6': 'ch2646440',
        'jk7': 'ch2646441',
        'jk8': 'ch2646442',
        'jk9': 'ch2646485',
        'jk101': 'ch2647992',
        'jk211': 'ch2646846',
    }


    def __init__(self, nicolive_program_id: str, verbose: bool = False, console_output: bool = False, log_path: Path | None = None) -> None:
        """
        NDGRClient のコンストラクタ
        nicolive_program_id にニコニコ実況のチャンネル ID を渡したときは、
        当該ニコニコ実況チャンネルで現在放送中のニコニコ生放送番組 (実況枠) に対して処理を行う

        Args:
            nicolive_program_id (str): ニコニコ生放送の番組 ID (ex: lv345479988) or ニコニコ実況のチャンネル ID (ex: jk1, jk211)
            verbose (bool, default=False): 詳細な動作ログを出力するかどうか
            console_output (bool, default=False): 動作ログをコンソールに出力するかどうか
            log_path (Path | None, default=None): 動作ログをファイルに出力する場合のパス (show_log と併用可能)
        """

        if nicolive_program_id.startswith('jk'):
            # nicolive_program_id が jk から始まる場合、ニコニコ実況 ID として扱う
            ## ニコニコチャンネル ID とニコニコ生放送番組 ID は異なる概念だが、ニコニコ生放送では /watch/(ニコニコチャンネル ID) の URL で
            ## 当該チャンネルで現在放送中の番組にアクセスできる仕様があるので、それを使っている
            if nicolive_program_id not in self.JIKKYO_CHANNEL_ID_MAP:
                raise ValueError(f'Invalid jikkyo_channel_id: {nicolive_program_id}')
            self.nicolive_program_id = self.JIKKYO_CHANNEL_ID_MAP[nicolive_program_id]
            self.jikkyo_channel_id = nicolive_program_id
        else:
            # それ以外の場合は lv から始まる通常のニコニコ生放送番組 ID として扱う
            if not nicolive_program_id.startswith('lv'):
                raise ValueError(f'Invalid nicolive_program_id: {nicolive_program_id}')
            self.nicolive_program_id = nicolive_program_id
            self.jikkyo_channel_id = None

        self.verbose = verbose
        self.show_log = console_output
        self.log_path = log_path

        # httpx の非同期 HTTP クライアントのインスタンスを作成
        self.httpx_client = httpx.AsyncClient(headers=self.HTTP_HEADERS, follow_redirects=True)


    @property
    def is_logged_in(self) -> bool:
        """
        ニコニコアカウントがログイン済みかどうかを返す
        """
        return 'user_session' in self.httpx_client.cookies


    async def login(self, mail: str | None = None, password: str | None = None, cookies: dict[str, str] | None = None) -> dict[str, str] | None:
        """
        ニコニコアカウントにログインするか、既存の Cookie を HTTP クライアントに設定する
        基本初回ログイン時以外は一度取得した Cookie を使い回して無駄なログインセッションが作成されるのを防ぐべき
        ログインが成功すると Cookie 辞書を返す

        Args:
            mail (str | None): ニコニコアカウントのメールアドレス
            password (str | None): ニコニコアカウントのパスワード
            cookies (dict[str, str] | None): 既存の Cookie 辞書

        Returns:
            dict[str, str] | None: 現在 HTTP クライアントにセットされている Cookie 辞書 (ログインが失敗した場合は None を返す)

        Raises:
            ValueError: mail と password の両方、または cookies のいずれかが指定されていない場合
            httpx.HTTPStatusError: ログインリクエストが失敗した場合
        """

        if (mail is None or password is None) and cookies is None:
            raise ValueError('Either both mail and password, or cookies must be provided.')

        # Cookie 辞書が指定されたとき、HTTP クライアントに Cookie を設定
        if cookies is not None:
            self.httpx_client.cookies.update(cookies)

            # https://account.nicovideo.jp/login にアクセスして x-niconico-id ヘッダーがセットされているか確認
            response = await self.httpx_client.get('https://account.nicovideo.jp/login', timeout=15.0)
            response.raise_for_status()
            if 'x-niconico-id' not in response.headers:
                return None

        # メールアドレスとパスワードが指定されたとき、ログイン処理を実行
        else:
            try:
                # この API にアクセスすると Cookie (user_session) が HTTP クライアントにセットされる
                response = await self.httpx_client.post('https://account.nicovideo.jp/api/v1/login', data={
                    'mail': mail,
                    'password': password,
                }, timeout=15.0)
                response.raise_for_status()
                # x-niconico-id ヘッダーがセットされていない場合はログインに失敗している
                if 'x-niconico-id' not in response.headers:
                    return None
                self.print(f'Login successful. Niconico User ID: {response.headers["x-niconico-id"]}', verbose_log=True)
                self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)
            except httpx.HTTPStatusError:
                self.print('Error during login:')
                self.print(traceback.format_exc())
                self.print(Rule(characters='-', style=Style(color='#E33157')))
                raise

        # 現在 HTTP クライアントにセットされている Cookie を返す
        return dict(self.httpx_client.cookies.items())


    @classmethod
    async def getProgramIDsOnDate(cls, jikkyo_channel_id: str, date: date) -> list[str]:
        """
        指定した日付に少なくとも一部が放送されている/放送されたニコニコ実況番組の ID を取得する

        Args:
            jikkyo_channel_id (str): ニコニコ実況チャンネル ID
            date (date): ニコニコ実況番組を取得する日付

        Returns:
            list[str]: 指定した日付に少なくとも一部が放送されている/放送されたニコニコ実況番組の ID のリスト (放送開始日時昇順)

        Raises:
            ValueError: ニコニコ実況のチャンネル ID が指定されていない場合
            httpx.HTTPStatusError: ニコニコ API へのリクエストに失敗した場合
        """

        if jikkyo_channel_id.startswith('jk') is False:
            raise ValueError(f'Invalid jikkyo_channel_id: {jikkyo_channel_id}')

        class NicoLiveProgramBroadcastPeriod(TypedDict):
            """
            https://api.cas.nicovideo.jp/v1/services/live/programs/(lv ID) から取得できるニコニコ生放送番組の放送期間の情報
            """
            nicoliveProgramId: str
            beginAt: datetime
            endAt: datetime

        # 2024/08/22 のニコニコチャンネル復旧までの繋ぎとして運用された暫定版ニコニコ実況の既知の番組 ID マップ
        ## Wayback Machine で取得した過去の HTML から取得した ID を記載している
        ## なぜか公式には NHK BS が NHK Eテレ の次のチャンネルとして記載されていて ID もその順序なので、ここだけそれに合わせる
        ## ref: https://web.archive.org/web/20240000000000*/https://originalnews.nico/464285
        provisional_jikkyo_program_id_map = {
            'jk1'  : ['lv345479988', 'lv345500317', 'lv345514103', 'lv345514218', 'lv345514551'],
            'jk2'  : ['lv345479989', 'lv345500319', 'lv345514108', 'lv345514221', 'lv345514557'],
            'jk101': ['lv345479990', 'lv345500325', 'lv345514113', 'lv345514225', 'lv345514561'],
            'jk4'  : ['lv345479991', 'lv345500329', 'lv345514118', 'lv345514231', 'lv345514567'],
            'jk5'  : ['lv345479993', 'lv345500330', 'lv345514126', 'lv345514239', 'lv345514569'],
            'jk6'  : ['lv345479994', 'lv345500332', 'lv345514132', 'lv345514246', 'lv345514573'],
            'jk7'  : ['lv345479995', 'lv345500337', 'lv345514139', 'lv345514247', 'lv345514578'],
            'jk8'  : ['lv345479996', 'lv345500340', 'lv345514141', 'lv345514253', 'lv345514581'],
            'jk9'  : ['lv345479997', 'lv345500342', 'lv345514145', 'lv345514257', 'lv345514587'],
            'jk211': ['lv345479998', 'lv345500347', 'lv345514147', 'lv345514260', 'lv345514593'],
        }

        # クラスメソッドから self.httpx_client にはアクセスできないため、新しい httpx.AsyncClient を作成している
        async with httpx.AsyncClient(headers=cls.HTTP_HEADERS, follow_redirects=True) as client:

            # まずは候補となるニコニコ生放送番組 ID を収集
            candidate_nicolive_program_ids: set[str] = set()
            candidate_nicolive_program_ids.update(provisional_jikkyo_program_id_map.get(jikkyo_channel_id, []))
            ## 放送中番組の ID を取得
            response = await client.get(f'https://ch.nicovideo.jp/{jikkyo_channel_id}/live', timeout=15.0)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            live_now = soup.find('div', id='live_now')
            if live_now:
                live_link = live_now.find('a', href=lambda href: bool(href and href.startswith('https://live.nicovideo.jp/watch/lv')))  # type: ignore
                if live_link:
                    live_id = cast(str, cast(Tag, live_link).get('href')).split('/')[-1]
                    candidate_nicolive_program_ids.add(live_id)
            ## 過去番組の ID をスクレイピングで取得
            for page in range(1, 3):  # 1 ページ目と 2 ページ目を取得
                response = await client.get(f'https://sp.ch.nicovideo.jp/api/past_lives/?page={page}&channel_id={jikkyo_channel_id}', timeout=15.0)
                if response.status_code != 200:
                    if page == 1:
                        # 1 ページは必ず取得できるはずなので、取得できなかった場合はニコ生側で何らかの問題が発生している
                        response.raise_for_status()
                    else:
                        # 2 ページ目が取得できなかった場合はページを分けるほど過去の番組情報がないと考えられるため、ループを抜ける
                        break
                soup = BeautifulSoup(response.content, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    href = a_tag['href']
                    if 'https://live.nicovideo.jp/watch/' in href:
                        candidate_nicolive_program_ids.add(href.split('/')[-1])

            # 候補となるニコニコ生放送番組の放送期間を取得
            broadcast_periods: list[NicoLiveProgramBroadcastPeriod] = []
            for program_id in candidate_nicolive_program_ids:
                response = await client.get(f'https://api.cas.nicovideo.jp/v1/services/live/programs/{program_id}', timeout=15.0)
                response.raise_for_status()
                response_json = response.json()
                assert 'data' in response_json
                assert 'onAirTime' in response_json['data']
                assert 'beginAt' in response_json['data']['onAirTime']
                assert 'endAt' in response_json['data']['onAirTime']
                assert 'timeshift' in response_json['data']
                assert 'enabled' in response_json['data']['timeshift']
                # タイムシフト非公開の番組からはコメントを取得できないのでスキップ
                if not response_json['data']['timeshift']['enabled']:
                    continue
                broadcast_periods.append({
                    'nicoliveProgramId': program_id,
                    'beginAt': datetime.fromisoformat(response_json['data']['onAirTime']['beginAt']),
                    'endAt': datetime.fromisoformat(response_json['data']['onAirTime']['endAt']),
                })

        # 指定された日付に放送されている番組をフィルタリング
        broadcast_periods = [
            period for period in broadcast_periods
            if period['beginAt'].date() <= date <= period['endAt'].date()
        ]

        # ID を放送開始日時が早い順に並べ替えてから返す
        broadcast_periods.sort(key=lambda x: x['beginAt'])
        return [period['nicoliveProgramId'] for period in broadcast_periods]


    @classmethod
    async def updateJikkyoChannelIDMap(cls) -> None:
        """
        https://originalnews.nico/464285 から最新の実況チャンネル ID とニコニコチャンネル ID のマッピングを取得し、
        クラス変数 JIKKYO_CHANNEL_ID_MAP に格納する
        ニコニコ実況がチャンネル生放送で本復旧するまでの暫定的な実装で (本復旧後に除去予定) 、NDGRClient の初期化前に実行する必要がある
        """

        # クラスメソッドから self.httpx_client にはアクセスできないため、新しい httpx.AsyncClient を作成している
        async with httpx.AsyncClient(headers=cls.HTTP_HEADERS, follow_redirects=True) as client:

            # スクレイピングを開始する前に https://jk.nicovideo.jp/ にリクエストしてのステータスコードを確認
            ## 暫定措置中は 302 リダイレクトが行われているので、302 リダイレクトが行われなくなっていたら本復旧したと判断して以降の処理を行わない
            response = await client.get('https://jk.nicovideo.jp/', follow_redirects=False, timeout=15.0)
            if response.status_code != 302:
                return  # 以降は何もしない

            response = await client.get('https://originalnews.nico/464285', timeout=15.0)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

        gallery = soup.find('div', id='gallery-1')
        if not gallery:
            raise ValueError('Gallery not found.')

        new_map = {}
        for item in cast(Tag, gallery).find_all('dl', class_='gallery-item'):
            caption = item.find('dd', class_='wp-caption-text gallery-caption')
            if not caption:
                continue

            link = caption.find('a')
            if not link:
                continue
            href = link.get('href')
            if not href:
                continue

            channel_name = link.text.replace('はこちら', '').strip()
            live_id = href.split('/')[-1]
            if channel_name == 'NHK総合':
                new_map['jk1'] = live_id
            elif channel_name == 'NHK Eテレ':
                new_map['jk2'] = live_id
            elif channel_name == '日本テレビ':
                new_map['jk4'] = live_id
            elif channel_name == 'テレビ朝日':
                new_map['jk5'] = live_id
            elif channel_name == 'TBSテレビ':
                new_map['jk6'] = live_id
            elif channel_name == 'テレビ東京':
                new_map['jk7'] = live_id
            elif channel_name == 'フジテレビ':
                new_map['jk8'] = live_id
            elif channel_name == 'TOKYO MX':
                new_map['jk9'] = live_id
            elif channel_name == 'NHK BS':
                new_map['jk101'] = live_id
            elif channel_name == 'BS11':
                new_map['jk211'] = live_id

        if len(new_map) != 10:
            raise ValueError(f'Expected 10 channels, but found {len(new_map)}.')

        cls.JIKKYO_CHANNEL_ID_MAP = new_map


    async def streamComments(self) -> AsyncGenerator[NDGRComment, None]:
        """
        NDGR メッセージサーバーからリアルタイムコメントを随時ストリーミングする非同期ジェネレータ
        このメソッドは番組が終了するか例外が発生するまで終了しない
        ニコニコ実況番組では、受信中の実況番組の終了直後に新しい番組が開始されたとき、透過的に継続してコメントを受信する

        Yields:
            NDGRComment: NDGR メッセージサーバーから受信したコメント

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
            ValueError: 既に放送を終了した番組に対してストリーミングを開始しようとした場合
        """

        async def stream_comments_inner() -> AsyncGenerator[NDGRComment | Literal['ENDED', 'RESTART'], None]:
            """
            NDGR View API から ChunkedEntry をリアルタイムストリーミングし、NDGR Segment API への接続情報を取得して ChunkedMessage 受信タスクを開始する
            この関数は番組の放送が終了するか例外が発生するまで終了しない
            """

            # 視聴ページから NDGR View API の URI を取得する
            nicolive_program_info = await self.fetchNicoLiveProgramInfo()
            if nicolive_program_info.status == 'ENDED':
                # すでに放送を終了した番組はストリーミングを開始できない
                ## 厳密には NDGR の各 API に接続することはできるが、当然新規にコメントが降ってくることはなく、過去ログ参照のみ
                raise ValueError(f'Program {self.nicolive_program_id} has already ended and cannot be streamed.')
            self.print(f'Title:  {nicolive_program_info.title} [{nicolive_program_info.status}] ({nicolive_program_info.nicoliveProgramId})')
            self.print(f'Period: {datetime.fromtimestamp(nicolive_program_info.openTime).strftime("%Y-%m-%d %H:%M:%S")} ~ '
                       f'{datetime.fromtimestamp(nicolive_program_info.endTime).strftime("%Y-%m-%d %H:%M:%S")} '
                       f'({datetime.fromtimestamp(nicolive_program_info.endTime) - datetime.fromtimestamp(nicolive_program_info.openTime)}h)')
            self.print(Rule(characters='-', style=Style(color='#E33157')))
            view_api_uri = await self.fetchNDGRViewURI(nicolive_program_info.webSocketUrl)

            # NDGR View API への初回アクセスかどうかを表すフラグ
            is_first_time: bool = True
            # NDGR View API への次回アクセス時に ?at= に渡すタイムスタンプ (が格納された ChunkedEntry.ReadyForNext)
            ready_for_next: chat.ChunkedEntry.ReadyForNext | None = None

            # fetch_chunked_message() で受信したコメントを yield で返すための Queue
            comment_queue: asyncio.Queue[NDGRComment] = asyncio.Queue()
            # アクティブな ChunkedMessage 受信タスクを格納する辞書
            active_segments: dict[str, asyncio.Task[None]] = {}

            async def fetch_program_info() -> Literal['RESTART', 'ENDED']:
                """
                毎分 05 秒に視聴ページから NicoLiveProgramInfo を取得し、状態を監視する
                この関数は番組の放送が終了するか、後続の番組に切り替えてコメント受信処理を再開する必要が出るまで終了しない

                Returns:
                    Literal['ENDED']: 番組の放送が終了した (後続の番組もない) 場合は 'ENDED' を返す
                    Literal['RESTART']: 後続の番組に切り替えてコメント受信処理を再開するために 'RESTART' を返す
                """

                # 毎分 05 秒に実行
                ## 00 秒ちょうどにアクセスするとギリギリ変更反映前のデータを取得してしまう可能性があるため、敢えて 5 秒待っている
                while True:
                    await asyncio.sleep(60 - datetime.now().second % 60 + 5)
                    try:
                        # 視聴ページから self.nicolive_program_id に対応する現在の番組ステータスを取得する
                        new_program_info = await self.fetchNicoLiveProgramInfo()

                        # 受信中番組がニコニコ実況番組ではなく、かつ番組の放送が終了した
                        if self.jikkyo_channel_id is None and new_program_info.status == 'ENDED':
                            return 'ENDED'  # 終了信号を返す

                        # 受信中番組がニコニコ実況番組のときのみ
                        elif self.jikkyo_channel_id is not None:

                            # 番組の放送が終了した場合、ニコニコ実況チャンネル ID とニコニコ生放送番組 ID のマッピングを更新し、
                            # 後続のニコニコ実況番組に切り替えてコメント受信処理を再開する
                            ## 08/22 まで公式生放送で運用されている暫定ニコニコ実況向けの処理
                            if new_program_info.status == 'ENDED':
                                await asyncio.sleep(25)  # 記事の反映まで 25 秒ほど待つ
                                await NDGRClient.updateJikkyoChannelIDMap()
                                self.nicolive_program_id = NDGRClient.JIKKYO_CHANNEL_ID_MAP[self.jikkyo_channel_id]
                                return 'RESTART'  # 再起動信号を返す

                            # 同一ニコニコチャンネルで連続して配信されているものの、ニコニコ生放送番組 ID が変更された場合は、
                            # 後続のニコニコ実況番組に切り替えてコメント受信処理を再開する
                            ## ニコニコ実況の毎日 04:00 での番組リセット向けの処理
                            elif new_program_info.nicoliveProgramId != self.nicolive_program_id:
                                return 'RESTART'  # 再起動信号を返す

                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.print('Error fetching program info:')
                        self.print(traceback.format_exc())
                        self.print(Rule(characters='-', style=Style(color='#E33157')))

            async def fetch_chunked_entries() -> None:
                """
                NDGR View API から ChunkedEntry をリアルタイムストリーミングし、
                NDGR Segment API への接続情報を取得して ChunkedMessage 受信タスクを開始する
                """

                nonlocal ready_for_next, is_first_time
                while True:

                    # 状態次第で NDGR View API の ?at= に渡すタイムスタンプを決定する
                    # 初回アクセス時は ?at=now を指定する
                    # 次回アクセス時は ?at= に ChunkedEntry.ReadyForNext.at に設定されている UNIX タイムスタンプを指定する
                    at: str | None = None
                    if ready_for_next is not None:
                        at = str(ready_for_next.at)
                    elif is_first_time:
                        at = 'now'
                        is_first_time = False

                    ready_for_next = None

                    # NDGR View API への接続が失敗した場合は 3 回リトライする
                    retry_count = 0
                    while retry_count < 3:
                        try:
                            async for chunked_entry in self.fetchChunkedEntries(view_api_uri, at):

                                # NDGR Segment API への接続情報を取得
                                ## 現在、現在受信中のセグメントの配信終了時刻の 6 秒前に次のセグメントへの接続情報が配信される仕様になっている
                                ## 次のセグメントへの接続情報を受信次第、即座にセグメント受信タスクを開始する
                                ## こうすることで、前のセグメントの配信が終了してから次のセグメントの配信開始後に受信するまでの時間的ギャップを回避できる
                                if chunked_entry.HasField('segment'):

                                    # セグメントの配信開始時刻と配信終了時刻の UNIX タイムスタンプを取得
                                    ## セグメントには配信開始時刻より前から接続できる
                                    segment = chunked_entry.segment
                                    segment_from = segment.from_.seconds + (segment.from_.nanos / 1e9)
                                    segment_until = segment.until.seconds + (segment.until.nanos / 1e9)
                                    self.print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] '
                                               f'Segment From: {datetime.fromtimestamp(segment_from).strftime("%H:%M:%S")} / '
                                               f'Segment Until: {datetime.fromtimestamp(segment_until).strftime("%H:%M:%S")}', verbose_log=True)
                                    self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)

                                    # 新しい ChunkedMessage 受信タスクを作成し、開始
                                    task = asyncio.create_task(fetch_chunked_message(segment))
                                    active_segments[segment.uri] = task

                                # 次回の NDGR View API アクセス用タイムスタンプを取得
                                elif chunked_entry.HasField('next'):
                                    ready_for_next = chunked_entry.next

                            # 例外が発生することなく受信が完了したらループを抜ける
                            break

                        except KeyboardInterrupt:
                            raise
                        except Exception:
                            self.print('Error fetching chunked entries:')
                            self.print(traceback.format_exc())
                            self.print(Rule(characters='-', style=Style(color='#E33157')))
                            retry_count += 1
                            if retry_count >= 3:
                                raise  # 3回リトライしても失敗したら継続を諦めて例外を投げる
                            await asyncio.sleep(1)  # 1秒待ってリトライ

                    # chunked_entry.next が設定されていない場合は放送が終了したとみなす
                    if ready_for_next is None:
                        break

            async def fetch_chunked_message(segment: chat.MessageSegment) -> None:
                """
                NDGR Segment API から 16 秒間 ChunkedMessage (から変換された NDGRComment) をリアルタイムストリーミングし、
                受信次第 stream_comments_inner() で yield するための Queue に格納する
                この関数は fetch_chunked_entries() からバックグラウンド実行される
                """

                try:
                    async for comment in self.fetchChunkedMessages(segment.uri):
                        await comment_queue.put(comment)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.print('Error fetching chunked messages:')
                    self.print(traceback.format_exc())
                    self.print(Rule(characters='-', style=Style(color='#E33157')))
                finally:
                    # 配信終了時刻を過ぎて ChunkedMessage 受信が完了したらアクティブリストから削除
                    active_segments.pop(segment.uri, None)

            # ChunkedEntry 受信タスクを開始
            program_info_task = asyncio.create_task(fetch_program_info())
            chunked_entries_task = asyncio.create_task(fetch_chunked_entries())

            try:
                while True:
                    # コメントキューからコメントを取り出すタスクと番組情報状態監視タスクを同時に待機し、どちらかが完了するまで待機
                    ## 大半のケースでコメントキューの方が早く完了する (コメントは多い時だと 0.01 秒間隔で降ってくるため)
                    done, _ = await asyncio.wait(
                        [asyncio.create_task(comment_queue.get()), program_info_task],
                        return_when = asyncio.FIRST_COMPLETED,
                    )

                    # 先に完了した方のタスクを処理
                    for task in done:

                        # 番組情報状態監視タスクが先に完了した: 現在コメント受信中の番組の放送が終了した
                        if task is program_info_task:
                            result = cast(Literal['ENDED', 'RESTART'], task.result())
                            # ここで ENDED (処理終了) または RESTART (次の番組へ移行) を返した時点で
                            ## stream_comments_inner() での処理は終了する
                            if result == 'ENDED':
                                self.print('Program Ended. Stopping...')
                            elif result == 'RESTART':
                                self.print('Program Ended. Switching to Next Program...')
                            self.print(Rule(characters='-', style=Style(color='#E33157')))
                            yield result

                        # コメントキューから受信したコメントを取得して yield で返す
                        else:
                            comment = cast(NDGRComment, await task)
                            yield comment
                            comment_queue.task_done()
            finally:

                # すべてのアクティブな ChunkedMessage 受信タスクをキャンセル
                for task in active_segments.values():
                    task.cancel()
                chunked_entries_task.cancel()
                program_info_task.cancel()

                # タスクが完全に終了するのを待つ
                await asyncio.gather(chunked_entries_task, program_info_task, *active_segments.values(), return_exceptions=True)

        # コメント受信処理を開始
        while True:
            async for comment in stream_comments_inner():
                # comment が 'ENDED' のときはこのメソッドでの処理を終了
                if comment == 'ENDED':
                    return
                # comment が 'RESTART' のときは一度ジェネレータを中断し、新たに stream_comments_inner() を呼び出す
                elif comment == 'RESTART':
                    break  # ここで break すると外側の while True: ループに戻る
                # comment が NDGRComment のときは yield する
                else:
                    yield comment


    async def downloadBackwardComments(self) -> list[NDGRComment]:
        """
        NDGR メッセージサーバーから過去に投稿されたコメントを遡ってダウンロードする

        Returns:
            list[NDGRComment]: 過去に投稿されたコメントのリスト (投稿日時昇順)

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        # 視聴ページから NDGR View API の URI を取得する
        nicolive_program_info = await self.fetchNicoLiveProgramInfo()
        self.print(f'Title:  {nicolive_program_info.title} [{nicolive_program_info.status}] ({nicolive_program_info.nicoliveProgramId})')
        self.print(f'Period: {datetime.fromtimestamp(nicolive_program_info.openTime).strftime("%Y-%m-%d %H:%M:%S")} ~ '
                   f'{datetime.fromtimestamp(nicolive_program_info.endTime).strftime("%Y-%m-%d %H:%M:%S")} '
                   f'({datetime.fromtimestamp(nicolive_program_info.endTime) - datetime.fromtimestamp(nicolive_program_info.openTime)}h)')
        self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)
        view_api_uri = await self.fetchNDGRViewURI(nicolive_program_info.webSocketUrl)

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
            # 次回アクセス時は ?at= に ChunkedEntry.ReadyForNext.at に設定されている UNIX タイムスタンプを指定する
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
                ChunkedEntry の受信を処理する非同期ジェネレータ関数
                ChunkedEntry には、NDGR Segment API / NDGR Backward API など複数の API のアクセス先 URI が含まれる
                """

                nonlocal ready_for_next, backward_api_uri
                async for chunked_entry in self.fetchChunkedEntries(view_api_uri, at):

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
            self.print(f'Retrieving {backward_api_uri} ...', verbose_log=True)
            self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)
            response = await self.httpx_client.get(backward_api_uri, timeout=15.0)
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
                self.print(str(comment), verbose_log=True)
                self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)

            # 現在の comments の前側に temp_comments の内容を連結
            comments = temp_comments + comments
            self.print(f'Retrieved a total of {len(comments)} comments.', end='\r')  # 進捗ログを上書きする

            # next フィールドが設定されていれば、続けて過去のコメントを取得
            if packed_segment.HasField('next'):
                # NDGR Backward API の URI を次のコメント取得用に更新
                backward_api_uri = packed_segment.next.uri
            else:
                break

            # 短時間に大量アクセスすると 403 を返される可能性があるので、0.01 秒待つ
            ## ニコニコ生放送 (Re:仮) ではアクセス制限が厳しめだったが、今はそんなに待たなくても規制されないっぽい
            await asyncio.sleep(0.01)

        self.print('')  # 最終行の進捗ログを消さないように改行する
        self.print(Rule(characters='-', style=Style(color='#E33157')))
        return comments


    async def fetchNicoLiveProgramInfo(self) -> NicoLiveProgramInfo:
        """
        ニコニコ生放送の視聴ページを解析し、ニコニコ生放送の番組情報を取得する

        Returns:
            NicoLiveProgramInfo: 解析されたニコニコ生放送の番組情報

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            ValueError: タイムシフト視聴を開始できない場合
            AssertionError: 解析に失敗した場合
        """

        watch_page_url = f'https://live.nicovideo.jp/watch/{self.nicolive_program_id}'
        reserve_response = await self.httpx_client.get(watch_page_url, timeout=15.0)
        reserve_response.raise_for_status()

        soup = BeautifulSoup(reserve_response.text, 'html.parser')
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
            nicoliveProgramId = embedded_data['program']['nicoliveProgramId'],
            title = embedded_data['program']['title'],
            description = embedded_data['program']['description'],
            status = embedded_data['program']['status'],
            openTime = embedded_data['program']['openTime'],
            beginTime = embedded_data['program']['beginTime'],
            vposBaseTime = embedded_data['program']['vposBaseTime'],
            endTime = embedded_data['program']['endTime'],
            scheduledEndTime = embedded_data['program']['scheduledEndTime'],
            webSocketUrl = embedded_data['site']['relive']['webSocketUrl'],
        )

        # この時点で status が ENDED (放送終了済み) かつ websocketUrl が空文字列の場合、
        # まだタイムシフトが有効であればタイムシフト予約 + 視聴開始を行うことで webSocketUrl が取得できるようになる
        ## なお、放送終了済みの段階でタイムシフト視聴を開始するにはプレミアム会員である必要がある
        ## タイムシフト視聴の開始はログイン中でないとできないため、ログイン中のみ実行する
        if program_info.status == 'ENDED' and program_info.webSocketUrl == '' and self.is_logged_in:

            # タイムシフト予約を実行
            api_url = f'https://live2.nicovideo.jp/api/v2/programs/{self.nicolive_program_id}/timeshift/reservation'
            reserve_response = await self.httpx_client.post(api_url, headers={'x-frontend-id': '9'}, timeout=15.0)
            ## meta.errorCode が "DUPLICATED" の場合は既にタイムシフト予約済みなので無視する
            if reserve_response.status_code != 200 and reserve_response.json().get('meta', {}).get('errorCode') != 'DUPLICATED':
                raise ValueError(f'Failed to reserve timeshift. (HTTP Error {reserve_response.status_code}) Are you premium member?')

            # タイムシフト視聴を開始
            ## この API の実行後、ニコニコ生放送の視聴ページから webSocketUrl が取得できるようになる
            start_watching_response = await self.httpx_client.patch(api_url, headers={'x-frontend-id': '9'}, timeout=15.0)
            if start_watching_response.status_code != 200:
                raise ValueError(f'Failed to start timeshift watching. (HTTP Error {start_watching_response.status_code}) Are you premium member?')

            # 再度ニコニコ生放送の視聴ページから webSocketUrl を取得
            program_info = await self.fetchNicoLiveProgramInfo()
            if program_info.webSocketUrl == '':
                raise ValueError('Failed to get webSocketUrl after timeshift reservation and start watching.')
            self.print('Timeshift watching has started.', verbose_log=True)

        # 上記条件以外で webSocketUrl が空文字列の場合は例外を送出…すると streamComments() での再接続処理に問題が出るため、行わない
        # エラー処理は各自で行う必要がある

        return program_info


    async def fetchNDGRViewURI(self, webSocketUrl: str) -> str:
        """
        ニコニコ生放送の視聴ページから取得した webSocketUrl に接続し、NDGR View API の URI を取得する
        Protobuf ストリームが返ることからして、NDGR メッセージサーバーは大災害前のニコ生の WebSocket API とは仕様が大きく異なる
        この API を叩くことで NDGR メッセージサーバー内部でどこまでリソース確保が行われているのかはよくわからない…
        (レスポンスヘッダーを見る限り CloudFront のキャッシュがヒットしてそうなので、多くの同時接続を捌けるようキャッシュされている？)

        Args:
            webSocketUrl (str): 視聴ページから取得した webSocketUrl

        Returns:
            str: 当該番組に対応する NDGR View API の URI

        Raises:
            httpx.HTTPStatusError: HTTP リクエストが失敗した場合
            AssertionError: 解析に失敗した場合
        """

        # もし WebSocket URL が空文字列の場合は例外を送出
        if webSocketUrl == '':
            raise ValueError('webSocketUrl is empty.')

        # ニコニコ生放送の視聴ページから取得した webSocketUrl に接続
        async with websockets.connect(webSocketUrl, user_agent_header=self.HTTP_HEADERS['user-agent']) as websocket:

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
        Protobuf ストリームを読み込み、読み取った Protobuf チャンクをジェネレータで返す
        Protobuf ストリームを最後まで読み切ったら None を返す
        エラー発生時は 5 回までリトライしてから例外を送出する

        Args:
            uri (str): 読み込む Protobuf ストリームの URI
            protobuf_class (Type[ProtobufType]): 読み込む Protobuf の型

        Yields:
            ProtobufType: Protobuf チャンク (protobuf_class で指定した型)
        """

        api_name = ''
        if '/view/' in uri:
            api_name = 'NDGR View API'
        elif '/segment/' in uri:
            api_name = 'NDGR Segment API'
        self.print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] Fetching {api_name} ...', verbose_log=True)
        self.print(uri, verbose_log=True)
        self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)

        max_retries = 5  # 5回までリトライ
        retry_delay = 3  # 3秒待ってリトライ

        for attempt in range(max_retries):
            try:
                protobuf_reader = ProtobufStreamReader()

                # Protobuf ストリームを取得
                # HTTP エラー発生時は例外を送出してリトライさせる
                async with self.httpx_client.stream('GET', uri, timeout=httpx.Timeout(15.0, read=None)) as response:
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

                            # ジェネレータとして読み取った Protobuf を返す
                            yield protobuf

                # Protobuf ストリームを最後まで読み切ったら、ループを抜ける
                self.print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] Fetched {api_name}.', verbose_log=True)
                self.print(uri, verbose_log=True)
                self.print(Rule(characters='-', style=Style(color='#E33157')), verbose_log=True)
                break

            # HTTP 接続エラー発生時、しばらく待ってからリトライを試みる
            except (httpx.HTTPError, httpx.StreamError):
                if attempt < max_retries - 1:
                    self.print(f'Error fetching {api_name}. Retrying in {retry_delay} seconds...')
                    self.print(traceback.format_exc())
                    await asyncio.sleep(retry_delay)

                # 最後の試行でも失敗した場合、例外を再発生させる
                else:
                    self.print(f'Error fetching {api_name}. Max retries reached.')
                    self.print(traceback.format_exc())
                    self.print(Rule(characters='-', style=Style(color='#E33157')))
                    raise


    def print(self, *args: Any, verbose_log: bool = False, **kwargs: Any) -> None:
        """
        NDGRClient の動作ログをコンソールやファイルに出力する

        Args:
            verbose_log (bool, default=False): 詳細な動作ログかどうか (指定された場合、コンストラクタで verbose が指定された時のみ出力する)
        """

        # このログが詳細な動作ログで、かつ詳細な動作ログの出力が有効でない場合は何もしない
        if verbose_log is True and self.verbose is False:
            return

        # 有効ならログをコンソールに出力する
        if self.show_log is True:
            print(*args, **kwargs)

        # ログファイルのパスが指定されている場合は、ログをファイルにも出力
        if self.log_path is not None:
            with self.log_path.open('a') as f:
                print(*args, **kwargs, file=f)


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
            at = datetime.fromtimestamp(chunked_message.meta.at.seconds + (chunked_message.meta.at.nanos / 1e9)),
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
        if comment.raw_user_id > 0:
            user_id = str(comment.raw_user_id)
        else:
            user_id = str(comment.hashed_user_id)

        # XMLCompatibleComment オブジェクトを生成
        xml_compatible_comment = XMLCompatibleComment(
            # lv 付きの生放送番組 ID をスレッド ID として設定
            ## NDGR メッセージサーバーには「スレッド」と一対一で対応する概念は存在しない
            thread = f'lv{comment.live_id}',
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
    def convertToXMLString(comments: Sequence[NDGRComment | XMLCompatibleComment]) -> str:
        """
        コメントリストをコメント投稿時刻順にソートしたヘッダーなし XML 文字列 (.nicojk 形式) に変換する

        Args:
            comments (Sequence[NDGRComment | XMLCompatibleComment]): NDGRComment または XMLCompatibleComment のリスト

        Returns:
            str: XML 文字列
        """

        def sanitize_for_xml(text: str) -> str:
            # XML と互換性のない制御文字を除去
            # 有効な XML 制御文字 (タブ、改行、復帰) は保持
            return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

        # XML のエレメントツリー
        elem_tree = ET.Element('packet')

        # コメントを XMLCompatibleComment に変換し、コメント投稿時刻昇順でソート
        xml_compatible_comments = [
            NDGRClient.convertToXMLCompatibleComment(comment) if isinstance(comment, NDGRComment) else comment
            for comment in comments
        ]
        xml_compatible_comments.sort(key=lambda x: x.date_with_usec)

        # コメントごとに
        for xml_compatible_comment in xml_compatible_comments:

            # コメントをさらに辞書に変換
            comment_dict = xml_compatible_comment.model_dump()

            # コメント本文を取得して消す（ XML ではタグ内の値として入るため）
            chat_content = comment_dict['content']
            del comment_dict['content']

            # ユーザー ID が 35 文字以上のコメントを NX-Jikkyo に投稿されたコメントと判定し、識別用に nx_jikkyo="1" を追加する
            ## NX-Jikkyo で生成されるユーザー ID は SHA-1: 40 文字 (初期に投稿されたコメントのみ UUID v4: 36 文字) のため、
            ## 35 文字以上であれば確実に NX-Jikkyo に投稿されたコメントだと判定できる
            ## NDGRClient ライブラリの責務的には本来ここに書くべき処理ではないが、とはいえこの関数を独自実装するとコードが重複するためやむを得ず…
            if len(comment_dict['user_id']) >= 35:
                comment_dict['nx_jikkyo'] = '1'

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
