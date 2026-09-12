import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import pytest
from curl_cffi import requests

from ndgr_client import NDGRClient, NDGRComment, NDGRStreamingSessionError, NicoLiveProgramInfo
from ndgr_client.proto.dwango.nicolive.chat.data import atoms_pb2 as atoms
from ndgr_client.proto.dwango.nicolive.chat.service.edge import payload_pb2 as chat
from ndgr_client.protobuf_stream_reader import ProtobufStreamReader


@pytest.fixture
async def live_client(request: pytest.FixtureRequest) -> AsyncIterator[NDGRClient]:
    """jk1 の現在の番組へ接続するクライアントを作成し、テスト終了時に接続を解放する。"""

    oauth_file = request.config.getoption('--oauth-file')
    async with NDGRClient('jk1', oauth_file=Path(oauth_file) if oauth_file is not None else None) as ndgr_client:
        yield ndgr_client


@pytest.mark.anyio
async def test_live_stream_comments(live_client: NDGRClient) -> None:
    """jk1 からコメントを受信し、XML 互換形式へ変換できることを確認する。"""

    # 深夜の投稿間隔も考慮して最大3分待ち、受信後はジェネレータを閉じて受信タスクを解放する
    stream = live_client.streamComments()
    try:
        async with asyncio.timeout(180):
            comments = [await anext(stream), await anext(stream)]
        assert len({comment.id for comment in comments}) == 2
        for comment in comments:
            assert comment.live_id > 0
            assert 0 <= time.time() - comment.at.timestamp() < 180
            converted = NDGRClient.convertToXMLCompatibleComment(comment)
            assert converted.date == int(comment.at.timestamp())
            assert converted.content == comment.content
            assert converted.user_id == (
                str(comment.raw_user_id) if comment.raw_user_id > 0 else comment.hashed_user_id
            )
    finally:
        await stream.aclose()


@pytest.mark.anyio
async def test_live_download_backward_comments(live_client: NDGRClient) -> None:
    """jk1 の過去ログを最後まで取得し、投稿順と XML への変換結果を確認する。"""

    # 投稿数が増える番組終了前も含め、全ページの取得完了を最大5分待つ
    async with asyncio.timeout(300):
        comments = await live_client.downloadBackwardComments()
    assert len(comments) > 0
    assert all(previous.at <= current.at for previous, current in pairwise(comments))

    # nicojk は chat 要素を並べる形式なので、検査用の親要素を付けて全コメントを解析する
    xml = NDGRClient.convertToXMLString(comments)
    root = ElementTree.fromstring(f'<packet>{xml}</packet>')
    nodes = root.findall('chat')
    assert len(nodes) == len(comments)
    # 全件について、番組と投稿日時が変換後にも保持されていることを確認する
    for comment, node in zip(comments, nodes, strict=True):
        assert node.attrib['thread'] == f'lv{comment.live_id}'
        assert int(node.attrib['date']) == int(comment.at.timestamp())
        assert node.attrib['user_id'] == (
            str(comment.raw_user_id) if comment.raw_user_id > 0 else comment.hashed_user_id
        )


@pytest.fixture
def message() -> chat.ChunkedMessage:
    """利用者情報を含まない合成コメントを、既知の投稿日時と装飾で作成する。"""

    message = chat.ChunkedMessage()
    message.meta.id = 'comment-1'
    message.meta.at.seconds = 1700000000
    message.meta.at.nanos = 250000000
    message.meta.origin.chat.live_id = 123
    message.message.chat.content = '<試験 & "本文">\n次の行'
    message.message.chat.hashed_user_id = 'a:test-user'
    message.message.chat.no = 7
    message.message.chat.vpos = 125
    message.message.chat.account_status = atoms.Chat.Premium
    message.message.chat.modifier.position = atoms.Chat.Modifier.shita
    message.message.chat.modifier.size = atoms.Chat.Modifier.small
    message.message.chat.modifier.full_color.r = 18
    message.message.chat.modifier.full_color.g = 52
    message.message.chat.modifier.full_color.b = 86
    return message


@pytest.fixture
def program() -> NicoLiveProgramInfo:
    """通信制御の検査に使う、固定された放送中番組の情報を返す。"""

    return NicoLiveProgramInfo(
        nicoliveProgramId='lv123',
        title='Test',
        description='',
        status='ON_AIR',
        openTime=1700000000,
        beginTime=1700000000,
        vposBaseTime=1700000000,
        endTime=1700086400,
        scheduledEndTime=1700086400,
        webSocketUrl='wss://example.test/watch',
    )


