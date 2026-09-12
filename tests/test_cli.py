from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ndgr_client.__main__ import app
from ndgr_client.niconico_oauth import NiconicoOAuthError, NiconicoProgramEndedError


@pytest.mark.parametrize(
    ('target', 'error', 'should_skip'),
    [
        ('all', NiconicoProgramEndedError('PROGRAM_ENDED'), True),
        ('jk1', NiconicoProgramEndedError('PROGRAM_ENDED'), False),
        ('all', NiconicoOAuthError('Authentication failed'), False),
    ],
)
def test_download_skips_only_unavailable_programs_in_all_mode(
    tmp_path: Path, target: str, error: ValueError, should_skip: bool
) -> None:
    """全件取得で番組が取得対象外になった場合だけ次へ進み、個別指定や認証失敗はエラーになることを確認する。"""

    # 最初のチャンネルで指定した例外を送出し、次のチャンネルでは0件の取得に成功する状況を再現する
    with patch('ndgr_client.__main__.NDGRClient') as client_class:
        client_class.JIKKYO_CHANNEL_ID_MAP = {'jk1': 'ch1', 'jk2': 'ch2'}
        client = client_class.return_value.__aenter__.return_value
        client.downloadBackwardComments = AsyncMock(side_effect=[error, []])
        client_class.convertToXMLString.return_value = ''
        result = CliRunner().invoke(app, ['download', target, '--output-dir', str(tmp_path)])

    # スキップ時は出力を作らず、次のチャンネルの取得と保存まで完了することを確認する
    if should_skip is True:
        assert result.exit_code == 0
        assert 'jk1: skipped (PROGRAM_ENDED)' in result.output
        assert 'jk2:     0 comments' in result.output
        assert client.downloadBackwardComments.await_count == 2
        assert sorted(path.name for path in tmp_path.iterdir()) == ['jk2.nicojk']
    else:
        assert result.exit_code != 0
        assert result.exception is error
        assert client.downloadBackwardComments.await_count == 1
        assert list(tmp_path.iterdir()) == []
