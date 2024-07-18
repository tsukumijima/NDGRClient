from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel
from typing import Literal, Union


class NicoLiveProgramInfo(BaseModel):
    """
    ニコニコ生放送 (Re:仮) の視聴ページから取得した embedded-data のうち、有用そうな情報
    フィールド名は embedded-data 内の各値のキー名と同一 (そのため敢えて camelCase のままにしている)
    """
    # 生放送の番組タイトル
    title: str
    # 生放送の番組概要
    description: str
    # 生放送の放送ステータス
    status: str
    # 生放送番組のリリース時刻 (Unix タイムスタンプ)
    releaseTime: int
    # 生放送番組の公開時刻 (開始前の待機時間を含む) (Unix タイムスタンプ)
    openTime: int
    # 生放送番組の開始時刻 (Unix タイムスタンプ)
    beginTime: int
    # コメントの vpos の基準時間 (Unix タイムスタンプ)
    vposBaseTime: int
    # 生放送番組の終了時刻 (Unix タイムスタンプ)
    endTime: int
    # 生放送番組の予定終了時刻 (Unix タイムスタンプ)
    scheduledEndTime: int
    # 生放送番組のストリーミング URL
    streamContentUri: str | None
    # NDGR サーバーへの接続用 URL
    ndgrProgramCommentViewUri: str
    # NDGR サーバーへのコメント投稿用 URL
    ndgrProgramCommentPostUri: str


class NDGRComment(BaseModel):
    """
    NDGR サーバーから返される Protobuf 形式のコメントデータのうち、有用そうな情報
    フィールド名は基本的に Protobuf の各値のキー名と同一
    他にも (実際に送られてきていれば) 有用そうな情報は色々あるが、ニコニコ生放送 (Re:仮) の時点では下記情報くらいしか送信されていない
    """
    # コメント ID (?) / ex: "EhgKEgmBfWBX18SQARFaOaNDSRHkkhCy-h0"
    id: str
    # コメント日時
    at: datetime
    # 生放送 ID (?) / ex: 345479473
    live_id: int
    # 生のユーザー ID (ニコニコ生放送 (Re:仮) では現在常に 0)
    raw_user_id: int
    # ハッシュ化されたユーザー ID / ex: "i:QKQvAEkmnovz"
    hashed_user_id: str
    # アカウント状態
    account_status: Literal['Standard', 'Premium']
    # コメ番 (ニコニコ生放送 (Re:仮) ではなぜか設定されておらず、現在常に 0)
    no: int
    # vposBaseTime から起算したコメント投稿時刻の相対時間 (1/100 秒単位) / ex: 18336492
    vpos: int
    # コメントの描画位置
    position: Literal['naka', 'shita', 'ue']
    # コメントの描画サイズ
    size: Literal['medium', 'small', 'big']
    # コメントの描画色
    color: Union[
        Literal[
            'white', 'red', 'pink', 'orange', 'yellow', 'green', 'cyan', 'blue', 'purple', 'black',
            'white2', 'red2', 'pink2', 'orange2', 'yellow2', 'green2', 'cyan2', 'blue2', 'purple2', 'black2'
        ],
        NDGRCommentFullColor
    ]
    # コメントのフォントスタイル
    font: Literal['defont', 'mincho', 'gothic']
    # コメントの不透明度
    opacity: Literal['Normal', 'Translucent']
    # コメント内容
    content: str

    def __str__(self) -> str:
        return (
            f'[{self.at.strftime("%Y/%m/%d %H:%M:%S.%f")}] [white]{self.content}[/white]\n'
            f'[grey70]User: {self.hashed_user_id} | Command: {self.position} {self.size} {self.color} {self.font}[/grey70]'
        )


class NDGRCommentFullColor(BaseModel):
    r: int
    g: int
    b: int