@pytest.fixture
async def client() -> AsyncIterator[NDGRClient]:
    """通信の差し替え対象となるクライアントを作成し、終了時に接続を解放する。"""

    async with NDGRClient('jk1') as client:
        yield client


@pytest.mark.parametrize('overflow', [False, True])
@pytest.mark.parametrize('anonymous', [False, True])
def test_comment_conversion_preserves_fields(message: chat.ChunkedMessage, overflow: bool, anonymous: bool) -> None:
    """通常・混雑時コメントの各フィールドと、匿名・非匿名の XML 表現を固定値で照合する。"""

    if anonymous is False:
        message.message.chat.raw_user_id = 42
    if overflow is True:
        message.message.overflowed_chat.CopyFrom(message.message.chat)
    comment = NDGRClient.convertToNDGRComment(message)
    assert comment.model_dump() == {
        'id': 'comment-1',
        'at': datetime.fromtimestamp(1700000000.25),
        'live_id': 123,
        'raw_user_id': 0 if anonymous else 42,
        'hashed_user_id': 'a:test-user',
        'account_status': 'Premium',
        'no': 7,
        'vpos': 125,
        'position': 'shita',
        'size': 'small',
        'color': {'r': 18, 'g': 52, 'b': 86},
        'font': 'defont',
        'opacity': 'Normal',
        'content': '<試験 & "本文">\n次の行',
    }
    converted = NDGRClient.convertToXMLCompatibleComment(comment)
    assert converted.date == 1700000000
    assert converted.date_usec == 250000
    assert converted.user_id == ('a:test-user' if anonymous else '42')
    assert converted.anonymity == (1 if anonymous else None)
    assert converted.premium == 1
    assert converted.mail == ('184 shita small #123456' if anonymous else 'shita small #123456')

    # XML のエスケープを実際に解析し、本文と属性が保存されることを確認する
    node = ElementTree.fromstring(NDGRClient.convertToXMLString([comment]))
    assert node.text == '<試験 & "本文">\n次の行'
    assert node.attrib['thread'] == 'lv123'
    assert node.attrib['no'] == '7'
    assert node.attrib['vpos'] == '125'
    assert node.attrib['date_usec'] == '250000'
    assert node.attrib['user_id'] == converted.user_id


def test_protobuf_reader_preserves_split_frames() -> None:
    """2バイトの長さヘッダーと本文が任意の位置で分割されても、全メッセージを復元できることを確認する。"""

    first = b'x' * 130
    framed = b'\x82\x01' + first + b'\x03end'
    # ヘッダー途中、本文途中、メッセージ境界の全位置を別々の受信分割として検査する
    for split in range(1, len(framed)):
        reader = ProtobufStreamReader()
        actual = []
        for chunk in (framed[:split], framed[split:]):
            reader.addNewChunk(chunk)
            while (payload := reader.unshiftChunk()) is not None:
                actual.append(payload)
        assert actual == [first, b'end'], split
        assert reader.buffer == bytearray()


@pytest.mark.anyio
async def test_backward_download_follows_all_pages(
    client: NDGRClient, program: NicoLiveProgramInfo, message: chat.ChunkedMessage
) -> None:
    """View API の次回時刻を辿り、過去ログの全ページを投稿順に結合して返すことを確認する。"""

    async def entries(uri: str, at: str | None) -> AsyncIterator[chat.ChunkedEntry]:
        """初回に次回時刻、その次に過去ログの接続先を返す。"""

        entry = chat.ChunkedEntry()
        if at == 'now':
            entry.next.at = 100
        else:
            assert at == '100'
            entry.backward.segment.uri = 'https://example.test/newer'
        yield entry

    newer = chat.PackedSegment()
    newer.messages.add().CopyFrom(message)
    newer.messages[0].meta.id = 'newer'
    newer.messages[0].meta.at.seconds += 10
    newer.next.uri = 'https://example.test/older'
    older = chat.PackedSegment()
    older.messages.add().CopyFrom(message)
    # コメント以外の通知や、必要なフィールドがないメッセージを同じページへ混ぜる
    older.messages.add()
    older.messages.add().meta.id = 'metadata-only'
    missing_modifier = older.messages.add()
    missing_modifier.CopyFrom(message)
    missing_modifier.message.chat.ClearField('modifier')
    responses = []
    for page in (newer, older):
        response = requests.Response()
        response.status_code = 200
        response.content = page.SerializeToString()
        responses.append(response)

    with (
        patch.object(client, 'fetchNicoLiveProgramInfo', return_value=program),
        patch.object(client, 'fetchNDGRViewURI', return_value='https://example.test/view'),
        patch.object(client, 'fetchChunkedEntries', side_effect=entries) as fetch_entries,
        patch.object(client.http_client, 'get', side_effect=responses) as get,
    ):
        async with asyncio.timeout(3):
            comments = await client.downloadBackwardComments()
    assert [comment.id for comment in comments] == ['comment-1', 'newer']
    assert [comment.at.timestamp() for comment in comments] == [1700000000.25, 1700000010.25]
    assert [call.args[1] for call in fetch_entries.call_args_list] == ['now', '100']
    assert [call.args[0] for call in get.call_args_list] == [
        'https://example.test/newer',
        'https://example.test/older',
    ]


