import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """実通信テストで使う OAuth 認証情報を実行時に指定できるようにする。"""

    parser.addoption('--oauth-file', default=None, help='OAuth credentials file for live tests (default: anonymous).')


@pytest.fixture
def anyio_backend() -> str:
    """NDGRClient が使用する asyncio で非同期テストを実行する。"""

    return 'asyncio'
