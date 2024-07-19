
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
async def stream(jikkyo_id: str):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # NDGRClient を初期化
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)

    # コメントをエンドレスでストリーミング開始
    def callback(comment: NDGRComment):
        pass
    await ndgr_client.streamComments(callback)


@app.command(help='Download backward comments (kakolog) from NDGR server.')
async def download(jikkyo_id: str, output_dir: Path = typer.Option(default=Path('.'), help='Output directory')):
    print(Rule(characters='-', style=Style(color='#E33157')))

    # NDGRClient を初期化
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)

    # コメントをダウンロード
    comments = await ndgr_client.downloadBackwardComments()
    print(f'Total comments: {len(comments)}')
    print(Rule(characters='-', style=Style(color='#E33157')))

    # output_dir に {jikkyo_id}.nicojk として保存
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / f'{jikkyo_id}.nicojk', mode='w', encoding='utf-8') as f:
        f.write(NDGRClient.convertToXMLString(comments))


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