@pytest.mark.anyio
@pytest.mark.parametrize('notification', ['error', 'disconnect'])
async def test_watch_session_failure_is_reported(client: NDGRClient, notification: str) -> None:
    """視聴サーバーからの拒否・切断通知を、再接続対象の例外として返すことを確認する。"""

    with patch('ndgr_client.ndgr_client.websockets.connect') as connect:
        websocket = connect.return_value.__aenter__.return_value
        websocket.recv = AsyncMock(return_value=json.dumps({'type': notification, 'data': {'reason': 'END_PROGRAM'}}))
        with pytest.raises(NDGRStreamingSessionError, match='END_PROGRAM'):
            await client.fetchNDGRViewURI('wss://example.test/watch')
        connect.return_value.__aexit__.assert_awaited_once()


@pytest.mark.anyio
async def test_watch_session_answers_ping(client: NDGRClient) -> None:
    """視聴接続で ping に応答し、通知された NDGR View URI を返して接続を閉じることを確認する。"""

    with patch('ndgr_client.ndgr_client.websockets.connect') as connect:
        websocket = connect.return_value.__aenter__.return_value
        websocket.recv = AsyncMock(
            side_effect=[
                json.dumps({'type': 'ping'}),
                json.dumps({'type': 'messageServer', 'data': {'viewUri': 'https://example.test/view'}}),
            ]
        )
        assert await client.fetchNDGRViewURI('wss://example.test/watch') == 'https://example.test/view'
        assert [json.loads(call.args[0]) for call in websocket.send.call_args_list] == [
            {'type': 'startWatching', 'data': {'reconnect': False}},
            {'type': 'pong'},
        ]
        websocket.close.assert_awaited_once()


@pytest.mark.anyio
async def test_stream_recreates_connection_after_watch_failure(
    client: NDGRClient, program: NicoLiveProgramInfo
) -> None:
    """視聴接続の失敗後に HTTP クライアントを交換して再試行し、恒久的なエラーは呼び出し元へ返すことを確認する。"""

    original = client.http_client
    terminal_error = ValueError('Stop after retry')
    with (
        patch.object(client, 'fetchNicoLiveProgramInfo', return_value=program),
        patch.object(
            client, 'fetchNDGRViewURI', side_effect=[NDGRStreamingSessionError('Disconnected'), terminal_error]
        ) as fetch,
        patch.object(client, 'STREAM_RETRY_DELAY_SECONDS', 0),
        patch.object(original, 'close', wraps=original.close) as close,
    ):
        stream = client.streamComments()
        try:
            async with asyncio.timeout(3):
                with pytest.raises(ValueError) as error:
                    await anext(stream)
            assert error.value is terminal_error
            assert fetch.await_count == 2
            assert client.http_client is not original
            close.assert_awaited_once()
        finally:
            await stream.aclose()


