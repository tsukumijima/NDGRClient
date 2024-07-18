
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import __version__
from ndgr_client.ndgr_client import NDGRClient
from ndgr_client.schemas import NDGRComment
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Server Client')

@app.command(help='Stream comments from NDGR server.')
async def stream(jikkyo_id: str):
    ndgr_client = NDGRClient(jikkyo_id, show_log=True)
    def callback(comment: NDGRComment):
        print(comment)
        print(Rule(characters='-', style=Style(color='#E33157')))
    await ndgr_client.streamComments(callback)


@app.command(help='Download backward comments from NDGR server.')
def download_kakolog():
    pass


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
