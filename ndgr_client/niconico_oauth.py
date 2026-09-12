from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlencode

import anyio
from aiohttp import web
from curl_cffi import requests
from filelock import AsyncFileLock
from pydantic import AliasPath, BaseModel, ConfigDict, Field, SecretStr, ValidationError


TokenUpdateResult = TypeVar('TokenUpdateResult')


class NiconicoOAuthError(ValueError):
    """再認可や設定の修正が必要なニコニコ OAuth のエラー。"""


class NiconicoProgramEndedError(ValueError):
    """視聴 API が PROGRAM_ENDED を返し、番組への接続を受け付けない場合のエラー。"""


class OAuthTokenResponse(BaseModel):
    """トークン API が返すアクセストークン、更新用トークン、有効期間。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    expires_in: int = Field(gt=0)


class OAuthCredentials(BaseModel):
    """ファイルに保存する OAuth クライアント情報と、認可したアカウントのトークン。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    client_id: str = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    expires_at: float
    user_id: str = Field(pattern=r'^\d+$')


class OAuthUserInfo(BaseModel):
    """OpenID Connect のユーザー情報 API が返す、認可したアカウントのユーザー ID 。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    sub: str = Field(pattern=r'^\d+$')


class OAuthWebSocketData(BaseModel):
    """視聴 API が返す WebSocket 接続先。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    url: str = Field(pattern=r'^wss://')


class OAuthWebSocketResponse(BaseModel):
    """視聴 API のレスポンス。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    data: OAuthWebSocketData


class OAuthWebSocketErrorResponse(BaseModel):
    """視聴 API が接続を拒否した理由を表すエラーコード。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    error_code: str = Field(validation_alias=AliasPath('meta', 'errorCode'))


