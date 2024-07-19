
import typer
from pathlib import Path
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import __version__
from ndgr_client.ndgr_client import NDGRClient
from ndgr_client.constants import NDGRComment
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Server Client Library')

@app.command(help='Stream comments from NDGR server.')
async def stream(jikkyo_id: str = typer.Argument(help='Jikkyo ID (jk0, jk1, jk9, jk211 ...)')):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # NDGRClient を初期化
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)

    # コメントをエンドレスでストリーミング開始
    async def callback(comment: NDGRComment):
        pass
    await ndgr_client.streamComments(callback)


@app.command(help='Download backward comments (kakolog) from NDGR server.')
async def download(
    jikkyo_id: str = typer.Argument(help='Jikkyo ID (jk0, jk1, jk9, jk211 ...) or "all"'),
    output_dir: Path = typer.Option(default=Path('.'), help='Output directory'),
):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # jikkyo_id に 'all' が指定された場合は全てのチャンネルをダウンロード
    if jikkyo_id == 'all':
        jikkyo_ids = [id for id in NDGRClient.JIKKYO_ID_TO_REKARI_ID_MAP.keys() if id != 'jk0']
    else:
        jikkyo_ids = [jikkyo_id]

    comment_counts: dict[str, int] = {}
    for jid in jikkyo_ids:
        # NDGRClient を初期化
        ndgr_client = NDGRClient(jid, show_log=True)

        # コメントをダウンロード
        comments = await ndgr_client.downloadBackwardComments()
        comment_counts[jid] = len(comments)
        print(f'Total comments for {jid}: {comment_counts[jid]}')
        print(Rule(characters='-', style=Style(color='#E33157')))

        # output_dir に {jid}.nicojk として保存
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / f'{jid}.nicojk', mode='w', encoding='utf-8') as f:
            f.write(NDGRClient.convertToXMLString(comments))
        print(f'Saved to {output_dir / f"{jid}.nicojk"}')
        print(Rule(characters='-', style=Style(color='#E33157')))

    if jikkyo_id == 'all':
        print('Download completed for all channels.')
        for jid, count in comment_counts.items():
            print(f'{jid}: {count} comments')
        print(Rule(characters='-', style=Style(color='#E33157')))


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
