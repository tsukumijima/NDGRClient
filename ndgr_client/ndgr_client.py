
from __future__ import annotations

import asyncio
import json
import httpx
import lxml.etree as ET
from bs4 import BeautifulSoup, Tag
from datetime import datetime
from rich import print
from rich.rule import Rule
from rich.style import Style
from typing import Any, Awaitable, Callable, cast, Type, TypeVar

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
            show_log (bool, default=False): グラフィカルなログを出力するかどうか
        """

        if jikkyo_id not in self.JIKKYO_ID_TO_REKARI_ID_MAP:
            raise ValueError(f'Invalid jikkyo_id: {jikkyo_id}')

        self.jikkyo_id = jikkyo_id
        self.rekari_id = self.JIKKYO_ID_TO_REKARI_ID_MAP[jikkyo_id]
        self.show_log = show_log

        # httpx の非同期 HTTP クライアントのインスタンスを作成
        self.httpx_client = httpx.AsyncClient(
            ## リダイレクトを追跡する
            follow_redirects = True,
        )


    async def streamComments(self, callback: Callable[[NDGRComment], None | Awaitable[None]]) -> None:
        """
        NDGR サーバーからリアルタイムコメントを随時ストリーミングし、コールバックに渡す
        このメソッドはエラーなどでストリーミングが中断した場合を除き基本的に戻らない

        Args:
            callback (Callable[[NDGRComment], Awaitable[None]]): NDGR サーバーから受信したコメントを受け取るコールバック関数

        Raises:
            AssertionError: 解析に失敗した場合
        """

        # 視聴ページから NDGR View API の URI を取得する
        embedded_data = await self.parseWatchPage()
        view_api_uri = await self.getNDGRViewAPIUri(embedded_data.ndgrProgramCommentViewUri)

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
                ChunkedEntry には、NDGR Segment API / NDGR Backward API など複数の API のアクセス先 URI が含まれる
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
                        async def message_callback(chunked_message: chat.ChunkedMessage) -> None:

                            # meta または message が存在しない場合は空の ChunkedMessage なので無視する
                            if not chunked_message.HasField('meta') or not chunked_message.HasField('message'):
                                return

                            # 取り回しやすいように NDGRComment Pydantic モデルに変換した上で、コールバック関数に渡す
                            comment = self.convertToNDGRComment(chunked_message)
                            if self.show_log:
                                print(str(comment))
                                print(Rule(characters='-', style=Style(color='#E33157')))
                            callback(comment)

                        # NDGR Segment API から ChunkedMessage の受信を開始 (受信が完了するまで非同期にブロックする)
                        await self.readProtobufStream(segment.uri, chat.ChunkedMessage, message_callback)

            # NDGR View API から ChunkedEntry の受信を開始 (受信が完了するまで非同期にブロックする)
            await self.readProtobufStream(f'{view_api_uri}?at={at}', chat.ChunkedEntry, chunk_callback)


    async def downloadBackwardComments(self) -> list[NDGRComment]:
        """
        NDGR サーバーから過去に投稿されたコメントを遡ってダウンロードする

        Returns:
            list[NDGRComment]: 過去に投稿されたコメントのリスト (時系列昇順)
        """

        # 視聴ページから NDGR View API の URI を取得する
        embedded_data = await self.parseWatchPage()
        view_api_uri = await self.getNDGRViewAPIUri(embedded_data.ndgrProgramCommentViewUri)

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

            async def chunk_callback(chunked_entry: chat.ChunkedEntry) -> None:
                """
                ChunkedEntry の受信を処理するコールバック関数
                ChunkedEntry には、NDGR Segment API / NDGR Backward API など複数の API のアクセス先 URI が含まれる
                """
                nonlocal ready_for_next, backward_api_uri

                # next フィールドがある場合は、NDGR View API への次回アクセス時に ?at= に指定するタイムスタンプ
                # (が格納された ChunkedEntry.ReadyForNext) を更新する
                if chunked_entry.HasField('next'):
                    assert ready_for_next is None, 'Duplicated ReadyForNext'
                    ready_for_next = chunked_entry.next

                # backward フィールドがある場合は、BackwardSegment.segment.uri から NDGR Backward API の URI を取得する
                elif chunked_entry.HasField('backward'):
                    backward_api_uri = chunked_entry.backward.segment.uri
                    backward_api_uri_found.set()  # イベントを設定して、ループを終了させる

            # NDGR View API から ChunkedEntry の受信を開始する非同期タスクを作成
            read_task = asyncio.create_task(self.readProtobufStream(f'{view_api_uri}?at={at}', chat.ChunkedEntry, chunk_callback))

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
            streamContentUri = embedded_data['temporaryMeasure']['streamContentUri'],
            ndgrProgramCommentViewUri = embedded_data['temporaryMeasure']['ndgrProgramCommentViewUri'],
            ndgrProgramCommentPostUri = embedded_data['temporaryMeasure']['ndgrProgramCommentPostUri'],
        )
        if self.show_log:
            print(f'Title:  {program_info.title} [{program_info.status}]')
            print(f'Period: {datetime.fromtimestamp(program_info.openTime).strftime("%Y-%m-%d %H:%M:%S")} ~ '
              f'{datetime.fromtimestamp(program_info.scheduledEndTime).strftime("%Y-%m-%d %H:%M:%S")} '
              f'({datetime.fromtimestamp(program_info.scheduledEndTime) - datetime.fromtimestamp(program_info.openTime)}h)')
            print(Rule(characters='-', style=Style(color='#E33157')))

        return program_info


    async def getNDGRViewAPIUri(self, ndgrProgramCommentViewUri: str) -> str:
        """
        ニコニコ生放送 (Re:仮) の視聴ページから取得した ndgrProgramCommentViewUri を使って、NDGR View API の URI を取得する
        Protobuf ストリームが返ることからして、NDGR サーバーは大災害前のニコ生の WebSocket API とは仕様が大きく異なる
        この API を叩くことで NDGR サーバー内部でどこまでリソース確保が行われているのかはよくわからない…
        (レスポンスヘッダーを見る限り CloudFront のキャッシュがヒットしてそうなので、多くの同時接続を捌けるようキャッシュされている？)

        Args:
            ndgrProgramCommentViewUri (str): 視聴ページから取得した ndgrProgramCommentViewUri

        Returns:
            str: 当該番組に対応する NDGR View API の URI

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


    ProtobufType = TypeVar('ProtobufType', chat.ChunkedEntry, chat.ChunkedMessage, chat.PackedSegment)
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

        max_retries = 5  # 5回までリトライ
        retry_delay = 3  # 3秒待ってリトライ

        for attempt in range(max_retries):
            try:
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
                break  # 成功した場合、ループを抜ける

            except (httpx.HTTPError, httpx.StreamError) as e:
                if attempt < max_retries - 1:
                    if self.show_log:
                        print(f'HTTP error occurred: {e}. Retrying in {retry_delay} seconds...')
                    await asyncio.sleep(retry_delay)
                else:
                    if self.show_log:
                        print(f'Max retries reached. Error: {e}')
                    raise  # 最後の試行でも失敗した場合、例外を再発生させる


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

        # コマンド文字列を生成
        ## NDGR サーバーのコメントは現状常に匿名化されるため、mail フィールドに '184' を設定している
        command = ['184']
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

        # XMLCompatibleComment オブジェクトを生成
        xml_compatible_comment = XMLCompatibleComment(
            thread = str(comment.live_id),
            no = comment.no,
            vpos = comment.vpos,
            date = int(comment.at.timestamp()),
            date_usec = int((comment.at.timestamp() % 1) * 1000000),
            user_id = f'rekari:{comment.hashed_user_id}',  # ニコニコ生放送 (Re:仮) のコメントだと識別できる prefix を付与
            mail = ' '.join(command),
            premium = 1 if comment.account_status == 'Premium' else None,
            anonymity = 1,  # コメントは常に匿名化されているため、常に anonymity フィールドに 1 を設定している
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

        # XML のエレメントツリー
        elem_tree = ET.Element('packet')

        # NDGR サーバーでは現状コメ番が付与されていないため、独自に連番で付与する
        comment_no = 1

        # コメント投稿時刻昇順でソート
        comments.sort(key=lambda x: x.at)

        # コメントごとに
        for comment in comments:

            # コメントを XMLCompatibleComment に変換したあと、さらに辞書に変換
            # このときコメ番を一つずつ加算していく
            comment = NDGRClient.convertToXMLCompatibleComment(comment)
            comment.no = comment_no
            comment_no += 1
            comment_dict = comment.model_dump()

            # コメント本文を取得して消す（ XML ではタグ内の値として入るため）
            chat_content = comment_dict['content']
            del comment_dict['content']

            # 属性を XML エレメントに追加
            chat_elem_tree = ET.SubElement(elem_tree, 'chat', {key: str(value) for key, value in comment_dict.items() if value is not None})

            # XML エレメント内の値に以前取得した本文を指定
            chat_elem_tree.text = chat_content

        # 素の XML を .nicojk 形式向けにフォーマットする
        # lxml.etree を使うことで属性の順序を保持できる
        # ref: https://banatech.net/blog/view/19
        xml_string = ET.tostring(elem_tree, encoding='utf-8', pretty_print=True).decode('utf-8').replace('>\n  ', '>\n')  # インデントを除去
        xml_string = xml_string.replace('<packet>\n', '').replace('</packet>', '').replace('<packet/>', '')  # <packet> タグを除去
        return xml_string.rstrip()
