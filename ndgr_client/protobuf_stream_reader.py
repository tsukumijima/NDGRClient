
from rich import print


class ProtobufStreamReader:
    """
    NDGR コメントサーバーの Protocol Buffer ストリームを読み取るためのクラス
    下記コードを Python に移植したもの (どういうロジックなのかはよくわかってない)
    ref: https://github.com/rinsuki-lab/ndgr-reader/blob/main/src/protobuf-stream-reader.ts
    """


    def __init__(self):
        """
        ProtobufStreamReader のコンストラクタ
        """

        self.buffer = bytearray()


    def addNewChunk(self, chunk: bytes) -> None:
        """
        新しいチャンクデータを内部バッファに追加する

        Args:
            chunk (bytes): 追加するバイト列
        """

        self.buffer.extend(chunk)


    def __readVarInt(self) -> tuple[int, int] | None:
        """
        バッファから可変長整数 (varint) を読み取る

        Returns:
            tuple[int, int] | None: (オフセット, 結果の整数値) のタプル (データが不足している場合は None を返す)
        """

        offset = 0
        result = 0
        i = 0
        while True:
            if offset >= len(self.buffer):
                return None  # データが不足している場合

            current = self.buffer[offset]
            result |= (current & 0x7F) << i
            offset += 1
            i += 7
            if not (current & 0x80):
                break

        return offset, result


    def unshiftChunk(self) -> bytes | None:
        """
        バッファから次のメッセージを抽出する

        Returns:
            bytes | None: 抽出されたメッセージ (データが不足している場合は None を返す)
        """

        varint_result = self.__readVarInt()
        if varint_result is None:
            return None

        offset, varint = varint_result
        if offset + varint > len(self.buffer):
            # データが不足している場合
            print(f'needs {offset + varint} bytes, but only {len(self.buffer)} bytes')
            return None

        message = bytes(self.buffer[offset:offset + varint])
        del self.buffer[:offset + varint]
        return message
