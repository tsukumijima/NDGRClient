
from rich import print
from rich.rule import Rule
from rich.style import Style

from ndgr_client import __version__
from ndgr_client.ndgr_client import NDGRClient
from ndgr_client.proto.dwango.nicolive.chat.service.edge import payload_pb2 as chat
from ndgr_client.utils import AsyncTyper


app = AsyncTyper(help='NDGRClient: Nicolive NDGR Comment Server Client')

@app.command(help='Stream comments from NDGR comment server.')
@app.command(help='Stream comments from NDGR comment server.')
async def stream(jikkyo_id: str):
    ndgr_client = NDGRClient(jikkyo_id)
    embedded_data = await ndgr_client.parseWatchPage()
    view_uri = await ndgr_client.acquireViewUri(embedded_data.ndgrProgramCommentViewUri)

    ready_for_next: chat.ChunkedEntry.ReadyForNext | None = None
    is_first_time: bool = True
    already_know_segments: set[str] = set()

    while True:
        at: str | None = None
        if ready_for_next is not None:
            at = str(ready_for_next.at)
        elif is_first_time:
            at = 'now'
            is_first_time = False

        ready_for_next = None

        async def chunk_callback(entry: chat.ChunkedEntry):
            nonlocal ready_for_next

            if entry.HasField('next'):
                if ready_for_next is not None:
                    raise ValueError('Duplicated ReadyForNext')
                ready_for_next = entry.next
            elif entry.HasField('segment'):
                segment = entry.segment
                if segment.uri not in already_know_segments:
                    already_know_segments.add(segment.uri)
                    async def message_callback(message: chat.ChunkedMessage):
                        print(message)
                        print(Rule(characters='-', style=Style(color='#E33157')))
                    await ndgr_client.readProtobufStream(segment.uri, chat.ChunkedMessage, message_callback)

        await ndgr_client.readProtobufStream(f'{view_uri}?at={at}', chat.ChunkedEntry, chunk_callback)


@app.command(help='Download backward comments from NDGR comment server.')
def download_kakolog():
    pass


@app.command(help='Show version.')
def version():
    print(f'NDGRClient version {__version__}')


if __name__ == '__main__':
    app()
