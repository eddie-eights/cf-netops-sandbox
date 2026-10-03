"""アラートの送り手の模擬テスト（AWS にも Splunk にも Grafana にも触れない）。
Splunk のアラートアクション（splunk/netops_alerts/bin/netops_sns.py）が保存済みサーチの結果を SNS の本文にして SigV4 で publish すること、
保存済みサーチ（default/savedsearches.conf）と Grafana のアラート（grafana/provisioning/alerting/netops.yaml）が同じ形の本文を出し、
受け手（workflow/rules.py の alerts_from_message）がそのまま読めること、SNS のトピック（terraform/base/core の alerts.tf）と
イメージ（splunk/Dockerfile・entrypoint.sh、grafana/start.sh）と ops/up.sh・ops/check.sh がその配線を持つこと。
受け手の側は tests/test_workflow.py（SQS → ワークフロー）と tests/test_sync.py（Lambda → Neptune の status）。
実行は uv run --group dev python tests/test_alerts.py（依存は標準ライブラリだけ。botocore があれば署名を突き合わせる）"""
import ast, contextlib, csv, datetime, glob, gzip, importlib.util, io, json, os, re, subprocess, sys, tempfile, urllib.error, urllib.parse

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "workflow"))
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


ACTION = "splunk/netops_alerts/bin/netops_sns.py"
sns = load(ACTION, "netops_sns")
import rules   # noqa: E402  受け手（workflow/rules.py）
src = read(ACTION)
KEYS = ["status", "device_id", "kind", "target", "detail", "starts_at"]
TOPIC = "arn:aws:sns:ap-northeast-1:111122223333:netops-alerts"
CREDS = ("AKIDTEST", "test-secret", "test-token")   # 偽の値（署名の計算を確かめるためだけ）

# ---- アラートアクション: 行 → アラート
check("アラートアクションは標準ライブラリだけで動く（Splunk の Python に boto3 は無い）",
      {n.split(".")[0] for node in ast.walk(ast.parse(src)) if isinstance(node, (ast.Import, ast.ImportFrom))
       for n in ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module])} <= set(sys.stdlib_module_names))
devmap = sns.parse_device_map(" 203.0.113.31=dc1-leaf-01 ,DC1-Leaf-02.Example.Net=dc1-leaf-02,壊れた要素,=x,y=,203.0.113.101=")
check("device map は「別名=機器名」をカンマで並べたもの。別名は小文字にし、= の無い要素と片方が空の要素は捨てる",
      devmap == {"203.0.113.31": "dc1-leaf-01", "dc1-leaf-02.example.net": "dc1-leaf-02"} and sns.parse_device_map("") == {} and sns.parse_device_map(None) == {})
check("機器名は device map を引き、無ければ小文字にしてドメインを落とす（IPv4 は落とさない）",
      [sns.device_name(n, devmap) for n in ("203.0.113.31", "DC1-LEAF-02.example.net", "Dc1-Spine-01.lab.local", "198.51.100.7", " dc1-leaf-01 ", "", None)]
      == ["dc1-leaf-01", "dc1-leaf-02", "dc1-spine-01", "198.51.100.7", "dc1-leaf-01", "", ""])
check("受け手（rules.device_name）も同じ規則で揃える（device map を通ったあとの名前はそのまま通る）",
      all(rules.device_name(n) == sns.device_name(n, {}) for n in ("DC1-LEAF-02.example.net", "198.51.100.7", "dc1-leaf-01", "")))
check("starts_at は epoch 秒の整数にする（小数は切り捨て、読めない値と負の値は 0）",
      [sns._epoch(v) for v in ("1790000000.9", 1790000000, "", None, "x", "-5")] == [1790000000, 1790000000, 0, 0, 0, 0])
ROWS = [
    {"device": "203.0.113.31", "kind": "bgp_down", "target": "10.255.0.1", "status": "firing", "detail": "bgp session to 10.255.0.1 is active (splunk)", "starts_at": "1790000000.5"},
    {"device": "DC1-SPINE-01.lab", "kind": "link_down", "target": " ethernet-1/1 ", "status": "RESOLVED", "detail": "", "starts_at": ""},
    {"device": "", "kind": "trap", "target": ".1.3", "status": "firing"},
    {"device": "dc1-leaf-01", "kind": "", "target": "x", "status": "firing"},
    {"device": "dc1-leaf-01", "kind": "trap", "target": ".1.3", "status": "pending"},
    {"device": "dc1-leaf-01", "kind": "trap", "status": "firing", "detail": "x" * 3000},
]
alerts = sns.alerts_from_rows(ROWS, devmap)
check("結果の 1 行 = アラート 1 件。機器か種類が無い行と、status が firing / resolved でない行は捨てる",
      [(a["device_id"], a["kind"], a["target"], a["status"], a["starts_at"]) for a in alerts]
      == [("dc1-leaf-01", "bgp_down", "10.255.0.1", "firing", 1790000000), ("dc1-spine-01", "link_down", "ethernet-1/1", "resolved", 0), ("dc1-leaf-01", "trap", "", "firing", 0)])
check("アラートの項目は 6 つで、detail は 1000 字で切る", all(list(a) == KEYS for a in alerts) and len(alerts[2]["detail"]) == 1000)
texts = sns.messages(alerts)
check("本文は {\"source\": \"splunk\", \"alerts\": […]} の JSON 1 通（空白を入れない）",
      len(texts) == 1 and json.loads(texts[0]) == {"source": "splunk", "alerts": alerts} and ", " not in texts[0].replace(alerts[0]["detail"], ""))
many = sns.messages([dict(alerts[0], target=f"10.0.0.{i}") for i in range(120)])
check("50 件ごとに 1 通に分ける（SNS の本文は 256 KB まで）。0 件なら何も送らない",
      [len(json.loads(t)["alerts"]) for t in many] == [50, 50, 20] and sns.messages([]) == [] and sns.MAX_ALERTS == 50
      and max(len(t.encode()) for t in sns.messages([dict(alerts[2], target=f"t{i}") for i in range(50)])) < 256 * 1024)
