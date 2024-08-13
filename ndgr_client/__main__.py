
import typer
from datetime import datetime
from pathlib import Path
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import __version__
from ndgr_client.ndgr_client import NDGRClient
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Message Server Client Library')

@app.command(help='Stream comments from NDGR server.')
async def stream(
    nicolive_program_id: str = typer.Argument(help='Nicolive program ID (ex: jk1, jk9, jk211 / lv345479988)'),
    verbose: bool = typer.Option(default=False, help='Verbose output'),
):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # NDGRClient を初期化
    await NDGRClient.updateJikkyoChannelIDMap()
    ndgr_client = NDGRClient(nicolive_program_id, verbose=verbose, console_output=True)

    # コメントをエンドレスでストリーミング開始
    async for comment in ndgr_client.streamComments():
        if verbose is True:
            print(f'[{datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")}] Comment Received. [grey70](ID: {comment.id})[/grey70]')
        print(str(comment))
        print(Rule(characters='-', style=Style(color='#E33157')))


@app.command(help='Download backward comments (kakolog) from NDGR server.')
async def download(
    nicolive_program_id: str = typer.Argument(help='Nicolive program ID (ex: jk1, jk9, jk211 / lv345479988) or "all"'),
    output_dir: Path = typer.Option(default=Path('.'), help='Output directory'),
    verbose: bool = typer.Option(default=False, help='Verbose output'),
):
    print(Rule(characters='=', style=Style(color='#E33157')))

    # jikkyo_id に 'all' が指定された場合は全てのチャンネルをダウンロード
    if nicolive_program_id == 'all':
        jikkyo_ids = [id for id in NDGRClient.JIKKYO_CHANNEL_ID_MAP.keys()]
    else:
        jikkyo_ids = [nicolive_program_id]

    await NDGRClient.updateJikkyoChannelIDMap()
    comment_counts: dict[str, int] = {}
    for jid in jikkyo_ids:
        # NDGRClient を初期化
        ndgr_client = NDGRClient(jid, verbose=verbose, console_output=True)

        # コメントをダウンロード
        comments = await ndgr_client.downloadBackwardComments()
        comment_counts[jid] = len(comments)

        # output_dir に {jid}.nicojk として保存
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / f'{jid}.nicojk', mode='w', encoding='utf-8') as f:
            f.write(NDGRClient.convertToXMLString(comments))
        print(f'Total comments for {jid}: {comment_counts[jid]}')
        print(f'Saved to {output_dir / f"{jid}.nicojk"}.')
        print(Rule(characters='=', style=Style(color='#E33157')))

    if nicolive_program_id == 'all':
        print('Download completed for all channels.')
        for jid, count in comment_counts.items():
            print(f'{jid:>5}: {count:>5} comments')
        print(Rule(characters='=', style=Style(color='#E33157')))


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