class NiconicoOAuth:
    """
    ニコニコ OAuth の初回認可と、保存済みトークンの更新を扱う。
    同じファイルを使う収集プロセスはファイルロックで更新を直列化する。
    トークンの更新中に呼び出し元がキャンセルされた場合も、更新処理が終了するまで待つ。

    Args:
        oauth_file (Path): クライアント情報とトークンを保存するファイル
    """

    # KonomiTV の中継 API は、state.server で指定したサーバーの /api/niconico/callback へブラウザをリダイレクトする
    ## https://github.com/tsukumijima/KonomiTV-API/blob/master/src/index.ts
    REDIRECT_URI = 'https://app.konomi.tv/api/redirect/niconico'
    # 同じ OAuth クライアントを使う KonomiTV と同じスコープを要求し、認可済みアカウントで再度同意を求められないようにする
    SCOPES = ' '.join(
        [
            'offline_access',
            'openid',
            'profile',
            'user.authorities.relives.watch.get',
            'user.authorities.relives.watch.interact',
            'user.premium',
        ]
    )
    TOKEN_URL = 'https://oauth.nicovideo.jp/oauth2/token'

    def __init__(self, oauth_file: Path) -> None:
        """
        認証情報の保存先を絶対パスで保持する。

        Args:
            oauth_file (Path): クライアント情報とトークンを保存するファイル
        """

        self.oauth_file = oauth_file.resolve()

    async def authorize(
        self,
        client_id: str,
        client_secret: str,
        callback_port: int = 25250,
        on_url: Callable[[str], None] = print,
    ) -> None:
        """
        初回認可用 URL を案内し、ブラウザから認可コードを受け取ってトークンへ交換・保存する。
        このプロセスとは別のマシンでブラウザを開く場合は、コールバックを受信するポートへの SSH 転送が必要になる。

        Args:
            client_id (str): 登録済み OAuth クライアント ID
            client_secret (str): OAuth クライアントのシークレット
            callback_port (int): 認可コードを受信するローカル HTTP サーバーのポート番号
            on_url (Callable[[str], None]): ブラウザで開く認可用 URL を引数として呼び出す関数

        Raises:
            asyncio.TimeoutError: 認可コードの受信が制限時間 (600秒) 内に完了しなかった場合
            NiconicoOAuthError: 認可が拒否された場合、またはトークン交換・ユーザー情報取得に失敗した場合
            curl_cffi.requests.exceptions.HTTPError: OAuth API がレート制限 (HTTP 429) またはサーバー障害 (HTTP 5xx) を返した場合
            asyncio.CancelledError: トークン保存中に呼び出し元がキャンセルされた場合 (保存処理は完了する)
            filelock.Timeout: 認証情報ファイルのロック取得が制限時間 (60秒) 内に完了しなかった場合
        """

        nonce = secrets.token_urlsafe(32)
        code_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def receive_callback(request: web.Request) -> web.Response:
            """ブラウザからのコールバックに含まれる認可コードと nonce を検査する。"""

            # この認可手続きで発行した nonce と一致するリクエストを1回だけ受け付ける
            query = request.query
            if (
                secrets.compare_digest(query.get('nonce', '').encode(), nonce.encode()) is False
                or code_future.done() is True
            ):
                return web.Response(
                    status=400, text='Invalid authorization callback.', headers={'Cache-Control': 'no-store'}
                )
            # 認可エラーを受信した場合は、待機中の authorize() に例外を返す
            if 'error' in query:
                code_future.set_exception(NiconicoOAuthError('Authorization was denied.'))
                return web.Response(status=400, text='Authorization was denied.', headers={'Cache-Control': 'no-store'})
            # 認可コードのないリクエストにはエラーを返し、正しいコールバックを待つ
            if not query.get('code'):
                return web.Response(
                    status=400, text='Missing authorization code.', headers={'Cache-Control': 'no-store'}
                )

            # 認可コードを待機中の authorize() に渡し、ブラウザには受信完了を通知する
            code_future.set_result(query['code'])
            return web.Response(
                text='Authorization code received. Return to the terminal to check completion.',
                headers={'Cache-Control': 'no-store'},
            )

        # 認可用 URL を案内する前に、コールバックを受信する HTTP サーバーを起動する
        app = web.Application()
        app.router.add_get('/api/niconico/callback', receive_callback, allow_head=False)
        # 認可コードを含む URL の記録を避けるため、HTTP サーバーのアクセスログを無効にする
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', callback_port).start()
            port = runner.addresses[0][1]
            state = base64.b64encode(
                json.dumps({'server': f'http://127.0.0.1:{port}', 'nonce': nonce}).encode()
            ).decode()
            on_url(
                'https://oauth.nicovideo.jp/oauth2/authorize?'
                + urlencode(
                    {
                        'client_id': client_id,
                        'redirect_uri': self.REDIRECT_URI,
                        'response_type': 'code',
                        'scope': self.SCOPES,
                        'state': state,
                    }
                )
            )
            code = await asyncio.wait_for(code_future, timeout=600)
        finally:
            await runner.cleanup()

        async def exchange_and_save() -> None:
            """認可コードをトークンへ交換し、認可したアカウントのユーザー ID とともに保存する。"""

            # 同じ認証情報ファイルを使う他のプロセスとの同時書き込みを防ぐため、ファイルロックを取得する
            await anyio.Path(self.oauth_file.parent).mkdir(parents=True, exist_ok=True)
            async with AsyncFileLock(str(self.oauth_file) + '.lock', timeout=60):
                requested_at = time.time()
                async with requests.AsyncSession(impersonate='chrome') as session:
                    response = await session.post(
                        self.TOKEN_URL,
                        data={
                            'grant_type': 'authorization_code',
                            'client_id': client_id,
                            'client_secret': client_secret,
                            'code': code,
                            'redirect_uri': self.REDIRECT_URI,
                        },
                        timeout=15,
                    )
                    self._checkResponse(response)
                    token = OAuthTokenResponse.model_validate_json(response.content)
                    # 取得したトークンでユーザー情報 API にアクセスし、視聴 API に渡すユーザー ID を確認する
                    response = await session.get(
                        'https://oauth.nicovideo.jp/open_id/userinfo',
                        headers={
                            'Authorization': f'Bearer {token.access_token.get_secret_value()}',
                        },
                        timeout=15,
                    )
                    self._checkResponse(response)
                    user = OAuthUserInfo.model_validate_json(response.content)
                await self._save(
                    OAuthCredentials(
                        client_id=client_id,
                        client_secret=SecretStr(client_secret),
                        access_token=token.access_token,
                        refresh_token=token.refresh_token,
                        expires_at=requested_at + token.expires_in,
                        user_id=user.sub,
                    )
                )

        await self._finishTokenUpdate(asyncio.create_task(exchange_and_save()))

    async def _load(self) -> OAuthCredentials:
        """
        保存済み認証情報を読み込む。

        Returns:
            OAuthCredentials: 保存済みのクライアント情報とトークン

        Raises:
            NiconicoOAuthError: 認証情報が存在しないか無効な場合
        """

        try:
            return OAuthCredentials.model_validate_json(await anyio.Path(self.oauth_file).read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError) as ex:
            raise NiconicoOAuthError('OAuth credentials are missing or invalid. Run the authorize command.') from ex

    async def _save(self, credentials: OAuthCredentials) -> None:
        """
        認証情報を一時ファイルへ書き込み、書き込みが完了してから保存先のファイルを置き換える。
        呼び出し元でファイルロックを保持すること。

        Args:
            credentials (OAuthCredentials): 保存する認証情報

        Raises:
            OSError: 認証情報ファイルの書き込みまたは置き換えに失敗した場合
        """

        # SecretStr は通常の JSON 出力では伏せ字になるため、保存時だけ値を取り出す
        data = credentials.model_dump(mode='json')
        data['client_secret'] = credentials.client_secret.get_secret_value()
        data['access_token'] = credentials.access_token.get_secret_value()
        data['refresh_token'] = credentials.refresh_token.get_secret_value()

        def write_atomic() -> None:
            """認証情報を一時ファイルへ書き込み、保存先のファイルを置き換える。"""

            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', dir=self.oauth_file.parent, delete=False
                ) as file:
                    temporary_path = Path(file.name)
                    json.dump(data, file)
                    file.flush()
                    os.fsync(file.fileno())
                temporary_path.replace(self.oauth_file)
            finally:
                # 保存に失敗した場合の一時ファイルも、認証情報を含むため片付ける
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        # ディスクへの書き込み待ちでイベントループを止めないよう、保存処理をワーカースレッドで実行する
        await asyncio.to_thread(write_atomic)

    async def getWebSocketURL(self, nicolive_program_id: str) -> str:
        """
        OAuth 認証で視聴 API にアクセスし、指定した番組の WebSocket 接続先を取得する。

        Args:
            nicolive_program_id (str): lv 付きの番組 ID

        Returns:
            str: 認可したアカウントで指定番組に接続するための WebSocket URL

        Raises:
            NiconicoProgramEndedError: 指定番組がすでに終了済みの場合
            NiconicoOAuthError: 認証情報が無効な場合、または視聴 API が認証・権限エラーを返した場合
            curl_cffi.requests.exceptions.HTTPError: 視聴 API がレート制限 (HTTP 429) またはサーバー障害 (HTTP 5xx) を返した場合
            asyncio.CancelledError: トークン更新中に呼び出し元がキャンセルされた場合 (更新処理は完了する)
            filelock.Timeout: 認証情報ファイルのロック取得が制限時間 (60秒) 内に完了しなかった場合
        """

        rejected_token: str | None = None
        # アクセストークンは、視聴 API へのリクエストの Authorization ヘッダーに設定する
        async with requests.AsyncSession(impersonate='chrome') as session:
            for attempt in range(2):
                credentials = await self._getCredentials(rejected_token)
                response = await session.get(
                    'https://api.live2.nicovideo.jp/api/v1/wsendpoint',
                    params={'nicoliveProgramId': nicolive_program_id, 'userId': credentials.user_id},
                    headers={
                        'Authorization': f'Bearer {credentials.access_token.get_secret_value()}',
                        'User-Agent': 'NDGRClient',
                    },
                    timeout=15,
                )
                # 有効期限前でも HTTP 401 で拒否された場合は、トークンを更新して一度だけ再試行する
                if response.status_code == 401 and attempt == 0:
                    rejected_token = credentials.access_token.get_secret_value()
                    continue
                # 接続を受け付けなくなった番組は、認証設定の修正が必要なエラーと区別して呼び出し元へ伝える
                ## 403 の本文が PROGRAM_ENDED の形式でない場合は、通常の認証エラーとして扱う
                if response.status_code == 403:
                    try:
                        error = OAuthWebSocketErrorResponse.model_validate_json(response.content)
                    except ValidationError:
                        # 本文から番組終了を読み取れない場合は、後続の _checkResponse() に進む
                        pass
                    else:
                        if error.error_code == 'PROGRAM_ENDED':
                            raise NiconicoProgramEndedError(
                                f'Program {nicolive_program_id} is no longer available (PROGRAM_ENDED).'
                            )
                self._checkResponse(response)
                return OAuthWebSocketResponse.model_validate_json(response.content).data.url

        # 401 の再試行は1回までで、2回目は URL を返すか _checkResponse() が例外を送出する
        raise AssertionError('OAuth watch API retry loop exited without a response.')

    async def _getCredentials(self, rejected_token: str | None = None) -> OAuthCredentials:
        """
        保存済みトークンを読み、期限切れまたは拒否された場合に更新して保存する。

        Args:
            rejected_token (str | None): 視聴 API へのアクセス時に HTTP 401 で拒否されたアクセストークン

        Returns:
            OAuthCredentials: 使用可能な認証情報 (更新が不要な場合は保存済みの情報)

        Raises:
            NiconicoOAuthError: 認証情報が無効な場合、またはトークン更新 API がエラーを返した場合
            curl_cffi.requests.exceptions.HTTPError: トークン更新 API がレート制限 (HTTP 429) またはサーバー障害 (HTTP 5xx) を返した場合
            asyncio.CancelledError: トークン更新中に呼び出し元がキャンセルされた場合 (更新処理は完了する)
            filelock.Timeout: 認証情報ファイルのロック取得が制限時間 (60秒) 内に完了しなかった場合
            OSError: 更新した認証情報の保存に失敗した場合
        """

        async def refresh_and_save() -> OAuthCredentials:
            """ファイルロックの取得後に認証情報を読み込み、必要ならトークンを更新して保存する。"""

            # ロックの取得後にファイルを読み込み、他のプロセスが保存した新しいトークンも判定対象にする
            async with AsyncFileLock(str(self.oauth_file) + '.lock', timeout=60):
                credentials = await self._load()
                is_rejected = rejected_token is not None and secrets.compare_digest(
                    credentials.access_token.get_secret_value(),
                    rejected_token,
                )
                if credentials.expires_at > time.time() + 60 and is_rejected is False:
                    return credentials

                # アクセストークンと更新用トークンは、API が返した新しい値を同じファイルへ保存する
                requested_at = time.time()
                async with requests.AsyncSession(impersonate='chrome') as session:
                    response = await session.post(
                        self.TOKEN_URL,
                        data={
                            'grant_type': 'refresh_token',
                            'client_id': credentials.client_id,
                            'client_secret': credentials.client_secret.get_secret_value(),
                            'refresh_token': credentials.refresh_token.get_secret_value(),
                        },
                        timeout=15,
                    )
                self._checkResponse(response)
                token = OAuthTokenResponse.model_validate_json(response.content)
                credentials.access_token = token.access_token
                credentials.refresh_token = token.refresh_token
                credentials.expires_at = requested_at + token.expires_in
                await self._save(credentials)
                return credentials

        return await self._finishTokenUpdate(asyncio.create_task(refresh_and_save()))

    @staticmethod
    def _checkResponse(response: requests.Response) -> None:
        """
        HTTP ステータスコードを確認し、エラーの種類に応じた例外を送出する。

        Args:
            response (requests.Response): OAuth API のレスポンス

        Raises:
            NiconicoOAuthError: OAuth API が HTTP 4xx (429 を除く) を返した場合
            curl_cffi.requests.exceptions.HTTPError: OAuth API がレート制限 (HTTP 429) またはサーバー障害 (HTTP 5xx) を返した場合
        """

        # レート制限やサーバー障害は通信エラー、それ以外の失敗は認証設定の確認を促すエラーとして返す
        if response.status_code == 429 or response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            raise NiconicoOAuthError(
                f'OAuth request failed (HTTP {response.status_code}). Check authorization and permissions.'
            )

    @staticmethod
    async def _finishTokenUpdate(task: asyncio.Task[TokenUpdateResult]) -> TokenUpdateResult:
        """
        トークン更新タスクの終了を待ち、処理中に受けたキャンセルを呼び出し元へ返す。

        Args:
            task (asyncio.Task[TokenUpdateResult]): ファイルロックの取得、トークンの更新・保存、ロックの解放を行うタスク

        Returns:
            TokenUpdateResult: 更新処理の結果

        Raises:
            asyncio.CancelledError: 更新処理の完了前に呼び出し元がキャンセルされた場合 (更新処理は完了する)
        """

        # 呼び出し元がキャンセルされても、トークンの更新と保存を行うタスクの終了まで待つ
        ## 更新リクエストがサーバーで処理された後に中断すると、新しい更新用トークンを保存できず再認可が必要になる
        ## 繰り返しのキャンセルも更新タスクへ伝えず、タスクが返した結果または例外を受け取る
        is_cancelled = False
        while task.done() is False:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                is_cancelled = True
        result = task.result()
        if is_cancelled is True:
            raise asyncio.CancelledError
        return result
