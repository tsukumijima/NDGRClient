
import typer

from ndgr_client import __version__


app = typer.Typer(help='NDGRClient: Nicolive NDGR Comment Server Client')

@app.command(help='Stream comments from NDGR comment server.')
def stream():
    pass


@app.command(help='Download backward comments from NDGR comment server.')
def download_kakolog():
    pass


@app.command(help='Show version.')
def version():
    typer.echo(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
