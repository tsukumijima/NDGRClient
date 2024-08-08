from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel
from typing import Literal, Union


class NicoLiveProgramInfo(BaseModel):
    """
    ニコニコ生放送の視聴ページから取得した embedded-data のうち、有用そうな情報
    フィールド名は embedded-data 内の各値のキー名と同一 (そのため敢えて camelCase のままにしている)
    """
    # 生放送の番組 ID (ex: lv345479473)
    nicoliveProgramId: str
    # 生放送の番組タイトル
    title: str
    # 生放送の番組概要
    description: str
    # 生放送の放送ステータス
    status: str
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
    # WebSocket サーバーへの接続用 URL
    webSocketUrl: str


class NDGRComment(BaseModel):
    """
    NDGR サーバーから返される Protobuf 形式のコメントデータのうち、有用そうな情報
    フィールド名は基本的に Protobuf の各値のキー名と同一
    """
    # コメント ID (?) / ex: "EhgKEgmBfWBX18SQARFaOaNDSRHkkhCy-h0"
    id: str
    # コメント日時
    at: datetime
    # 生放送 ID (?) / ex: 345479473
    live_id: int
    # 生のユーザー ID
    ## 184 コメントでは 0 (未設定) になる
    raw_user_id: int
    # ハッシュ化されたユーザー ID / ex: "a:QKQvAEkmnovz"
    hashed_user_id: str
    # アカウント状態
    account_status: Literal['Standard', 'Premium']
    # コメ番
    ## 従来のコメ番とは異なり、ベストエフォートでの採番となっており連番性・ユニーク性は一切保証されていないため注意
    ## 「おおむねうまく値がセットされる想定ですが、それはたまたまそうなっているという前提のもと、割り切った用途でご利用ください」とのこと
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
        # 生のユーザー ID が 0 より上だったら生のユーザー ID を、そうでなければ匿名化されたユーザー ID を表示する
        user_id = self.raw_user_id if self.raw_user_id > 0 else self.hashed_user_id
        return (
            f'[{self.at.strftime("%Y/%m/%d %H:%M:%S.%f")}][No:{self.no}] [white]{self.content}[/white]\n'
            f'[grey70]User: {user_id} | Command: {self.position} {self.size} {self.color} {self.font}[/grey70]'
        )


class NDGRCommentFullColor(BaseModel):
    r: int
    g: int
    b: int


class XMLCompatibleComment(BaseModel):
    """
    ニコニコ旧来の XML 互換コメントデータ
    """
    # コメントのスレッド ID
    thread: str
    # コメント番号（コメ番）
    no: int
    # スレッド ID から起算したコメントの再生位置（1/100秒）
    vpos: int
    # コメント投稿時間の UNIX タイムスタンプ
    date: int
    # コメント投稿時間の小数点以下の時間 (6桁)
    date_usec: int
    # ユーザー ID（コマンドに 184 が指定されている場合は匿名化される）
    user_id: str
    # コメントのコマンド（184, red naka big など）
    mail: str
    # コメントしたユーザーがプレミアム会員であれば 1
    premium: Literal[1] | None = None
    # コメントしたユーザーが匿名化されていれば 1
    anonymity: Literal[1] | None = None
    # コメント内容
    content: str
