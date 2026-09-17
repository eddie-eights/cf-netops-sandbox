"""ops/up.sh と ops/sync-graph.sh が Web の EC2 の上で打つ。lab の定義から作ったトポロジ（lab/lab_topology.py の JSON）を Neptune に入れる。

Web と同じ環境変数（/etc/<prefix>-web.env）と依存（/opt/<prefix>-web/lib）で動かす。GUI の「静的データを投入」と同じ関数（graph.seed）を呼ぶ。
Neptune は出来た直後だとつながらないことがあるので、30 秒おきに 10 回まで試す。

環境変数:
  LAB_TOPOLOGY_B64  lab/lab_topology.py の出力（{"devices": [...], "links": [...]}）を base64 にしたもの。無ければ agent/data の静的データ
  GRAPH_REPLACE     1 なら Neptune に入っていても入れ直す（lab を変えたあとの同期。動的な status は消えて全部 UP に戻る）。
                    既定は空で、Neptune が空のときだけ入れる（初期ロード）
"""
import base64
import json
import os
import sys
import time

PREFIX = "fukuda-nwc-poc"
APP = f"/opt/{PREFIX}-web"

# systemd の EnvironmentFile と同じく「名前=値」を 1 行ずつ読む（値に空白があるので source しない）
with open(f"/etc/{PREFIX}-web.env", encoding="utf-8") as f:
    for line in f:
        key, sep, value = line.rstrip("\n").partition("=")
        if sep and key and not key.startswith("#"):
            os.environ[key] = value
sys.path[:0] = [f"{APP}/src", f"{APP}/lib"]

import graph  # noqa: E402
import topology  # noqa: E402

if not graph.configured():
    sys.exit(f"SSM の {os.environ.get('PARAM_PREFIX', '')}/neptune-endpoint が読めない（terraform/pipeline/graph の apply が終わっているか）")

if os.environ.get("LAB_TOPOLOGY_B64"):
    lab = json.loads(base64.b64decode(os.environ["LAB_TOPOLOGY_B64"]))
    devices, links, source = lab["devices"], lab["links"], "lab の定義（lab/lab_topology.py）"
else:
    devices, links = topology.load_static()
    source = "静的データ（agent/data）"

last = None
for attempt in range(1, 11):
    try:
        counts = graph.count()
        break
    except Exception as e:  # 出来た直後の接続失敗や、IAM の反映待ち
        last = e
        print(f"Neptune にまだつながらない（{attempt}/10）: {e}", flush=True)
        time.sleep(30)
else:
    sys.exit(f"Neptune につながらない: {last}")

if counts["devices"] and os.environ.get("GRAPH_REPLACE") != "1":
    print(f"Neptune にはもう入っている。投入を飛ばす（入れ直すなら ops/sync-graph.sh --replace）: {counts}")
else:
    print(f"Neptune に {source} を入れた（{len(devices)} 台 / {len(links)} 本）: {graph.seed(devices, links)}")
