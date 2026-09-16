"""ops/up.sh が Web の EC2 の上で打つ。Neptune が空なら静的トポロジ（agent/data）を入れ、空でなければ何もしない。

Web と同じ環境変数（/etc/<prefix>-web.env）と依存（/opt/<prefix>-web/lib）で動かす。GUI の「静的データを投入」と同じ関数を呼ぶ。
Neptune は出来た直後だとつながらないことがあるので、30 秒おきに 10 回まで試す。
"""
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
    sys.exit(f"SSM の {os.environ.get('PARAM_PREFIX', '')}/neptune-endpoint が読めない（terraform/graph の apply が終わっているか）")

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

if counts["devices"]:
    print(f"Neptune にはもう入っている。投入を飛ばす: {counts}")
else:
    print(f"Neptune に静的データを入れた: {graph.seed(*topology.load_static())}")
