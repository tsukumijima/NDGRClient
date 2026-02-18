#!/usr/bin/env python3

"""
n-air-app/nicolive-comment-protobuf リポジトリから .proto ファイルを取得し、
protoc で Python コードと mypy スタブを生成するスクリプト。

以下の処理を行う:
  1. GitHub API でリモートリポジトリから .proto ファイル一覧を取得
  2. 各 .proto ファイルをダウンロードし、Python 予約語フィールド名をリネーム (例: from → from_)
  3. trailing whitespace を除去した上で proto/ ディレクトリに保存
  4. protoc を実行して Python コード (.py) と mypy スタブ (.pyi) を生成
  5. 生成ファイル内の import パス書き換えと trailing whitespace 除去
"""

import keyword
import re
import shutil
import subprocess
import sys
from pathlib import Path

import niquests


# Python の予約語一覧
PYTHON_KEYWORDS = set(keyword.kwlist)

# リモートリポジトリの情報
GITHUB_REPO = 'n-air-app/nicolive-comment-protobuf'
GITHUB_BRANCH = 'main'
PROTO_DIR_IN_REPO = 'proto'

# ローカルのディレクトリパス
PROJECT_ROOT = Path(__file__).resolve().parent
PROTO_DIR = PROJECT_ROOT / 'proto'
GENERATED_DIR = PROJECT_ROOT / 'ndgr_client' / 'proto'


def strip_trailing_whitespace(content: str) -> str:
    """
    各行末の trailing whitespace を除去する。

    Args:
        content (str): 対象のファイル内容

    Returns:
        str: trailing whitespace を除去した内容
    """

    return '\n'.join(line.rstrip() for line in content.split('\n'))


def fetch_proto_file_paths() -> list[str]:
    """
    GitHub API を使ってリモートリポジトリの proto ディレクトリ内にある .proto ファイルパス一覧を取得する。

    Returns:
        list[str]: proto ディレクトリからの相対パス一覧 (例: 'dwango/nicolive/chat/data/atoms.proto')
    """

    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1'
    response = niquests.get(api_url, headers={'Accept': 'application/vnd.github.v3+json'})
    response.raise_for_status()
    tree_data = response.json()

    proto_file_paths: list[str] = []
    prefix = f'{PROTO_DIR_IN_REPO}/'
    for item in tree_data['tree']:
        # proto/ 以下の .proto ファイルのみを抽出
        if item['type'] == 'blob' and item['path'].startswith(prefix) and item['path'].endswith('.proto'):
            # 'proto/dwango/nicolive/...' から先頭の 'proto/' を除去して相対パスにする
            relative_path = item['path'][len(prefix) :]
            proto_file_paths.append(relative_path)

    return sorted(proto_file_paths)


def download_proto_file(relative_path: str) -> str:
    """
    GitHub の raw コンテンツ URL から .proto ファイルの内容をダウンロードする。

    Args:
        relative_path (str): proto ディレクトリからの相対パス (例: 'dwango/nicolive/chat/data/atoms.proto')

    Returns:
        str: ダウンロードしたファイルの内容
    """

    raw_url = f'https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{PROTO_DIR_IN_REPO}/{relative_path}'
    response = niquests.get(raw_url)
    response.raise_for_status()
    if response.text is None:
        raise RuntimeError(f'Failed to download proto file: {relative_path}')
    return response.text


def rename_reserved_field_names(proto_files: dict[str, str]) -> dict[str, str]:
    """
    proto ファイル内のフィールド名が Python の予約語と衝突する場合、
    フィールド名末尾に _ を付与してリネームし、説明コメントを追加する。
    protobuf ではフィールド番号でシリアライズされるため、フィールド名の変更はバイナリ互換性に影響しない。

    Args:
        proto_files (dict[str, str]): 相対パスをキー、ファイル内容を値とする辞書

    Returns:
        dict[str, str]: リネーム処理後の辞書
    """

    # Python 予約語をパイプ区切りで結合 (長い語から並べて最長一致を保証)
    keywords_alternation = '|'.join(re.escape(kw) for kw in sorted(PYTHON_KEYWORDS, key=len, reverse=True))

    # proto フィールド宣言にマッチする正規表現
    ## 例: "  google.protobuf.Timestamp from = 1;"
    ##      "  optional string import = 2;"
    ##      "  map<string, int32> class = 3;"
    field_pattern = re.compile(
        r'^(\s*'  # インデント
        r'(?:optional\s+|repeated\s+)?'  # optional/repeated 修飾子
        r'(?:map<[^>]+>\s+|\S+\s+))'  # 型名 + スペース (map 型を含む)
        r'(' + keywords_alternation + r')'  # Python 予約語のフィールド名
        r'(\s*=\s*\d+\s*;.*)'  # " = <番号>;" 以降
        r'$',
    )

    patched_files: dict[str, str] = {}
    for relative_path, content in proto_files.items():
        lines = content.split('\n')
        new_lines: list[str] = []
        is_modified = False

        for line in lines:
            match = field_pattern.match(line)
            if match is not None:
                prefix = match.group(1)  # "  google.protobuf.Timestamp "
                field_name = match.group(2)  # "from"
                suffix = match.group(3)  # " = 1;"

                # インデントを取得してコメント行を生成
                indent = re.match(r'(\s*)', line)
                assert indent is not None
                indent_str = indent.group(1)
                comment_line = (
                    f'{indent_str}// Python では {field_name} が予約語のため {field_name}_ にリネームしている'
                )
                renamed_line = f'{prefix}{field_name}_{suffix}'

                new_lines.append(comment_line)
                new_lines.append(renamed_line)
                is_modified = True
                print(f'  Renamed reserved field: {relative_path}: {field_name} -> {field_name}_')
            else:
                new_lines.append(line)

        if is_modified is True:
            patched_files[relative_path] = '\n'.join(new_lines)
        else:
            patched_files[relative_path] = content

    return patched_files