got = rules.alerts_from_message(texts[0], now=1790000123)
check("受け手（rules.alerts_from_message）がそのまま読める: anomaly_id は <機器>#<種類>#<対象>、starts_at が無ければ受けた時刻",
      [(a["anomaly_id"], a["status"], a["first_seen"], a["source"]) for a in got]
      == [("dc1-leaf-01#bgp_down#10.255.0.1", "firing", 1790000000, "splunk"), ("dc1-spine-01#link_down#ethernet-1/1", "resolved", 1790000123, "splunk"),
          ("dc1-leaf-01#trap#", "firing", 1790000123, "splunk")])
with tempfile.TemporaryDirectory() as tmp:
    RESULTS = os.path.join(tmp, "results.csv.gz")
    with gzip.open(RESULTS, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["device", "kind", "target", "status", "detail", "starts_at", "__mv_device"])
        w.writeheader()
        w.writerow(dict(ROWS[0], __mv_device=""))
        w.writerow({"device": "dc1-leaf-02", "kind": "trap", "target": ".1.3.6.1.4.1.9.9.41.2.0.1", "status": "resolved", "detail": "改行\nとカンマ, を含む", "starts_at": "1790000060"})
    rows = sns.read_rows(RESULTS)
    check("結果のファイル（gzip の CSV。Splunk が results_file で渡す）を行の dict にする（余分な列は無視、改行とカンマ入りの値も読む）",
          len(rows) == 2 and rows[0]["device"] == "203.0.113.31" and rows[1]["detail"] == "改行\nとカンマ, を含む"
          and [a["device_id"] for a in sns.alerts_from_rows(rows, devmap)] == ["dc1-leaf-01", "dc1-leaf-02"])

    # ---- 設定（entrypoint.sh が書くファイル）
    ep = read("splunk", "entrypoint.sh")
    check("entrypoint.sh がファイルに写す環境変数は、アラートアクションが読むもの（ENV_KEYS）と同じ並び",
          tuple(re.search(r"\nfor k in ([A-Z_ ]+); do\n", ep).group(1).split()) == sns.ENV_KEYS
          and re.search(r'ENV_FILE="\$\{NETOPS_ALERTS_ENV:-([^}]+)\}"', ep).group(1) == sns.ENV_FILE)
    check("entrypoint.sh は写したあと上流の入口へ exec する（引数はそのまま）、ファイルは splunk ユーザーが読める（umask 022）",
          ep.rstrip().endswith('exec /sbin/entrypoint.sh "$@"') and "umask 022" in ep and "set -eu" in ep)
    check("認証情報そのもの（AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN）はファイルに写さない",
          not any(k in ep or k in sns.ENV_KEYS for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")))
    ENVF = os.path.join(tmp, "netops-alerts.env")
    DM = "203.0.113.31=dc1-leaf-01,10.255.2.2=dc1-leaf-02"
    stub = os.path.join(tmp, "entrypoint.sh")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(ep.replace('exec /sbin/entrypoint.sh "$@"', 'echo "upstream $*"'))
    r = subprocess.run(["bash", stub, "start-service"], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "NETOPS_ALERTS_ENV": ENVF, "AWS_REGION": "ap-northeast-1", "ALERTS_TOPIC_ARN": TOPIC, "DEVICE_MAP": DM,
                            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc", "SPLUNK_PASSWORD": "must-not-be-copied"})
    written = read(ENVF) if os.path.exists(ENVF) else ""
    check("entrypoint.sh を実際に走らせる: 6 行の KEY=VALUE を書き、他の環境変数（SPLUNK_PASSWORD など）は写さない",
          r.returncode == 0 and r.stdout.strip() == "upstream start-service" and [l.split("=", 1)[0] for l in written.splitlines()] == list(sns.ENV_KEYS)
          and "must-not-be-copied" not in written)
    env = sns.load_env(ENVF, environ={})
    check("load_env はそのファイルを読む（値の中の = とカンマはそのまま、空の値は入れない）",
          env == {"AWS_REGION": "ap-northeast-1", "ALERTS_TOPIC_ARN": TOPIC, "DEVICE_MAP": DM, "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"})
    check("プロセスの環境変数に同じ名前があればそちら（知らない名前は読まない）。ファイルが無くても落ちない",
          sns.load_env(ENVF, environ={"AWS_REGION": "us-east-1", "OTHER": "x"})["AWS_REGION"] == "us-east-1"
          and "OTHER" not in sns.load_env(ENVF, environ={"OTHER": "x"})
          and sns.load_env(os.path.join(tmp, "none"), environ={"ALERTS_TOPIC_ARN": TOPIC}) == {"ALERTS_TOPIC_ARN": TOPIC})

    # ---- SigV4
    URL = "https://sns.ap-northeast-1.amazonaws.com/"
    BODY = urllib.parse.urlencode({"Action": "Publish", "Version": "2010-03-31", "TopicArn": TOPIC, "Subject": "netops alert", "Message": '{"source":"splunk","alerts":[]}'}).encode()
    NOW = datetime.datetime(2026, 10, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    h = sns.signed_headers(URL, BODY, "ap-northeast-1", CREDS, now=NOW)
    check("SigV4 の署名（時刻を固定した既知の答え。botocore 1.43.94 の SigV4Auth と同じ値）",
          h["authorization"] == "AWS4-HMAC-SHA256 Credential=AKIDTEST/20261002/ap-northeast-1/sns/aws4_request, "
          "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, Signature=cab76b30225255c51cb022237f5ece528ed391e6aece6898d341b89856be87ff")
    check("一時的な認証情報の token はヘッダーに入れて署名にも含める。token が無ければヘッダーごと無い",
          h["x-amz-security-token"] == "test-token" and h["x-amz-date"] == "20261002T030405Z" and h["host"] == "sns.ap-northeast-1.amazonaws.com"
          and sns.signed_headers(URL, BODY, "ap-northeast-1", CREDS[:2] + ("",), now=NOW)["authorization"].endswith(
              "SignedHeaders=content-type;host;x-amz-date, Signature=d6bd5e6c29754aa30f48b4b57c5b9e8e7ef4bd9b5c4cbc1e0af600ce89e8c06a")
          and "x-amz-security-token" not in sns.signed_headers(URL, BODY, "ap-northeast-1", CREDS[:2] + ("",), now=NOW))
    check("秘密鍵は署名の計算にだけ使い、ヘッダーには出ない", not any("test-secret" in v for v in h.values()))
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ImportError:
        print("-- botocore が無いので、署名の突き合わせは既知の答えだけ")
    else:
        req = AWSRequest(method="POST", url=URL, data=BODY, headers={"Content-Type": h["content-type"]})
        SigV4Auth(Credentials(*CREDS), "sns", "ap-northeast-1").add_auth(req)
        at = datetime.datetime.strptime(req.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        check("botocore の SigV4Auth と同じ署名になる（同じ時刻・同じ本文）",
              sns.signed_headers(URL, BODY, "ap-northeast-1", CREDS, now=at)["authorization"] == req.headers["Authorization"])
    check("リージョンはトピックの ARN から取る（ARN で分からないときだけ AWS_REGION）",
          sns.topic_region(TOPIC) == "ap-northeast-1" and sns.topic_region("", "us-west-2") == "us-west-2" and sns.topic_region("arn:aws:sns", "x") == "x")

    # ---- 認証情報と publish（HTTP は差し替える）
    class Res(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Opener:
        def __init__(self, *answers): self.calls, self.answers = [], list(answers)
        def open(self, req, timeout=None):
            self.calls.append((req, timeout))
            a = self.answers.pop(0) if self.answers else b"<ok/>"
            if isinstance(a, Exception):
                raise a
            return Res(a)

    real_opener = sns.OPENER
    check("HTTP はプロキシの環境変数を見ない opener で出す（認証情報の口は 169.254.170.2、SNS は VPC のエンドポイント）",
          "ProxyHandler({})" in src and "urlopen(" not in src and src.count("OPENER.open(") == 2)
    CRED_JSON = json.dumps({"AccessKeyId": "AKIDTEST", "SecretAccessKey": "test-secret", "Token": "test-token", "Expiration": "2026-10-02T09:00:00Z"}).encode()
    sns.OPENER = Opener(CRED_JSON, CRED_JSON)
    check("認証情報は ECS のタスクロールの口（http://169.254.170.2 + AWS_CONTAINER_CREDENTIALS_RELATIVE_URI）から取る",
          sns.credentials(env) == CREDS and sns.OPENER.calls[0] == ("http://169.254.170.2/v2/credentials/abc", sns.TIMEOUT)
          and sns.credentials({"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9911/creds"}) == CREDS and sns.OPENER.calls[1][0] == "http://127.0.0.1:9911/creds")
    try:
        sns.credentials({}); raised = ""
    except RuntimeError as e:
        raised = str(e)
    check("口が無い（タスクロールが付いていない）ときは理由を言って止まる。アクセスキーの環境変数へは逃げない",
          "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" in raised and "AWS_ACCESS_KEY_ID" not in src and "AWS_SECRET_ACCESS_KEY" not in src)
    sns.OPENER = Opener()
    sns.publish(env, CREDS, texts[0])
    req, timeout = sns.OPENER.calls[0]
    form = dict(urllib.parse.parse_qsl(req.data.decode()))
    check("publish は SNS の Query API へ POST する（トピックのリージョンのエンドポイント、Action=Publish、本文はアラートの JSON）",
          req.full_url == URL and req.get_method() == "POST" and timeout == sns.TIMEOUT
          and form == {"Action": "Publish", "Version": "2010-03-31", "TopicArn": TOPIC, "Subject": "netops alert", "Message": texts[0]})
    check("publish のリクエストは SigV4 で署名してある（token 付き）",
          req.get_header("Authorization").startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "/ap-northeast-1/sns/aws4_request" in req.get_header("Authorization")
          and req.get_header("X-amz-security-token") == "test-token")
    sns.OPENER = Opener()
    sns.publish(dict(env, AWS_ENDPOINT_URL_SNS="http://127.0.0.1:9911/"), CREDS, "{}")
    check("AWS_ENDPOINT_URL_SNS があればそこへ送る（手元で偽の SNS に向けて試すとき）", sns.OPENER.calls[0][0].full_url == "http://127.0.0.1:9911/")
    sns.OPENER = real_opener

    # ---- 送り直し
    def run_send(outcomes):
        """outcomes の順に publish が成功（None）/ 失敗（例外）する。(送れた通数, publish の回数, 認証情報を取った回数, 待った秒, stderr)"""
        log = {"publish": 0, "creds": 0, "slept": []}
        def publish(e, creds, text):
            log["publish"] += 1
            o = outcomes.pop(0) if outcomes else None
            if o is not None:
                raise o
        def credentials(e):
            log["creds"] += 1
            return CREDS
        keep = sns.publish, sns.credentials
        sns.publish, sns.credentials = publish, credentials
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                sent = sns.send(env, ["a", "b"], sleep=log["slept"].append)
        finally:
            sns.publish, sns.credentials = keep
        return sent, log["publish"], log["creds"], log["slept"], err.getvalue()

    check("2 通とも 1 回で送れれば待たない", run_send([])[:4] == (2, 2, 2, []))
    http = urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(b"<Error><Code>AuthorizationError</Code></Error>"))
    sent, n, ncreds, slept, err = run_send([http, urllib.error.URLError("timed out")])
    check("失敗したら認証情報を取り直して送り直す（1 通につき 3 回まで。待ちは 1 秒、2 秒）",
          (sent, n, ncreds, slept) == (2, 4, 4, [1, 2]) and err.count("ERROR publish に失敗した") == 2 and "HTTP 403" in err and "AuthorizationError" in err and "URLError" in err)
    sent, n, ncreds, slept, err = run_send([OSError("down")] * 3)
    check("3 回とも失敗した通は諦めて次の通へ進む（送れた通数を返す）", (sent, n, slept) == (1, 4, [1, 2]) and "（3/3）" in err)
    check("失敗のログに認証情報は出ない", not any(s in err for s in ("test-secret", "test-token", "AKIDTEST")))
    check("送り直しの回数と待ちの定数", (sns.ATTEMPTS, sns.TIMEOUT, sns.SUBJECT, sns.STATUSES) == (3, 10, "netops alert", ("firing", "resolved")))

    # ---- main（Splunk が --execute で起こし、標準入力に JSON を渡す）
    def run_main(argv, stdin, envd=None, sent=None, fail=None):
        published = []
        def send(e, texts, sleep=None):
            if fail:
                raise fail
            published.extend(texts)
            return len(texts) if sent is None else sent
        keep = sns.load_env, sns.send
        sns.load_env, sns.send = (lambda: dict(env if envd is None else envd)), send
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = sns.main(argv, io.StringIO(stdin))
        finally:
            sns.load_env, sns.send = keep
        return rc, published, err.getvalue()

    PAYLOAD = json.dumps({"search_name": "netops_gnmi", "results_file": RESULTS, "session_key": "must-not-be-logged"})
    rc, published, err = run_main(["netops_sns.py", "--execute"], PAYLOAD)
    check("main: 結果のファイルを読んで publish し、0 で終わる（件数を 1 行で残す）",
          rc == 0 and len(published) == 1 and [a["device_id"] for a in json.loads(published[0])["alerts"]] == ["dc1-leaf-01", "dc1-leaf-02"]
          and "INFO search=netops_gnmi rows=2 alerts=2 published=1/1" in err)
    check("main: Splunk のセッションキーはログに出さない", "must-not-be-logged" not in err)
    check("main: --execute 以外では何もしない（1）", run_main(["netops_sns.py"], PAYLOAD)[0] == 1 and run_main(["netops_sns.py", "--other"], PAYLOAD)[:2] == (1, []))
    rc, published, err = run_main(["netops_sns.py", "--execute"], PAYLOAD, envd={"AWS_REGION": "ap-northeast-1"})
    check("main: トピックの ARN が無ければ送らずに 2（理由を ERROR で残す）", rc == 2 and published == [] and "ERROR ALERTS_TOPIC_ARN が無い" in err)
    check("main: 送れなかった通があれば 2（Splunk の sendmodalert に失敗として残る）", run_main(["netops_sns.py", "--execute"], PAYLOAD, sent=0)[0] == 2)
    rc, published, err = run_main(["netops_sns.py", "--execute"], json.dumps({"results_file": os.path.join(tmp, "none.csv.gz")}))
    check("main: 結果のファイルが無い・JSON が壊れているなどは traceback を出さずに 3", rc == 3 and "ERROR Unexpected error: FileNotFoundError" in err and "Traceback" not in err
          and run_main(["netops_sns.py", "--execute"], "{壊れた")[0] == 3 and run_main(["netops_sns.py", "--execute"], PAYLOAD, fail=ValueError("x"))[0] == 3)
    with gzip.open(RESULTS, "wt", encoding="utf-8", newline="") as f:
        f.write("device,kind,target,status,detail,starts_at\n")
    rc, published, err = run_main(["netops_sns.py", "--execute"], PAYLOAD)
    check("main: 行が 0 件なら何も送らずに 0", rc == 0 and published == [] and "published=0/0" in err)


# ---- Splunk の app（conf）
def parse_conf(text):
    """Splunk の conf → {stanza: {key: value}}。行末の \\ は次の行へ続く（続きの行はコメントにならない）"""
    out, stanza, key = {}, None, None
    for line in text.splitlines():
        if key is not None:
            out[stanza][key] += "\n" + line.rstrip("\\").rstrip() if line.endswith("\\") else "\n" + line
            if not line.endswith("\\"):
                key = None
            continue
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            stanza = s[1:-1]; out[stanza] = {}
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        out[stanza][k] = v.rstrip("\\").rstrip() if v.endswith("\\") else v
        if v.endswith("\\"):
            key = k
    return out


APP = ("splunk", "netops_alerts")
saved = parse_conf(read(*APP, "default", "savedsearches.conf"))
actions = parse_conf(read(*APP, "default", "alert_actions.conf"))
check("保存済みサーチは 3 つ（gNMI の BGP / IS-IS、trap、trap を時間で閉じる）", list(saved) == ["netops_gnmi", "netops_trap", "netops_trap_clear"])
COMMON = {"enableSched": "1", "cron_schedule": "* * * * *", "realtime_schedule": "0", "dispatch.latest_time": "+5m", "alert.track": "0", "alert.digest_mode": "1",
          "alert.suppress": "0", "counttype": "number of events", "relation": "greater than", "quantity": "0", "action.netops_sns": "1"}
check("どのサーチも毎分走り、遅れても飛ばさず（realtime_schedule = 0）、結果が 1 行でもあれば全部の行をまとめて 1 回アクションへ渡す（digest）。抑制はしない",
      all({k: s.get(k) for k in COMMON} == COMMON for s in saved.values()))
check("アクションは netops_sns だけ（メールや webhook は付けない）", all([k for k in s if k.startswith("action.")] == ["action.netops_sns"] for s in saved.values()))
pipes = {n: [p.strip() for p in s["search"].split("\n| ")] for n, s in saved.items()}
check("どのサーチも _raw だけにしてから spath で項目を取る（KV_MODE = json の項目に spath を重ねると多値になって 1 行も出ない）",
      all(p[1] == "fields _time _indextime _raw source" and p[2] == "spath" and s["search"].count("spath") == 1 for (n, p), s in zip(pipes.items(), saved.values())))
check("どのサーチも最後は table device kind target status detail starts_at（アラートアクションが読む列）",
      all(p[-1] == "table device kind target status detail starts_at" for p in pipes.values())
      and all(re.search(rf"r\.get\(\"{c}\"\)", src) for c in ("device", "kind", "target", "status", "detail", "starts_at")))
check("窓は索引に入った時刻で切る（直前の 1 分を 10 秒手前にずらして 1 回だけ読む。イベントの時刻の窓 dispatch.earliest_time は広く取る）",
      all(pipes[n][0].endswith("_index_earliest=-1m@m-10s _index_latest=@m-10s") and saved[n]["dispatch.earliest_time"] == "-1h" for n in ("netops_gnmi", "netops_trap"))
      and pipes["netops_trap_clear"][0].endswith("_index_earliest=-70m@m-10s _index_latest=@m-10s") and saved["netops_trap_clear"]["dispatch.earliest_time"] == "-3h")
check("イベントは source で選ぶ（Spark の splunk_events が telegraf:<measurement> を付ける）。index は決め打ちしない（SPLUNK_INDEX で変わる）",
      pipes["netops_gnmi"][0].startswith('index=* (source="telegraf:bgp_neighbor" OR source="telegraf:isis_interface") ')
      and all(pipes[n][0].startswith('index=* source="telegraf:snmp_trap" ') for n in ("netops_trap", "netops_trap_clear"))
      and 'f"telegraf:{r[\'measurement\'] or \'unknown\'}"' in read("spark", "snmp_sinks.py"))
DEVICE = "eval device=coalesce('tags.sysName', 'tags.agent_host', 'tags.source')"
check("機器は tags.sysName > tags.agent_host > tags.source（gNMI と trap は IP。アラートアクションが DEVICE_MAP で名前に直す）",
      all(DEVICE in p for p in pipes.values()))
g = saved["netops_gnmi"]["search"]
check("gNMI: BGP は session_state が established 以外で bgp_down、IS-IS は oper_state が up 以外で isis_down（対象ごとに最後の状態 1 つ）",
      'eval kind=if(source=="telegraf:bgp_neighbor", "bgp_down", "isis_down")' in g and "'fields.session_state'" in g and "'fields.oper_state'" in g
      and "stats latest(state) as state latest(_time) as starts_at by device kind target" in g
      and 'eval status=if((kind=="bgp_down" AND state=="established") OR (kind=="isis_down" AND state=="up"), "resolved", "firing")' in g)
check("gNMI: target は BGP がピアのアドレス、IS-IS が IF 名（status Lambda の set_layer_status が引く名前）",
      "coalesce('tags.peer_address', 'tags.neighbor_peer_address'), 'tags.interface_name'" in g)


def norm(oid, search):
    """サーチの中の OID の正規化（replace(replace(x, "^iso\\.", ".1."), "^1\\.", ".1.")）を同じ正規表現で写す"""
    m = re.search(r"""replace\(replace\('tags\.oid', "([^"]+)", "([^"]+)"\), "([^"]+)", "([^"]+)"\)""", search)
    return re.sub(m.group(3), m.group(4), re.sub(m.group(1), m.group(2), oid))


t, c = saved["netops_trap"]["search"], saved["netops_trap_clear"]["search"]
check("trap: OID は先頭を .1. に揃える（MIB が無いと Telegraf は iso.3.6… と書く）",
      [norm(o, t) for o in ("iso.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.3", ".1.3.6.1.6.3.1.1.5.3", "iso.3.6.1.4.1.9.1.0.1")]
      == [".1.3.6.1.6.3.1.1.5.3"] * 3 + [".1.3.6.1.4.1.9.1.0.1"] and norm("iso.3.6.1.6.3.1.1.5.3", c) == ".1.3.6.1.6.3.1.1.5.3")
IGNORED = [".1.3.6.1.6.3.1.1.5.1", ".1.3.6.1.6.3.1.1.5.2", ".1.3.6.1.4.1.8072.4.0.2", ".1.3.6.1.4.1.8072.4.0.3"]
LINK = [".1.3.6.1.6.3.1.1.5.3", ".1.3.6.1.6.3.1.1.5.4"]
excluded = lambda s: re.findall(r'oid!="(\.[0-9.]+)"', s)   # noqa: E731
check("trap: 機器が起きた知らせ（coldStart / warmStart / nsNotifyShutdown / nsNotifyRestart）は異常にしない", excluded(t) == IGNORED)
check("trap: linkDown は link_down の firing、linkUp は resolved。それ以外は kind = trap（target は trap の OID）の firing",
      'eval kind=if(oid==".1.3.6.1.6.3.1.1.5.3" OR oid==".1.3.6.1.6.3.1.1.5.4", "link_down", "trap")' in t
      and 'eval status=if(oid==".1.3.6.1.6.3.1.1.5.4", "resolved", "firing")' in t
      and 'eval target=if(kind=="link_down", coalesce(if_name, if_descr, if_index, "?"), oid)' in t)
vb = dict(re.findall(r'(if_\w+)=if\(isnull\(if_\w+\) AND match\(varbind, "([^"]+)"\)', t))
check("trap: IF は varbind の ifName > ifDescr > ifIndex（名前でも数値 OID でも、末尾の ifIndex 付きでも拾う。似た OID は拾わない）",
      list(vb) == ["if_name", "if_descr", "if_index"]
      and all(re.search(vb["if_name"], v) for v in ("ifName", "ifName.5", ".1.3.6.1.2.1.31.1.1.1.1.5"))
      and all(re.search(vb["if_descr"], v) for v in ("ifDescr.5", ".1.3.6.1.2.1.2.2.1.2.5"))
      and all(re.search(vb["if_index"], v) for v in ("ifIndex.5", ".1.3.6.1.2.1.2.2.1.1.5"))
      and not any(re.search(vb[k], v) for k, v in (("if_name", "ifNameX"), ("if_name", ".1.3.6.1.2.1.31.1.1.1.18.5"), ("if_index", ".1.3.6.1.2.1.2.2.1.10.5"), ("if_descr", "ifAdminStatus.5")))
      and 'foreach "fields.*" [ eval varbind=replace(replace("<<MATCHSTR>>", "^iso\\.", ".1."), "^1\\.", ".1."),' in t)
check("trap の解消: link の trap は対象にしない（linkUp が閉じる）。その機器から link 以外の trap が 10 分来なければ、機器ごとにまとめて resolved",
      excluded(c) == IGNORED + LINK and "stats max(_indextime) as last latest(_time) as starts_at by device oid" in c
      and "eventstats max(last) as device_last by device" in c
      and 'where device_last >= relative_time(now(), "-11m@m-10s") AND device_last < relative_time(now(), "-10m@m-10s")' in c
      and 'eval kind="trap", target=oid, status="resolved"' in c)
check("trap の解消の target は trap の firing と同じ（OID）なので、同じ anomaly_id を閉じる",
      "target=oid" in c and 'coalesce(if_name, if_descr, if_index, "?"), oid)' in t)
check("サーチが出す kind は受け手が知っているものだけ（link_down はワークフローを起こし、bgp_down / isis_down / trap は status だけ）",
      set(re.findall(r'"(link_down|bgp_down|isis_down|trap)"', g + t + c)) == {"link_down", "bgp_down", "isis_down", "trap"} and rules.START_KINDS == {"link_down"})
check("アラートアクションの定義: カスタム、標準入力は JSON、Python 3", actions == {"netops_sns": dict(actions["netops_sns"], **{"is_custom": "1", "payload_format": "json", "python.required": "latest"})}
      and os.path.exists(os.path.join(ROOT, *APP, "bin", "netops_sns.py")) and os.path.exists(os.path.join(ROOT, *APP, "default", "data", "ui", "alerts", "netops_sns.html")))
check("spec（README/*.conf.spec）がある（無いと btool check が知らない設定として警告する）",
      "[netops_sns]" in read(*APP, "README", "alert_actions.conf.spec") and "action.netops_sns = " in read(*APP, "README", "savedsearches.conf.spec"))
appc, meta = parse_conf(read(*APP, "default", "app.conf")), parse_conf(read(*APP, "metadata", "default.meta"))
check("app は有効で、画面には出さない。サーチ・アクション・props は全体へ export する（HEC のイベントは他の app の文脈でも読める）",
      appc["install"]["state"] == "enabled" and appc["ui"]["is_visible"] == "0" and appc["package"]["id"] == "netops_alerts"
      and all(meta[k] == {"export": "system"} for k in ("alert_actions", "savedsearches", "props")))
check("props: telegraf:* の source は JSON として読む（画面で tags.* / fields.* を使えるように）", parse_conf(read(*APP, "default", "props.conf")) == {"source::telegraf:*": {"KV_MODE": "json"}})
check("app の中に認証情報や local/ は無い（公開リポジトリ）",
      not os.path.exists(os.path.join(ROOT, *APP, "local")) and not any(re.search(r"(?i)(password|token|secret)\s*=", read(*APP, "default", f))
                                                                         for f in ("savedsearches.conf", "alert_actions.conf", "app.conf", "props.conf")))

# ---- Splunk のイメージ
df = read("splunk", "Dockerfile")
dcode = [l for l in df.splitlines() if l.strip() and not l.startswith("#")]
check("Splunk のイメージは上流の公式イメージに app と入口を足すだけ（RUN は無い = arm64 の PC でも QEMU 無しでビルドできる）",
      dcode == ["ARG SPLUNK_VERSION=10.4.3", "FROM splunk/splunk:${SPLUNK_VERSION}", "COPY --chown=splunk:splunk netops_alerts /opt/splunk-etc/apps/netops_alerts",
                "COPY --chmod=0755 entrypoint.sh /sbin/netops-entrypoint.sh", 'ENTRYPOINT ["/sbin/netops-entrypoint.sh"]', 'CMD ["start-service"]'])
up = read("ops", "up.sh")
check("up.sh の SPLUNK_VERSION / GRAFANA_VERSION は Dockerfile の ARG の既定値と同じ",
      re.search(r"^SPLUNK_VERSION=([\d.]+)", up, re.M).group(1) == re.search(r"ARG SPLUNK_VERSION=([\d.]+)", df).group(1)
      and re.search(r"^GRAFANA_VERSION=([\d.]+)", up, re.M).group(1) == re.search(r"ARG GRAFANA_VERSION=([\d.]+)", read("grafana", "Dockerfile")).group(1))
check("up.sh は Splunk と Grafana のイメージを <版>-<ディレクトリのハッシュ> のタグでビルドする（中身を変えたら別のタグ。Splunk は amd64、Grafana は arm64）",
      'SPLUNK_TAG=$(dir_tag "$SPLUNK_VERSION" splunk)' in up and 'GRAFANA_TAG=$(dir_tag "$GRAFANA_VERSION" grafana)' in up
      and 'docker buildx build --platform linux/amd64 --build-arg "SPLUNK_VERSION=$SPLUNK_VERSION" -t "$REG/$PREFIX-splunk:$SPLUNK_TAG" --push splunk/' in up
      and 'docker buildx build --platform linux/arm64 --build-arg "GRAFANA_VERSION=$GRAFANA_VERSION" -t "$REG/$PREFIX-grafana:$GRAFANA_TAG" --push grafana/' in up)
stf = read("terraform", "pipeline", "analytics", "splunk.tf")
check("Splunk のタスクはトピックの ARN と device map を環境変数で受け、タスクロールは土台のトピックへの sns:Publish だけを足す",
      '{ name = "ALERTS_TOPIC_ARN", value = local.alerts_topic_arn }' in stf and re.search(r'\{ name = "DEVICE_MAP", value = var\.device_map \}', stf) is not None
      and re.search(r'Action\s*=\s*\["sns:Publish"\]\s*\n\s*Resource\s*=\s*local\.alerts_topic_arn', stf) is not None and '"sns:*"' not in stf)
lt = load("lab/lab_topology.py", "lab_topology")
dm = subprocess.run([sys.executable, os.path.join(ROOT, "lab", "lab_topology.py"), "lab", "--device-map"], capture_output=True, text=True, cwd=ROOT)
dmap = sns.parse_device_map(dm.stdout.strip())
check("device map（lab/lab_topology.py --device-map）は管理 IP と回線の IP を機器名に引ける形で、アラートアクションがそのまま読む",
      dm.returncode == 0 and len(dmap) >= 8 and all(sns.IPV4_RE.match(k) and re.fullmatch(r"[a-z0-9-]+", v) for k, v in dmap.items())
      and sns.device_name("10.255.2.1", dmap) == "dc1-leaf-01" and len(dm.stdout.strip()) < 4000)

# ---- Grafana のアラート
gy = read("grafana", "provisioning", "alerting", "netops.yaml")
gcode = "\n".join(l for l in gy.splitlines() if not l.lstrip().startswith("#"))
tmpl = re.search(r'\{\{ define "netops\.sns" \}\}(.*)\{\{ end \}\}\s*$', gcode, re.S).group(1)
check("Grafana の本文の項目は Splunk のアラートアクションと同じ 6 つ（同じ順）、source は grafana",
      re.findall(r'"(\w+)":', tmpl) == ["source", "alerts"] + KEYS and tmpl.startswith('{"source":"grafana","alerts":['))


def render(alerts):
    """netops.sns のテンプレートを Go のテンプレートの代わりに写す（使っている構文だけ）。%q は JSON の文字列と同じ引用"""
    m = re.fullmatch(r"(.*)\{\{ range \$i, \$a := \.Alerts \}\}(.*)\{\{ end \}\}(.*)", tmpl, re.S)
    out = []
    for i, a in enumerate(alerts):
        s = m.group(2).replace("{{ if $i }},{{ end }}", "," if i else "")
        s = s.replace('{{ printf "%q" (printf "%s is down (grafana)" $a.Labels.ifName) }}', json.dumps(f"{a['labels'].get('ifName', '')} is down (grafana)"))
        s = re.sub(r'\{\{ printf "%q" \$a\.Labels\.(\w+) \}\}', lambda x: json.dumps(a["labels"].get(x.group(1), "")), s)
        s = s.replace('{{ printf "%q" $a.Status }}', json.dumps(a["status"])).replace("{{ $a.StartsAt.Unix }}", str(a["starts_at"]))
        out.append(s)
    return m.group(1) + "".join(out) + m.group(3)


body = render([{"status": "firing", "labels": {"sysName": "dc1-leaf-01", "ifName": "ethernet-1/49", "kind": "link_down"}, "starts_at": 1790000000},
               {"status": "resolved", "labels": {"sysName": "DC1-Spine-01", "ifName": 'eth"x', "kind": "link_down"}, "starts_at": 1790000030}])
check("Grafana の本文は埋めたあと JSON になる（テンプレートの構文は全部埋まる。値の中の \" も壊さない）",
      "{{" not in body and [a["target"] for a in json.loads(body)["alerts"]] == ["ethernet-1/49", 'eth"x'] and all(list(a) == KEYS for a in json.loads(body)["alerts"]))
check("受け手がそのまま読める: Grafana の link_down は Splunk の linkDown trap と同じ anomaly_id（同じ障害は 1 つのワークフロー）",
      [(a["anomaly_id"], a["status"], a["first_seen"], a["source"]) for a in rules.alerts_from_message(body)]
      == [("dc1-leaf-01#link_down#ethernet-1/49", "firing", 1790000000, "grafana"), ("dc1-spine-01#link_down#eth\"x", "resolved", 1790000030, "grafana")]
      and rules.alerts_from_message(sns.messages(sns.alerts_from_rows([{"device": "10.255.2.1", "kind": "link_down", "target": "ethernet-1/49", "status": "firing"}], dmap))[0], now=1)[0]["anomaly_id"]
      == "dc1-leaf-01#link_down#ethernet-1/49")
check("$ は二重にしない（$$ と書くと Grafana 13.2.2 が起動しない）。${…} は連絡先の環境変数 2 つだけ", "$$" not in gcode and set(re.findall(r"\$\{(\w+)\}", gcode)) == {"ALERTS_TOPIC_ARN", "AWS_REGION"})
check("ルール: ifOperStatus が 2 の IF を link_down にする（ループバック・管理ポート・サブ IF・admin down は見ない。値で判定するので直れば次の評価で解消）",
      "expr: 'snmp_interface_ifOperStatus{ifName!~\"(lo|mgmt).*|.*[.].*\"} unless on (sysName, ifName) (snmp_interface_ifAdminStatus == 2)'" in gcode
      and re.search(r"type: within_range\s*\n\s*params: \[1\.5, 2\.5\]", gcode) is not None and "condition: C" in gcode and "for: 0s" in gcode and "interval: 30s" in gcode)
ss = read("spark", "snmp_sinks.py")
check("ルールのメトリクス名とラベルは Spark が AMP に書く名前（snmp_<measurement>_<field>、tag はそのままラベル）で、データソースは uid: amp",
      'METRIC_PREFIX = "snmp"' in ss and "datasourceUid: amp" in gcode and re.search(r"^\s*uid: amp$", read("grafana", "provisioning", "datasources", "prometheus.yaml"), re.M) is not None
      and all(f in read("telegraf", "telegraf.conf.in") for f in ("ifOperStatus", "ifAdminStatus", "ifName", "sysName")))
check("データが無い・クエリが失敗したときは直前の状態のまま（分からないときに発火も解消もしない）", "noDataState: KeepLast" in gcode and "execErrState: KeepLast" in gcode)
check("ルールは kind: link_down のラベルを付ける（本文の kind。ワークフローが起きる種類）", re.search(r"labels:\s*\n\s*kind: link_down", gcode) is not None and "link_down" in rules.START_KINDS)
check("連絡先は SNS（鍵は書かない = タスクロールで SigV4）。解消も送る",
      "type: sns" in gcode and "topic_arn: ${ALERTS_TOPIC_ARN}" in gcode and re.search(r"sigv4:\s*\n\s*region: \$\{AWS_REGION\}", gcode) is not None
      and "disableResolveMessage: false" in gcode and "message: '{{ template \"netops.sns\" . }}'" in gcode
      and not re.search(r"(?i)access_key|secret_key|assume_role|profile", gcode))
check("通知ポリシー: IF ごとに 1 通、発火はすぐ、解消は 30 秒以内、直らないあいだは 4 時間ごとに送り直す",
      "receiver: netops-sns" in gcode and "group_by: ['alertname', 'sysName', 'ifName']" in gcode and "group_wait: 0s" in gcode and "group_interval: 30s" in gcode and "repeat_interval: 4h" in gcode)
gdf = read("grafana", "Dockerfile")
check("Grafana のイメージは provisioning を持ち、SigV4 を既定の認証情報（タスクロール）で使う",
      "COPY provisioning /etc/grafana/netops" in gdf and "GF_AUTH_SIGV4_AUTH_ENABLED=true" in gdf and "GF_AWS_ALLOWED_AUTH_PROVIDERS=default" in gdf)
gtf = read("terraform", "pipeline", "analytics", "grafana.tf")
check("Grafana のタスクはトピックの ARN を環境変数で受け、タスクロールは土台のトピックへの sns:Publish だけを足す",
      '{ name = "ALERTS_TOPIC_ARN", value = local.alerts_topic_arn }' in gtf and re.search(r'Action\s*=\s*\["sns:Publish"\]\s*\n\s*Resource\s*=\s*local\.alerts_topic_arn', gtf) is not None and '"sns:*"' not in gtf)


def start_sh(**env):
    """grafana/start.sh を一時ディレクトリに向けて走らせ、並んだアラートの定義とデータソースを返す"""
    with tempfile.TemporaryDirectory() as tmp:
        sh = read("grafana", "start.sh").replace("SRC=/etc/grafana/netops", f"SRC={os.path.join(ROOT, 'grafana', 'provisioning')}")
        sh = sh.replace("/tmp/grafana-", f"{tmp}/grafana-").replace('exec /run.sh "$@"', 'echo "run $GF_PATHS_PROVISIONING"')
        assert "/etc/grafana" not in sh and "/tmp/grafana" not in sh.replace(tmp, "") and "exec " not in sh
        r = subprocess.run(["sh", "-c", sh], capture_output=True, text=True, env=dict({"PATH": os.environ["PATH"]}, **env))
        assert r.returncode == 0 and r.stdout.strip().endswith(f"run {tmp}/grafana-provisioning"), r.stderr
        return sorted(os.listdir(f"{tmp}/grafana-provisioning/alerting")), sorted(os.listdir(f"{tmp}/grafana-provisioning/datasources"))


AMP = "https://aps-workspaces.ap-northeast-1.amazonaws.com/workspaces/ws-x"
check("start.sh: アラートの定義は PROMETHEUS_URL と ALERTS_TOPIC_ARN の両方があるときだけ並べる（ルールは AMP を読み、連絡先はトピックへ送る）",
      start_sh(PROMETHEUS_URL=AMP, ALERTS_TOPIC_ARN=TOPIC) == (["netops.yaml"], ["prometheus.yaml"])
      and start_sh(PROMETHEUS_URL=AMP) == ([], ["prometheus.yaml"])
      and start_sh(ALERTS_TOPIC_ARN=TOPIC, OPENSEARCH_URL="https://x") == ([], ["opensearch.yaml"]) and start_sh() == ([], []))

# ---- SNS のトピック（土台）と受け手の配線
atf = read("terraform", "base", "core", "alerts.tf")
acode = "\n".join(l for l in atf.splitlines() if not l.lstrip().startswith("#"))
check("トピックは土台（base/core）に 1 つ（<接頭辞>-alerts）、保存時の暗号化は AWS 管理の鍵",
      acode.count('resource "aws_sns_topic" ') == 1 and 'name = "${local.name_prefix}-alerts"' in acode and 'kms_master_key_id = "alias/aws/sns"' in acode)
check("トピックのポリシー: 許すのはこのアカウントだけ（Principal に * の Allow は無い）",
      re.search(r'sid\s*=\s*"OwnAccount".*?identifiers = \["arn:\$\{local\.partition\}:iam::\$\{local\.account_id\}:root"\]', acode, re.S) is not None
      and len(re.findall(r'identifiers = \["\*"\]', acode)) == 1 and acode.index('identifiers = ["*"]') > acode.index('effect    = "Deny"'))
deny = acode[acode.index('dynamic "statement"'):]
check("閉域（network_perimeter）のとき、VPC の外からの sns:Publish を拒む（拒むのは Publish だけ）",
      "for_each = var.network_perimeter ? [1] : []" in deny and 'sid       = "DenyOutsideVpc"' in deny and 'actions   = ["sns:Publish"]' in deny
      and re.findall(r'test\s*=\s*"(\w+)"\s*\n\s*variable\s*=\s*"([\w:]+)"', deny)
      == [("StringNotEqualsIfExists", "aws:SourceVpc"), ("BoolIfExists", "aws:ViaAWSService"), ("Bool", "aws:PrincipalIsAWSService"), ("ArnNotLike", "aws:PrincipalArn")]
      and "values   = [aws_vpc.this.id]" in deny and "values   = local.perimeter_exempt_principals" in deny)
check("土台は alerts_topic_arn を出し、閉域の IAM 側の Deny に sns:* がある（events:* はもう無い）",
      re.search(r'output "alerts_topic_arn" \{[^}]*value\s*=\s*aws_sns_topic\.alerts\.arn', read("terraform", "base", "core", "outputs.tf")) is not None
      and '"sns:*"' in read("terraform", "base", "core", "perimeter.tf") and '"events:*"' not in read("terraform", "base", "core", "perimeter.tf"))
TRY = 'alerts_topic_arn = try(data.terraform_remote_state.main.outputs.alerts_topic_arn, "")'
check("送り手（analytics）と受け手（workflow / graph）は土台の state からトピックを読む（古い土台なら apply の前に理由を言って止まる）",
      all(TRY in read("terraform", *r, "locals.tf") for r in (("pipeline", "analytics"), ("pipeline", "graph"), ("workflow",)))
      and all('condition     = local.alerts_topic_arn != ""' in read("terraform", *f) for f in
              (("pipeline", "analytics", "grafana.tf"), ("pipeline", "analytics", "splunk.tf"), ("pipeline", "graph", "sync.tf"), ("workflow", "events.tf"))))
wtf = read("terraform", "workflow", "events.tf")
check("受け手は 2 つ: SQS（raw message delivery。ワークフロー）と Lambda（status）",
      re.search(r'resource "aws_sns_topic_subscription" "anomalies" \{[^}]*protocol\s*=\s*"sqs"[^}]*raw_message_delivery\s*=\s*true', wtf, re.S) is not None
      and re.search(r'resource "aws_sns_topic_subscription" "status" \{[^}]*protocol\s*=\s*"lambda"', read("terraform", "pipeline", "graph", "sync.tf"), re.S) is not None)
check("EventBridge のルールと Spark からの put_events はどこにも無い",
      not any("aws_cloudwatch_event_" in read(f) or "events:PutEvents" in read(f)
              for f in glob.glob(os.path.join(ROOT, "terraform", "**", "*.tf"), recursive=True) if ".terraform" not in f)
      and "put_events" not in "\n".join(l for l in ss.splitlines() if not l.lstrip().startswith("#")).split('"""', 2)[2])

# ---- ops/check.sh
chk = read("ops", "check.sh")
listed = re.search(r"\n  for t in ([^;]+); do\n", chk).group(1).split()
check("check.sh は tests/ の test_*.py を全部走らせる（このテストも）",
      sorted(listed) == sorted("tests/" + os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "tests", "test_*.py"))) and "tests/test_alerts.py" in listed)
dirs = re.search(r"\nfind ([a-z ]+) -name '\*\.py'", chk).group(1).split()
tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"], capture_output=True, text=True, cwd=ROOT).stdout.split()
check("check.sh の構文検査は .py のあるディレクトリを全部見る（splunk/ のアラートアクションと graph/ の Lambda も）",
      {p.split("/")[0] for p in tracked} <= set(dirs) and {"splunk", "graph"} <= set(dirs))
print(f"通過 {passed} / 失敗 0")
