"""調査の証拠を取るツール（フェーズ 3。エージェントが原因分析のために OpenSearch / Prometheus / S3 Tables を見に行く）。

2026-09-17 ユーザー決定「Spark が異常を検知したら EventBridge にイベント発行して、それを検知した agent が Neptune や S3、OpenSearch、
Prometheus を見に行って原因分析」。Neptune は graph.py / topology.py（neighbors / blast_radius）、ここは残りの 3 つ:
  search_logs    OpenSearch Serverless の logs コレクション（terraform/pipeline/analytics の sinks=opensearch。Spark が traps を書く）を機器名で検索
  query_metrics  Amazon Managed Service for Prometheus（sinks=prometheus。Spark が metrics を remote write）に PromQL を投げる
  query_history  S3 Tables（Iceberg）の履歴。Athena のワークグループとカタログの接続をまだ配備していないので、案内だけ返す（未実装）

エンドポイントは環境変数 OPENSEARCH_ENDPOINT（https://...aoss.amazonaws.com）/ OPENSEARCH_INDEX（既定 snmp-logs）/
PROMETHEUS_QUERY_URL（https://aps-workspaces.<region>.amazonaws.com/workspaces/<id>/api/v1/query）。
どれも無ければ「まだ配備されていない」を返して、フェーズ 1 / 2 の構成でも落ちない。
署名は botocore の SigV4（サービス名 aoss / aps）。requests は使わず urllib で送る（tools Lambda は素の python3.13、依存を増やさない）。
tools Lambda（terraform/workflow）と chat runtime（agent/app.py）の両方から同じものが呼ばれる。
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError

OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "").rstrip("/")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "snmp-logs")
PROMETHEUS_QUERY_URL = os.environ.get("PROMETHEUS_QUERY_URL", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or "ap-northeast-1"
TIMEOUT = 20


def _signed(method: str, url: str, service: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    """SigV4 で署名して送る。応答の JSON を返し、届かない・拒否されたときは {"error": ...}"""
    headers = dict(headers or {})
    req = AWSRequest(method=method, url=url, data=body, headers=headers)
    try:
        SigV4Auth(boto3.Session().get_credentials(), service, REGION).add_auth(req)
    except (BotoCoreError, AttributeError, TypeError) as e:  # NoCredentialsError は BotoCoreError の子
        return {"error": f"署名できない（認証情報が無い）: {e}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=dict(req.headers), method=method), timeout=TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"届かない: {e}"}


def search_logs(device_id: str = "", minutes: int = 60, limit: int = 20) -> dict:
    """直近 minutes 分の traps / ログを機器名で検索（新しい順）。device_id が空なら全機器"""
    if not OPENSEARCH_ENDPOINT:
        return {"error": "ログの検索はまだ配備されていない（terraform/pipeline/analytics を sinks に opensearch を入れて apply すると使える）", "hits": []}
    minutes = max(1, min(int(minutes), 24 * 60))
    limit = max(1, min(int(limit), 100))
    since = int((time.time() - minutes * 60) * 1000)
    must = [{"range": {"@timestamp": {"gte": since}}}]
    if device_id:
        # Spark は tags.sysName / tags.agent_host / tags.source を持つ（spark/snmp_sinks.py の opensearch の文書）
        must.append({"multi_match": {"query": device_id, "fields": ["tags.sysName", "tags.agent_host", "tags.source", "device_id"]}})
    body = json.dumps({"size": limit, "sort": [{"@timestamp": "desc"}], "query": {"bool": {"must": must}}}).encode()
    res = _signed("POST", f"{OPENSEARCH_ENDPOINT}/{OPENSEARCH_INDEX}/_search", "aoss", body, {"Content-Type": "application/json"})
    if "error" in res:
        return {"error": res["error"], "hits": []}
    hits = [h.get("_source", {}) for h in (res.get("hits") or {}).get("hits", [])]
    return {"count": len(hits), "index": OPENSEARCH_INDEX, "minutes": minutes, "hits": hits}


def query_metrics(query: str, minutes: int = 15) -> dict:
    """PromQL の range query（step 60 秒）。例: interface_ifOperStatus{sysName="hq-ce-01"}"""
    if not PROMETHEUS_QUERY_URL:
        return {"error": "メトリクスの検索はまだ配備されていない（terraform/pipeline/analytics を sinks に prometheus を入れて apply すると使える）", "series": []}
    if not query:
        return {"error": "query（PromQL）が空", "series": []}
    minutes = max(1, min(int(minutes), 24 * 60))
    end = int(time.time())
    params = urllib.parse.urlencode({"query": query, "start": end - minutes * 60, "end": end, "step": "60s"})
    url = PROMETHEUS_QUERY_URL.rstrip("/")
    url = url[: -len("/query")] + "/query_range" if url.endswith("/query") else url + "/query_range"
    res = _signed("GET", f"{url}?{params}", "aps")
    if "error" in res:
        return {"error": res["error"], "series": []}
    if res.get("status") != "success":
        return {"error": f"Prometheus: {res.get('errorType', '')} {res.get('error', '')}".strip(), "series": []}
    series = []
    for r in (res.get("data") or {}).get("result", []):
        vals = r.get("values") or ([r["value"]] if "value" in r else [])
        series.append({"metric": r.get("metric", {}), "last": vals[-1][1] if vals else None, "points": len(vals),
                       "values": [[int(float(t)), v] for t, v in vals[-20:]]})
    return {"count": len(series), "minutes": minutes, "series": series}


def query_history(device_id: str = "", hours: int = 24) -> dict:
    """S3 Tables（Iceberg）に溜めた全メッセージの履歴。Athena をまだ配備していないので案内だけ（2026-09-17）"""
    return {"error": "S3 Tables の履歴の検索（Athena）はまだ配備していない。直近はメトリクスを query_metrics、ログを search_logs で見る",
            "device_id": device_id, "hours": hours, "rows": []}


TOOL_SPECS = [
    {"toolSpec": {
        "name": "search_logs",
        "description": "監視ログ（SNMP trap など）を機器名で検索する。異常の原因を調べるとき、その機器で直前に何が起きたかを見るのに使う。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "device_id": {"type": "string", "description": "機器名（例 hq-ce-01）。空なら全機器"},
            "minutes": {"type": "integer", "description": "何分前まで見るか（既定 60、最大 1440）"},
            "limit": {"type": "integer", "description": "件数の上限（既定 20、最大 100）"},
        }}},
    }},
    {"toolSpec": {
        "name": "query_metrics",
        "description": "監視メトリクス（Prometheus）に PromQL を投げる。インタフェースの状態やトラフィックの推移を見るのに使う。例: interface_ifOperStatus{sysName=\"hq-ce-01\"}",
        "inputSchema": {"json": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "PromQL（メトリクス名は <measurement>_<field>、ラベルは Telegraf のタグ）"},
            "minutes": {"type": "integer", "description": "何分前から見るか（既定 15、最大 1440）"},
        }}},
    }},
    {"toolSpec": {
        "name": "query_history",
        "description": "監視データの長期の履歴（S3 Tables）を機器名で引く。まだ配備されていないときは案内だけ返す。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "device_id": {"type": "string", "description": "機器名（例 hq-ce-01）。空なら全機器"},
            "hours": {"type": "integer", "description": "何時間前まで見るか（既定 24）"},
        }}},
    }},
]
TOOLS = {"search_logs": search_logs, "query_metrics": query_metrics, "query_history": query_history}


def run_tool(name: str, args: dict) -> dict:
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames})
    except (TypeError, ValueError) as e:
        return {"error": str(e)}
