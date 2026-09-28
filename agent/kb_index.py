"""Knowledge Base のベクトル索引を作る Lambda（terraform/agent/kb.tf の aws_lambda_invocation.kb_index が apply のときに 1 回呼ぶ）。

コレクションは VPC エンドポイントからしか届かない（ネットワークポリシーが AllowFromPublic = false）ので、
Terraform を打つ PC からは索引を作れない。この Lambda は VPC の中（土台の internal SG）で動き、
自分のロール（データアクセスポリシーでは CreateIndex / DescribeIndex だけ）で SigV4 に署名して PUT する。

入力: {"endpoint": "https://<id>.<region>.aoss.amazonaws.com", "index": "kb-index", "body": {settings と mappings}}
索引がもうあれば作らずに返す（mappings が違っていても直さない。変えるときはコレクションごと作り直す）。
データアクセスポリシーの反映は作成から 1 分ほど遅れるので、403 と届かない間は 10 秒おきに打ち直す（最大で timeout まで）。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
RETRY_SECONDS = 10


def _call(method, url, body=None):
    """SigV4（サービス名 aoss）で署名して送り、(status, 本文) を返す。4xx / 5xx も例外にしない"""
    data = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    # aoss は本文の SHA-256 を x-amz-content-sha256 で要る（署名の対象にも入れる）
    headers["x-amz-content-sha256"] = hashlib.sha256(data).hexdigest()
    req = AWSRequest(method=method, url=url, data=data or None, headers=headers)
    SigV4Auth(boto3.Session().get_credentials(), "aoss", REGION).add_auth(req)
    r = urllib.request.Request(url, data=data or None, method=method, headers=dict(req.headers.items()))
    try:
        with urllib.request.urlopen(r, timeout=30) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, TimeoutError) as e:  # DNS（private hosted zone）や経路がまだのとき。0 で返して打ち直させる
        return 0, str(e)


def handler(event, context, sleep=time.sleep):
    url = f"{event['endpoint'].rstrip('/')}/{event['index']}"
    while True:
        status, text = _call("HEAD", url)
        if status == 200:
            return {"index": event["index"], "created": False}
        if status == 404:
            status, text = _call("PUT", url, event["body"])
            if status == 200:
                return {"index": event["index"], "created": True}
            # 別の呼び出しが先に作った（HEAD と PUT の間）
            if status == 400 and "resource_already_exists_exception" in text:
                return {"index": event["index"], "created": False}
        # 403 はデータアクセスポリシーの反映待ち、0 は届かない。残り時間が次の 1 回分を切ったらあきらめる
        if status not in (0, 403) or context.get_remaining_time_in_millis() < (RETRY_SECONDS + 35) * 1000:
            raise RuntimeError(f"{url}: {status} {text[:500]}")
        print(f"{status} {text[:200]}（反映待ち）。{RETRY_SECONDS} 秒後に打ち直す")
        sleep(RETRY_SECONDS)
