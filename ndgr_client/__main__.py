
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import __version__
from ndgr_client.ndgr_client import NDGRClient
from ndgr_client.constants import NDGRComment
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Server Client')

@app.command(help='Stream comments from NDGR server.')
async def stream(jikkyo_id: str):
    print(Rule(characters='-', style=Style(color='#E33157')))
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)
    def callback(comment: NDGRComment):
        pass
    await ndgr_client.streamComments(callback)


@app.command(help='Download backward comments (kakolog) from NDGR server.')
async def download_kakolog(jikkyo_id: str):
    print(Rule(characters='-', style=Style(color='#E33157')))
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)
    comments = await ndgr_client.downloadBackwardComments()
    print(f'Total comments: {len(comments)}')
    print(Rule(characters='-', style=Style(color='#E33157')))


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
