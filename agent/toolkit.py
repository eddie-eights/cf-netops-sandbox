"""エージェントのツール（topology.py / anomalies.py / evidence.py / proposals.py）が共有する小物。

同じコードが何本ものファイルに写してあったのを 1 か所に集めたもの。中身は 4 つ:

  runner()              TOOLS（ツール名 → 関数）から run_tool を作る。4 モジュールが同じものを持っていた
  client() / session()  boto3 のクライアントとセッションをプロセスに 1 つだけ作って使い回す
  Param                 「環境変数が先、無ければ SSM」の設定値（Neptune の接続先・Gateway の URL・Runtime の ARN）
  jst()                 epoch 秒を日本時間の文字列に（anomalies.py と proposals.py）

このファイルは 4 か所で動く。AgentCore Runtime のコンテナ（agent/Dockerfile）、tools Lambda の zip
（terraform/workflow/gateway.tf の archive_file）、Web の EC2（terraform/base/core の出力 upload_web_command）、
status Lambda の zip（terraform/pipeline/graph/sync.tf の archive_file。graph.py 経由で使う）。
agent/ のモジュールを増やしたら、この 4 か所の一覧にも足す。zip に入れ忘れると apply も plan も通ったまま、
実行時に ModuleNotFoundError で初めて分かる（tests/test_sync.py と tests/test_workflow.py が zip の中身を見ている）。
"""

import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# SSM のパラメータ名の頭（/<接頭辞>）。Terraform が環境変数で渡す。空なら SSM は引かない
PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or None
JST = timezone(timedelta(hours=9))
TTL = 60  # 設定値を SSM から引き直す間隔（秒）。まだ配備していないときに毎回叩かないため

_clients: dict = {}
_sessions: list = []


# ---------------------------------------------------------------- ツールの呼び出し
def runner(tools: dict, before=None):
    """TOOLS から run_tool(name, args) を作る。

    モデルが渡してくる toolUse の引数には仕様に無い名前が混ざることがあるので、関数が受け取れる名前だけを通す。
    before は毎回ツールの前にやること（topology だけが使う。元データを読み直す）。
    """

    def run_tool(name: str, args: dict) -> dict:
        fn = tools.get(name)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        if before is not None:
            before()
        # co_varnames は引数のあとにローカル変数も並ぶので、引数の数（co_argcount）で切る。
        # 切らないと、ローカル変数と同じ名前の余計な引数が素通りして TypeError になる
        names = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        try:
            return fn(**{k: v for k, v in (args or {}).items() if k in names})
        except (TypeError, ValueError) as e:
            return {"error": str(e)}

    return run_tool


# ---------------------------------------------------------------- boto3（作り直さない）
def client(service: str):
    """boto3 のクライアントはプロセスに 1 つだけ作る。

    1 つ目は 100 ミリ秒ほどかかり、2 つ目からは 1 ミリ秒（手元での実測）。Runtime も Lambda も一度起きたら
    使い回されるので、呼び出しのたびに作るとその分をまるごと捨てることになる。
    """
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=REGION)
    return _clients[service]


def session():
    """boto3.Session も 1 つだけ（SigV4 の署名に使う）。

    セッションは取ってきた認証情報を自分の中に持つので、使い回せば毎回の取り直しが消える。
    期限が切れたぶんの更新はセッションの中の仕組みがやる。
    """
    if not _sessions:
        _sessions.append(boto3.Session(region_name=REGION))
    return _sessions[0]


# ---------------------------------------------------------------- 配備で決まる設定値
class Param:
    """環境変数 <env> が先で、無ければ SSM の <PARAM_PREFIX>/<param> から引いて覚えておく設定値。

    Terraform が apply したときに決まるもの（Neptune の接続先・Gateway の URL・Runtime の ARN）を、
    どのモジュールも同じ手順で読むためのもの。どちらも無ければ空文字を返し、呼ぶ側はそれを見て
    「まだ配備されていない」と案内する（その機能をまだ作っていない構成でも落ちないため）。
    引けなかったときは ttl 秒のあいだ SSM を引き直さない（まだ無いものを毎回叩かない）。
    """

    def __init__(self, env: str, param: str, ttl: int = TTL):
        self.env = env
        self.param = param
        self.ttl = ttl
        self.cached = ""  # 一度引けた値。プロセスが終わるまで使う
        self.checked = 0.0  # 最後に SSM を引いた時刻

    def value(self) -> str:
        env = os.environ.get(self.env, "")
        if env:
            return env
        if self.cached or time.time() - self.checked < self.ttl or not PARAM_PREFIX:
            return self.cached
        self.checked = time.time()
        try:
            self.cached = client("ssm").get_parameter(Name=f"{PARAM_PREFIX}/{self.param}")["Parameter"]["Value"]
        except (ClientError, BotoCoreError):
            self.cached = ""
        return self.cached


# ---------------------------------------------------------------- 表示
def jst(epoch) -> str:
    """epoch 秒を日本時間の「2026-09-18 12:34:56」に。空や 0 なら空文字"""
    return datetime.fromtimestamp(int(epoch), JST).strftime("%Y-%m-%d %H:%M:%S") if epoch else ""