@pytest.mark.anyio
async def test_cancelled_stream_releases_waiting_tasks(client: NDGRClient, program: NicoLiveProgramInfo) -> None:
    """コメント待機中にキャンセルした場合、View・Segment の受信とキュー待機のタスクが終了することを確認する。"""

    segment_started = asyncio.Event()
    view_closed = asyncio.Event()
    segment_closed = asyncio.Event()

    async def entries(uri: str, at: str | None) -> AsyncIterator[chat.ChunkedEntry]:
        """同じセグメントを2回通知してから、受信待機を継続する。"""

        entry = chat.ChunkedEntry()
        entry.segment.uri = 'https://example.test/segment'
        try:
            yield entry
            yield entry
            await asyncio.Event().wait()
        finally:
            view_closed.set()

    async def messages(uri: str) -> AsyncIterator[NDGRComment]:
        """セグメント接続後、コメントを送らずに待機する。"""

        try:
            segment_started.set()
            await asyncio.Event().wait()
            return
            yield  # 非同期ジェネレータとして、コメントが届かない接続を再現する
        finally:
            segment_closed.set()

    baseline = asyncio.all_tasks()
    with (
        patch.object(client, 'fetchNicoLiveProgramInfo', return_value=program),
        patch.object(client, 'fetchNDGRViewURI', return_value='https://example.test/view'),
        patch.object(client, 'fetchChunkedEntries', side_effect=entries),
        patch.object(client, 'fetchChunkedMessages', side_effect=messages) as fetch_messages,
    ):
        stream = client.streamComments()
        task = asyncio.create_task(anext(stream))
        try:
            async with asyncio.timeout(3):
                await segment_started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert view_closed.is_set()
            assert segment_closed.is_set()
            assert fetch_messages.call_count == 1
            leaked = asyncio.all_tasks() - baseline
            assert leaked == set(), [pending.get_coro().__qualname__ for pending in leaked]
        finally:
            # 検査が失敗した場合も、このテストで起動した待機処理を回収する
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await stream.aclose()
            remaining = asyncio.all_tasks() - baseline
            for pending in remaining:
                pending.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.anyio
async def test_stream_switches_to_next_program(
    client: NDGRClient, program: NicoLiveProgramInfo, message: chat.ChunkedMessage
) -> None:
    """番組変更の検知後に古い受信を終了し、後続番組のコメントを同じジェネレータで返すことを確認する。"""

    switch = asyncio.Event()
    old_segment_closed = asyncio.Event()
    original_sleep = asyncio.sleep
    next_program = program.model_copy(update={'nicoliveProgramId': 'lv124', 'webSocketUrl': 'wss://example.test/next'})

    async def sleep(delay: float) -> None:
        """番組監視の待機だけを、テストから通知する番組変更の時点まで進める。"""

        if delay > 5:
            await switch.wait()
            switch.clear()
        else:
            await original_sleep(delay)

    async def entries(uri: str, at: str | None) -> AsyncIterator[chat.ChunkedEntry]:
        """番組ごとに異なる受信セグメントを通知する。"""

        entry = chat.ChunkedEntry()
        entry.segment.uri = uri
        yield entry
        await asyncio.Event().wait()

    async def messages(uri: str) -> AsyncIterator[NDGRComment]:
        """番組の識別情報を付けたコメントを1件返し、残りの受信を待機する。"""

        comment = NDGRClient.convertToNDGRComment(message)
        try:
            if uri == 'new-view':
                assert old_segment_closed.is_set()
                comment = comment.model_copy(update={'id': 'next-comment', 'live_id': 124})
            yield comment
            await asyncio.Event().wait()
        finally:
            if uri == 'old-view':
                old_segment_closed.set()

    with (
        patch.object(client, 'fetchNicoLiveProgramInfo', side_effect=[program, next_program, next_program]),
        patch.object(client, 'fetchNDGRViewURI', side_effect=['old-view', 'new-view']) as view,
        patch.object(client, 'fetchChunkedEntries', side_effect=entries),
        patch.object(client, 'fetchChunkedMessages', side_effect=messages),
        patch('ndgr_client.ndgr_client.asyncio.sleep', side_effect=sleep),
    ):
        stream = client.streamComments()
        try:
            async with asyncio.timeout(3):
                assert (await anext(stream)).live_id == 123
                switch.set()
                following = await anext(stream)
                assert (following.id, following.live_id) == ('next-comment', 124)
            assert [call.args[0] for call in view.call_args_list] == [program.webSocketUrl, next_program.webSocketUrl]
        finally:
            # 次の受信待機まで進めてからキャンセルし、稼働中の受信タスクを終了させる
            pending = asyncio.create_task(anext(stream))
            await original_sleep(0)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
