# pyright: reportPrivateUsage=false

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession
from curl_cffi import requests
from pydantic import SecretStr

from ndgr_client.niconico_oauth import NiconicoOAuth, NiconicoOAuthError, NiconicoProgramEndedError, OAuthCredentials


@pytest.mark.anyio
class TestNiconicoOAuth:
    """OAuth の認可結果の検査と、トークン更新時の認証情報の保存を確認する。"""

    @pytest.fixture(autouse=True)
    async def prepare_credentials(self, tmp_path: Path) -> None:
        """各テストの一時ディレクトリに、テスト用の認証情報ファイルを作成する。"""

        self.oauth = NiconicoOAuth(tmp_path / 'oauth.json')
        self.credentials = OAuthCredentials(
            client_id='test-client',
            client_secret=SecretStr('test-secret'),
            access_token=SecretStr('old-access'),
            refresh_token=SecretStr('old-refresh'),
            expires_at=time.time() + 3600,
            user_id='1',
        )
        await self.oauth._save(self.credentials)

    async def test_rejected_token_is_refreshed_once(self) -> None:
        """視聴 API に HTTP 401 で拒否された場合、新しいトークンで WebSocket 接続先を取得し直すことを確認する。"""

        rejected = requests.Response()
        rejected.status_code = 401
        accepted = requests.Response()
        accepted.status_code = 200
        accepted.content = b'{"data":{"url":"wss://example.test/watch"}}'
        refreshed = self.credentials.model_copy(update={'access_token': SecretStr('new-access')})

        # 視聴 API が最初は HTTP 401、次は接続先 URL を返す状況を再現する
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session = session_class.return_value.__aenter__.return_value
            session.get = AsyncMock(side_effect=[rejected, accepted])
            with patch.object(self.oauth, '_getCredentials', side_effect=[self.credentials, refreshed]) as load:
                assert await self.oauth.getWebSocketURL('lv1') == 'wss://example.test/watch'
            assert load.call_args_list[1].args == ('old-access',)
            assert session.get.await_count == 2
            assert session.get.call_args.kwargs['headers']['Authorization'] == 'Bearer new-access'

    async def test_failed_refresh_keeps_credentials(self) -> None:
        """トークンの更新が HTTP 400 で拒否された場合、保存済みの認証情報が保持されることを確認する。"""

        original = self.oauth.oauth_file.read_bytes()
        response = requests.Response()
        response.status_code = 400
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session_class.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            with pytest.raises(NiconicoOAuthError):
                await self.oauth._getCredentials('old-access')
        assert self.oauth.oauth_file.read_bytes() == original

    @pytest.mark.parametrize(
        ('error_code', 'expected_exception'),
        [('PROGRAM_ENDED', NiconicoProgramEndedError), ('FORBIDDEN', NiconicoOAuthError)],
    )
    async def test_ended_program_is_distinguished_from_authentication_failure(
        self, error_code: str, expected_exception: type[ValueError]
    ) -> None:
        """HTTP 403 の理由が PROGRAM_ENDED の場合だけ、番組が取得対象外になったことを伝える例外になることを確認する。"""

        response = requests.Response()
        response.status_code = 403
        response.content = json.dumps({'meta': {'status': 403, 'errorCode': error_code}}).encode()
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session_class.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
            with pytest.raises(expected_exception):
                await self.oauth.getWebSocketURL('lv1')

    @pytest.mark.parametrize(
        'content',
        [b'not-json', b'{}', b'{"meta":{"status":403}}'],
    )
    async def test_malformed_forbidden_response_is_authentication_error(self, content: bytes) -> None:
        """HTTP 403 でも PROGRAM_ENDED 形式でない本文は、認証設定の確認を促す例外になることを確認する。"""

        response = requests.Response()
        response.status_code = 403
        response.content = content
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session_class.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
            with pytest.raises(NiconicoOAuthError, match='HTTP 403'):
                await self.oauth.getWebSocketURL('lv1')

    async def test_other_process_refresh_is_reused(self) -> None:
        """他のプロセスが新しいトークンを保存済みなら、更新リクエストを送らずにそのトークンを使うことを確認する。"""

        self.credentials.access_token = SecretStr('already-refreshed')
        await self.oauth._save(self.credentials)
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            credentials = await self.oauth._getCredentials('old-access')
            session_class.assert_not_called()
        assert credentials.access_token.get_secret_value() == 'already-refreshed'

    async def test_failed_replace_keeps_previous_file(self) -> None:
        """保存先のファイルを置き換えられなかった場合、元の認証情報を保持し、一時ファイルを削除することを確認する。"""

        original = self.oauth.oauth_file.read_bytes()
        self.credentials.access_token = SecretStr('new-access')
        with patch.object(Path, 'replace', side_effect=OSError('Test save failure')):
            with pytest.raises(OSError, match='Test save failure'):
                await self.oauth._save(self.credentials)
        assert self.oauth.oauth_file.read_bytes() == original
        assert list(self.oauth.oauth_file.parent.iterdir()) == [self.oauth.oauth_file]

    async def test_cancelled_caller_still_saves_rotated_token(self) -> None:
        """トークン更新中に呼び出し元がキャンセルされても、新しい更新用トークンが保存されることを確認する。"""

        started = asyncio.Event()
        release = asyncio.Event()
        response = requests.Response()
        response.status_code = 200
        response.content = b'{"access_token":"new-access","refresh_token":"new-refresh","expires_in":3600}'

        async def post(*args: Any, **kwargs: Any) -> requests.Response:
            """更新リクエストへの応答を待たせ、呼び出し元をキャンセルする時点を再現する。"""

            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return response

        # トークン更新の応答待ちで呼び出し元をキャンセルし、その後に新しいトークンを返す
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session_class.return_value.__aenter__.return_value.post = AsyncMock(side_effect=post)
            task = asyncio.create_task(self.oauth._getCredentials('old-access'))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                task.cancel()
                await asyncio.sleep(0)
                assert task.done() is False
                task.cancel()
                await asyncio.sleep(0)
                assert task.done() is False
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert (await self.oauth._load()).refresh_token.get_secret_value() == 'new-refresh'

    async def test_callback_validates_request_before_exchanging_code(self) -> None:
        """不正なコールバックを拒否し、リダイレクト URI で認可コードをトークンへ交換して保存することを確認する。"""

        authorization_url: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        token = requests.Response()
        token.status_code = 200
        token.content = b'{"access_token":"new-access","refresh_token":"new-refresh","expires_in":3600}'
        user = requests.Response()
        user.status_code = 200
        user.content = b'{"sub":"1"}'

        # ニコニコの API への通信は差し替え、コールバックはローカルの HTTP サーバーへ送る
        with patch('ndgr_client.niconico_oauth.requests.AsyncSession') as session_class:
            session = session_class.return_value.__aenter__.return_value
            session.post = AsyncMock(return_value=token)
            session.get = AsyncMock(return_value=user)
            task = asyncio.create_task(
                self.oauth.authorize('test-client', 'test-secret', on_url=authorization_url.set_result)
            )
            try:
                url = await asyncio.wait_for(authorization_url, timeout=5)
                query = parse_qs(urlsplit(url).query)
                assert query['client_id'] == ['test-client']
                assert query['redirect_uri'] == [NiconicoOAuth.REDIRECT_URI]
                state = json.loads(base64.b64decode(query['state'][0]))
                assert set(state) == {'server'}
                assert state['server'].startswith('http://127.0.0.1:')
                callback = state['server'] + '/api/niconico/callback'
                async with ClientSession() as client:
                    async with client.get(state['server'] + '/unrelated') as response:
                        assert response.status == 404
                    async with client.get(callback) as response:
                        assert response.status == 400
                    async with client.post(callback, params={'code': 'test-code'}) as response:
                        assert response.status == 405
                    session.post.assert_not_awaited()
                    async with client.get(callback, params={'code': 'test-code'}) as response:
                        assert response.status == 200
                        assert response.headers['Cache-Control'] == 'no-store'
                        assert 'test-code' not in await response.text()
                await asyncio.wait_for(task, timeout=5)
            finally:
                if task.done() is False:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            assert session.post.call_args.kwargs['data']['code'] == 'test-code'
            assert session.post.call_args.kwargs['data']['client_id'] == 'test-client'
            assert session.post.call_args.kwargs['data']['client_secret'] == 'test-secret'
            assert session.post.call_args.kwargs['data']['redirect_uri'] == NiconicoOAuth.REDIRECT_URI
        assert (await self.oauth._load()).refresh_token.get_secret_value() == 'new-refresh'