def save_proto_files(proto_files: dict[str, str]) -> None:
    """
    ダウンロードした .proto ファイルをローカルの proto/ ディレクトリに保存する。
    既存の proto/dwango/ ディレクトリを一旦削除してからファイルを配置する。

    Args:
        proto_files (dict[str, str]): 相対パスをキー、ファイル内容を値とする辞書
    """

    # 既存の dwango/ ディレクトリを削除して、リモートの状態と完全に同期する
    dwango_dir = PROTO_DIR / 'dwango'
    if dwango_dir.exists():
        shutil.rmtree(dwango_dir)

    for relative_path, content in proto_files.items():
        file_path = PROTO_DIR / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # trailing whitespace を除去してから保存
        file_path.write_text(strip_trailing_whitespace(content), encoding='utf-8')
        print(f'  Saved: {relative_path}')


def generate_proto_code() -> None:
    """
    protoc を使って proto/ ディレクトリ内の .proto ファイルから Python コードと mypy スタブを生成する。
    生成後、import パスを ndgr_client.proto パッケージ配下に書き換える。
    """

    # 生成先の dwango/ ディレクトリを削除してクリーンな状態にする
    generated_dwango_dir = GENERATED_DIR / 'dwango'
    if generated_dwango_dir.exists():
        shutil.rmtree(generated_dwango_dir)

    # proto ファイルの一覧を取得
    proto_files = sorted(PROTO_DIR.rglob('*.proto'))
    if len(proto_files) == 0:
        print('Error: No .proto files found in proto/ directory.')
        sys.exit(1)

    # protoc コマンドを実行して Python コードと mypy スタブを生成
    protoc_command = [
        'protoc',
        f'--proto_path={PROTO_DIR}',
        f'--python_out={GENERATED_DIR}',
        f'--mypy_out={GENERATED_DIR}',
    ] + [str(proto_file) for proto_file in proto_files]

    print('  Running protoc...')
    result = subprocess.run(protoc_command, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'Error: protoc failed with return code: {result.returncode}')
        if result.stderr:
            print(result.stderr)
        sys.exit(1)
    print('  protoc completed successfully.')

    # 生成されたファイルの後処理 (import パス書き換え + trailing whitespace 除去)
    ## ' dwango.nicolive' を ' ndgr_client.proto.dwango.nicolive' に一括置換することで、
    ## from dwango.nicolive.xxx import ... のような import 文が
    ## from ndgr_client.proto.dwango.nicolive.xxx import ... に変換される
    generated_files = sorted(list(GENERATED_DIR.rglob('*.py')) + list(GENERATED_DIR.rglob('*.pyi')))
    for file_path in generated_files:
        content = file_path.read_text(encoding='utf-8')
        new_content = content.replace(' dwango.nicolive', ' ndgr_client.proto.dwango.nicolive')
        new_content = strip_trailing_whitespace(new_content)
        if content != new_content:
            file_path.write_text(new_content, encoding='utf-8')
            relative_path = file_path.relative_to(PROJECT_ROOT)
            print(f'  Post-processed: {relative_path}')


def main() -> None:
    """
    メイン処理: .proto ファイルのリモートからの取得と Python コード生成を実行する。
    """

    print(f'Fetching proto file list from {GITHUB_REPO} ({GITHUB_BRANCH} branch)...')
    proto_file_paths = fetch_proto_file_paths()
    print(f'  Found {len(proto_file_paths)} proto files.')

    print('Downloading proto files...')
    proto_files: dict[str, str] = {}
    for relative_path in proto_file_paths:
        proto_files[relative_path] = download_proto_file(relative_path)
        print(f'  Downloaded: {relative_path}')

    print('Renaming Python reserved word field names in proto files...')
    proto_files = rename_reserved_field_names(proto_files)

    print('Saving proto files to proto/ directory...')
    save_proto_files(proto_files)

    print('Generating Python code from proto files...')
    generate_proto_code()

    print('All done!')


if __name__ == '__main__':
    main()
