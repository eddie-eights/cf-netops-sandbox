"""Web の下ごしらえ。環境変数を読み、エージェントのモジュールを import できる状態にする。

web/ の他のモジュール（chat.py / topology_view.py / incident_view.py / app.py）は、
agent/ のモジュール（topology・anomalies・graph・proposals・toolkit）より先にこのファイルを import する。
sys.path と TOPOLOGY_DATA_DIR をここで決めているので、順番が逆だと静的データの場所が変わる。
"""

import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env_file() -> str:
    """手元で動かすときの .env（展開したフォルダ直下。ENV_FILE=<パス> で変えられる。並びは .env.example）。
    EC2 では systemd の EnvironmentFile が渡すので無くてよい。既にある環境変数は上書きしない。同じ名前は後の行が勝つ。
    読めたらそのパスを返す"""
    path = os.environ.get("ENV_FILE") or os.path.join(HERE, "..", ".env")
    if not os.path.isfile(path):
        return ""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and v:
                values[k] = v
    for k, v in values.items():
        os.environ.setdefault(k, v)
    return os.path.abspath(path)


ENV_FILE = load_env_file()
if not os.environ.get("AWS_REGION"):
    sys.exit("environment variable AWS_REGION is not set. "
             "EC2: /etc/<prefix>-web.env is written by the user_data of terraform/base/core (compare with .env.example). "
             "local: cp .env.example .env and fill it in (docs/development.md)")
REGION = os.environ["AWS_REGION"]
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
if not os.path.isabs(DATA_DIR):  # 相対パスは展開したフォルダ直下から（.env.example の DATA_DIR=agent/data）
    DATA_DIR = os.path.normpath(os.path.join(HERE, "..", DATA_DIR))
TITLE = os.environ.get("TITLE", "NetOps PoC")
MAX_PROMPT = 4000

# エージェントと同じモジュール。EC2 では同じディレクトリに置く（upload_web_command）。手元では agent/ から読む。
# 静的データの場所だけ DATA_DIR に合わせる
if not os.path.isfile(os.path.join(HERE, "topology.py")):
    sys.path.append(os.path.join(HERE, "..", "agent"))
os.environ["TOPOLOGY_DATA_DIR"] = DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("web")
