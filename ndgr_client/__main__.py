from datetime import datetime
from pathlib import Path

import anyio
import typer
from pydantic import TypeAdapter
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import NDGRClient, __version__
from ndgr_client.niconico_oauth import NiconicoOAuth, NiconicoProgramEndedError
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Message Server Client Library')


@app.command(help='Authorize a niconico account and save OAuth credentials.')
async def authorize(
    oauth_file: Path = typer.Option(..., help='OAuth credentials file to create'),
    client_id: str = typer.Option(..., envvar='NICONICO_CLIENT_ID', prompt=True),
    client_secret: str = typer.Option(..., envvar='NICONICO_CLIENT_SECRET', prompt=True, hide_input=True),
):
    """ブラウザでの初回認可を案内し、コメント取得に使うトークンをファイルへ保存する。"""

    await NiconicoOAuth(oauth_file).authorize(client_id, client_secret, on_url=typer.echo)
    print('OAuth credentials saved successfully.')


@app.command(help='Stream comments from NDGR server.')
async def stream(
    nicolive_program_id: str = typer.Argument(help='Nicolive program ID (ex: jk1, jk9, jk211 / lv345479988)'),
    oauth_file: Path | None = typer.Option(default=None, help='OAuth credentials file'),
    cookies_file: Path | None = typer.Option(default=None, help='JSON cookie dictionary'),
    verbose: bool = typer.Option(default=False, help='Verbose output'),
):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # NDGRClient を初期化
    # 非同期コンテキストマネージャーを使ってリソースの確実な解放を保証する
    async with NDGRClient(
        nicolive_program_id, verbose=verbose, console_output=True, oauth_file=oauth_file
    ) as ndgr_client:
        # ファイルから読み込んだ Cookie のログイン状態を確認し、無効ならコメントの収集開始前にエラーを返す
        if cookies_file is not None:
            cookies = TypeAdapter(dict[str, str]).validate_json(
                await anyio.Path(cookies_file).read_text(encoding='utf-8')
            )
            if await ndgr_client.setCookies(cookies) is False:
                raise ValueError('The saved cookies have expired. Import fresh cookies or use OAuth.')

        # コメントをエンドレスでストリーミング開始
        async for comment in ndgr_client.streamComments():
            if verbose is True:
                print(
                    f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] Comment Received. [grey70](ID: {comment.id})[/grey70]'
                )
            print(str(comment))
            print(Rule(characters='-', style=Style(color='#E33157')))


@app.command(help='Download backward comments (kakolog) from NDGR server.')
async def download(
    nicolive_program_id: str = typer.Argument(help='Nicolive program ID (ex: jk1, jk9, jk211 / lv345479988) or "all"'),
    output_dir: Path = typer.Option(default=Path('.'), help='Output directory'),
    oauth_file: Path | None = typer.Option(default=None, help='OAuth credentials file'),
    cookies_file: Path | None = typer.Option(default=None, help='JSON cookie dictionary'),
    verbose: bool = typer.Option(default=False, help='Verbose output'),
):
    print(Rule(characters='=', style=Style(color='#E33157')))

    # jikkyo_id に 'all' が指定された場合は全てのチャンネルをダウンロード
    if nicolive_program_id == 'all':
        jikkyo_ids = [id for id in NDGRClient.JIKKYO_CHANNEL_ID_MAP.keys()]
    else:
        jikkyo_ids = [nicolive_program_id]

    cookies: dict[str, str] | None = None
    if cookies_file is not None:
        cookies = TypeAdapter(dict[str, str]).validate_json(await anyio.Path(cookies_file).read_text(encoding='utf-8'))

    comment_counts: dict[str, int | None] = {}
    for jid in jikkyo_ids:
        # NDGRClient を初期化
        # 非同期コンテキストマネージャーを使ってリソースの確実な解放を保証する
        async with NDGRClient(jid, verbose=verbose, console_output=True, oauth_file=oauth_file) as ndgr_client:
            # ファイルから読み込んだ Cookie のログイン状態を確認し、無効ならコメントの収集開始前にエラーを返す
            if cookies is not None:
                if await ndgr_client.setCookies(cookies) is False:
                    raise ValueError('The saved cookies have expired. Import fresh cookies or use OAuth.')

            # コメントをダウンロード
            try:
                comments = await ndgr_client.downloadBackwardComments()
            except NiconicoProgramEndedError:
                # 全件取得では接続可能な番組がないチャンネルを記録し、次のチャンネルへ進む
                ## 番組を個別に指定した場合は、取得できなかったことをエラーとして返す
                if nicolive_program_id != 'all':
                    raise
                comment_counts[jid] = None
                print(f'Skipped {jid}: program is no longer available (PROGRAM_ENDED).')
                continue
            comment_counts[jid] = len(comments)

            # output_dir に {jid}.nicojk として保存
            # anyio.Path を使って非同期でファイル I/O を行い、イベントループのブロックを回避する
            async_output_dir = anyio.Path(output_dir)
            await async_output_dir.mkdir(parents=True, exist_ok=True)
            async with await (async_output_dir / f'{jid}.nicojk').open(mode='w', encoding='utf-8') as f:
                await f.write(NDGRClient.convertToXMLString(comments))
            print(f'Total comments for {jid}: {comment_counts[jid]}')
            print(f'Saved to {output_dir / f"{jid}.nicojk"}.')
            print(Rule(characters='=', style=Style(color='#E33157')))

    if nicolive_program_id == 'all':
        print('Download completed for all available channels.')
        for jid, count in comment_counts.items():
            # スキップしたチャンネルは、取得結果が0件だったチャンネルと区別して表示する
            if count is None:
                print(f'{jid:>5}: skipped (PROGRAM_ENDED)')
            else:
                print(f'{jid:>5}: {count:>5} comments')
        print(Rule(characters='=', style=Style(color='#E33157')))


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
