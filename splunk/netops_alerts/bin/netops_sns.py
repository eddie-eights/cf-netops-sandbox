"""Splunk のアラートアクション netops_sns。保存済みサーチ（default/savedsearches.conf）の結果を SNS のトピックへ publish する。

Splunk は `python netops_sns.py --execute` で起こし、標準入力に JSON（results_file = 結果の CSV（gzip）の場所 など）を渡す。
結果の 1 行 = アラート 1 件で、列は device / kind / target / status（firing | resolved）/ detail / starts_at（epoch 秒）。
publish する JSON は Grafana（grafana/provisioning/alerting）と同じ形で、workflow/rules.py の alerts_from_message と graph/status_handler.py が読む:
  {"source": "splunk", "alerts": [{"status", "device_id", "kind", "target", "detail", "starts_at"}, …]}

- 機器名: gNMI と trap のイベントは機器名でなく管理 IP（tags.source）を持つ。DEVICE_MAP（別名=機器名,…）で名前に直す
- 認証: ECS のタスクロール（AWS_CONTAINER_CREDENTIALS_RELATIVE_URI から一時的な認証情報を取り、SigV4 で署名する）。アクセスキーは置かない
- 設定: コンテナの環境変数を splunk/entrypoint.sh がファイルに写したもの（splunkd の子プロセスはコンテナの環境変数を引き継がない）
- ライブラリ: 標準ライブラリだけ（Splunk の Python に boto3 は無い。VPC から PyPI へも出られない）

失敗は stderr に "ERROR …" で書いて 0 以外で終わる（Splunk が splunkd.log の sendmodalert に残す）。Splunk は打ち直さないので、ここで 3 回まで試す
"""
import csv
import datetime
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = "/opt/container_artifact/nwc-alerts.env"   # splunk/entrypoint.sh が書く
ENV_KEYS = ("AWS_REGION", "ALERTS_TOPIC_ARN", "DEVICE_MAP", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_ENDPOINT_URL_SNS")
ECS_CREDENTIALS_HOST = "http://169.254.170.2"   # ECS のタスクの認証情報の口（タスクの中からだけ届く）
STATUSES = ("firing", "resolved")
MAX_ALERTS = 50      # 1 通に入れる件数（SNS の本文は 256 KB まで。1 件は数百バイト）
ATTEMPTS = 3
TIMEOUT = 10
SUBJECT = "netops alert"
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# プロキシの環境変数は見ない（認証情報の口は 169.254.170.2、SNS は VPC のエンドポイント）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(level, text):
    sys.stderr.write(f"{level} {text}\n")


def load_env(path=ENV_FILE, environ=None):
    """設定を読む。ファイル（KEY=VALUE の行。値が空の行は無いのと同じ）が土台で、プロセスの環境変数に同じ名前があればそちら"""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.rstrip("\n").partition("=")
                if sep and k in ENV_KEYS and v:
                    env[k] = v
    except OSError:
        pass
    for k in ENV_KEYS:
        v = (os.environ if environ is None else environ).get(k)
        if v:
            env[k] = v
    return env


def parse_device_map(text):
    """"203.0.113.31=dc1-leaf-01,dc1-leaf-01.example.net=dc1-leaf-01" → {別名（小文字）: 機器名}。= の無い要素は捨てる"""
    out = {}
    for p in (text or "").split(","):
        k, sep, v = p.partition("=")
        if sep and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


def device_name(name, devmap):
    """機器名をトポロジの device_id に揃える。小文字にして device map を引き、無ければドメインを落とす（IPv4 はそのまま）"""
    name = str(name or "").strip().lower()
    if not name:
        return ""
    short = name if IPV4_RE.match(name) else name.split(".", 1)[0]
    return devmap.get(name) or devmap.get(short) or short


def _epoch(v):
    try:
        return max(int(float(v or 0)), 0)
    except (TypeError, ValueError):
        return 0


def alerts_from_rows(rows, devmap):
    """サーチの結果の行 → アラートの list。機器か種類が無い行、status が firing / resolved でない行は捨てる"""
    out = []
    for r in rows:
        dev, kind = device_name(r.get("device"), devmap), str(r.get("kind") or "").strip()
        status = str(r.get("status") or "").strip().lower()
        if not dev or not kind or status not in STATUSES:
            continue
        out.append({"status": status, "device_id": dev, "kind": kind, "target": str(r.get("target") or "").strip(),
                    "detail": str(r.get("detail") or "")[:1000], "starts_at": _epoch(r.get("starts_at"))})
    return out


def messages(alerts, size=MAX_ALERTS):
    """publish する本文の list（MAX_ALERTS 件ごとに 1 通）"""
    return [json.dumps({"source": "splunk", "alerts": alerts[i:i + size]}, ensure_ascii=False, separators=(",", ":"))
            for i in range(0, len(alerts), size)]


def read_rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def credentials(env):
    """タスクロールの一時的な認証情報 (access key id, secret, token)。値はログに出さない"""
    rel, full = env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"), env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    url = (ECS_CREDENTIALS_HOST + rel) if rel else full
    if not url:
        raise RuntimeError("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI が無い（ECS のタスクロールが付いていない）")
    with OPENER.open(url, timeout=TIMEOUT) as res:
        d = json.load(res)
    return d["AccessKeyId"], d["SecretAccessKey"], d.get("Token") or ""


def topic_region(topic_arn, default=""):
    """arn:aws:sns:<region>:<account>:<name> の region"""
    parts = (topic_arn or "").split(":")
    return parts[3] if len(parts) >= 6 and parts[3] else default


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def signed_headers(url, body, region, creds, now=None, service="sns"):
    """SigV4 で署名した POST のヘッダー（body は bytes）。now は UTC の datetime（テスト用）"""
    key_id, secret, token = creds
    u = urllib.parse.urlsplit(url)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8", "host": u.netloc, "x-amz-date": amz_date}
    if token:
        headers["x-amz-security-token"] = token
    names = ";".join(sorted(headers))
    canonical = "\n".join(["POST", u.path or "/", u.query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), names,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={names}, Signature={signature}"
    return headers


def publish(env, creds, text):
    """SNS の Publish（Query API）。2xx 以外と通信の失敗は例外"""
    topic = env["ALERTS_TOPIC_ARN"]
    region = topic_region(topic, env.get("AWS_REGION", ""))
    url = env.get("AWS_ENDPOINT_URL_SNS") or f"https://sns.{region}.amazonaws.com/"
    body = urllib.parse.urlencode({"Action": "Publish", "Version": "2010-03-31", "TopicArn": topic, "Subject": SUBJECT, "Message": text}).encode()
    req = urllib.request.Request(url, data=body, headers=signed_headers(url, body, region, creds), method="POST")
    with OPENER.open(req, timeout=TIMEOUT) as res:
        res.read()


def send(env, texts, sleep=time.sleep):
    """本文を順に publish する。失敗したら認証情報を取り直して ATTEMPTS 回まで試す。送れた通数を返す"""
    sent = 0
    for text in texts:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                publish(env, credentials(env), text)
                sent += 1
                break
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code} {e.read()[:300]!r}"
            except (urllib.error.URLError, OSError, KeyError, ValueError, RuntimeError) as e:
                err = f"{type(e).__name__}: {e}"
            log("ERROR", f"publish に失敗した（{attempt}/{ATTEMPTS}）: {err}")
            if attempt < ATTEMPTS:
                sleep(attempt)
    return sent


def main(argv, stdin):
    if len(argv) < 2 or argv[1] != "--execute":
        log("FATAL", "Unsupported execution mode (expected --execute flag)")
        return 1
    try:
        payload = json.loads(stdin.read())
        env = load_env()
        if not env.get("ALERTS_TOPIC_ARN"):
            log("ERROR", f"ALERTS_TOPIC_ARN が無い（{ENV_FILE} とコンテナの環境変数）")
            return 2
        rows = read_rows(payload["results_file"])
        alerts = alerts_from_rows(rows, parse_device_map(env.get("DEVICE_MAP", "")))
        texts = messages(alerts)
        sent = send(env, texts)
        log("INFO", f"search={payload.get('search_name')} rows={len(rows)} alerts={len(alerts)} published={sent}/{len(texts)}")
        return 0 if sent == len(texts) else 2
    except Exception as e:   # Splunk に traceback を流さない（1 行で残す）
        log("ERROR", f"Unexpected error: {type(e).__name__}: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
