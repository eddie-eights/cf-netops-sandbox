"""機能 WORKFLOW（terraform/workflow、workflow/（rules / awsio / worker）、agent/proposals.py、agent/mcp_client.py、tools/）の模擬テスト。
AWS にも Temporal にも触れない。temporalio と boto3 を差し替えて 3 つのモジュールを読み、純粋な関数（プロンプト・JSON の読み取り・
許可リスト・二重起動の判定 = rules）と AWS 呼び出しの形（awsio）、proposals.decide の条件、mcp_client の応答の読み取り、
tools.json と Python の TOOL_SPECS の一致、Terraform と ops スクリプトのつながりを見る。実行は python3 tests/test_workflow.py（依存は無い）。"""
import ast, contextlib, json, os, re, sys, types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TF_DIR = os.path.join(ROOT, "terraform", "workflow")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()

# ---- 差し替え: boto3 / botocore（呼ばれた内容を記録する）
calls = []
class ClientError(Exception):
    def __init__(self, code="Err", msg=""):
        super().__init__(msg or code)
        self.response = {"Error": {"Code": code, "Message": msg}}
class BotoCoreError(Exception):
    pass
fake = {"execute_gremlin_query": {"result": {"data": []}}, "get_parameter": ClientError("ParameterNotFound")}
clients = []  # boto3.client に渡した (name, kw)
class FakeClient:
    def __init__(self, name):
        self.name = name
    def __getattr__(self, op):
        def call(**kw):
            calls.append((self.name, op, kw))
            r = fake.get(op)
            if isinstance(r, Exception):
                raise r
            return r if r is not None else {}
        return call
boto3 = types.ModuleType("boto3")
boto3.client = lambda name, **kw: clients.append((name, kw)) or FakeClient(name)
boto3.Session = lambda region_name=None: types.SimpleNamespace(get_credentials=lambda: None)
botocore = types.ModuleType("botocore"); botocore_exc = types.ModuleType("botocore.exceptions")
botocore_exc.ClientError = ClientError; botocore_exc.BotoCoreError = BotoCoreError
botocore_auth = types.ModuleType("botocore.auth"); botocore_auth.SigV4Auth = object
botocore_req = types.ModuleType("botocore.awsrequest"); botocore_req.AWSRequest = object
botocore_cfg = types.ModuleType("botocore.config"); botocore_cfg.Config = lambda **kw: kw
sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.exceptions": botocore_exc,
                    "botocore.auth": botocore_auth, "botocore.awsrequest": botocore_req, "botocore.config": botocore_cfg})

# ---- 差し替え: temporalio（デコレータは素通し）
def _passthrough(*a, **k):
    if len(a) == 1 and callable(a[0]) and not k:
        return a[0]
    return lambda f: f
t_activity = types.ModuleType("temporalio.activity"); t_activity.defn = _passthrough
t_workflow = types.ModuleType("temporalio.workflow")
t_workflow.defn = _passthrough; t_workflow.run = _passthrough; t_workflow.signal = _passthrough
t_workflow.execute_activity = None; t_workflow.wait_condition = None; t_workflow.now = None; t_workflow.info = None
# worker.py は自作モジュールを workflow.unsafe.imports_passed_through() で囲んで読む（Temporal のサンドボックス対策）。
# 素通しの context manager を置いておかないと import の時点で落ちる
t_workflow.unsafe = types.SimpleNamespace(imports_passed_through=contextlib.nullcontext)
t_client = types.ModuleType("temporalio.client"); t_client.Client = object; t_client.WorkflowFailureError = Exception
t_common = types.ModuleType("temporalio.common"); t_common.RetryPolicy = lambda **k: k
class ActivityError(Exception):
    def __init__(self, msg="", cause=None):
        super().__init__(msg)
        self.cause = cause
class ApplicationError(Exception):
    def __init__(self, msg="", non_retryable=False):
        super().__init__(msg)
        self.non_retryable = non_retryable
class WorkflowAlreadyStarted(Exception):
    pass
t_exc = types.ModuleType("temporalio.exceptions")
t_exc.WorkflowAlreadyStartedError = WorkflowAlreadyStarted; t_exc.ActivityError = ActivityError; t_exc.ApplicationError = ApplicationError
t_worker = types.ModuleType("temporalio.worker"); t_worker.Worker = object
t_root = types.ModuleType("temporalio")
sys.modules.update({"temporalio": t_root, "temporalio.activity": t_activity, "temporalio.workflow": t_workflow,
                    "temporalio.client": t_client, "temporalio.common": t_common, "temporalio.exceptions": t_exc,
                    "temporalio.worker": t_worker})

os.environ.update({"NEPTUNE_ENDPOINT": "nep:8182", "AUDIT_TABLE_BUCKET_ARN": "arn:aws:s3tables:ap-northeast-1:123456789012:bucket/audit",
                   "AUDIT_NAMESPACE": "netops", "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/x",
                   "LAB_INSTANCE_ID": "i-0123456789abcdef0", "PARAM_PREFIX": ""})
sys.path.insert(0, os.path.join(ROOT, "workflow"))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import worker  # noqa: E402 - Temporal のワークフローとアクティビティ
import awsio  # noqa: E402 - 環境変数と AWS 呼び出し
import rules  # noqa: E402 - 判断だけの純粋関数
import proposals  # noqa: E402
import mcp_client  # noqa: E402
import anomalies  # noqa: E402
import topology  # noqa: E402
import evidence  # noqa: E402
import handler  # noqa: E402

# ---- workflow/rules.py の純粋な関数
anomaly = {"anomaly_id": "hq-ce-01#link_down#eth1", "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1", "status": "open",
           "first_seen": 1700000000, "detail": "ifOperStatus down", "first_seen_jst": "2023-11-15 07:13:20"}
prompt = rules.build_prompt(anomaly)
check("プロンプトに機器・種別・対象が入る", all(s in prompt for s in ("hq-ce-01", "link_down", "eth1")))
check("プロンプトは JSON 1 個を求め、action の 3 択を示す", '"action"' in prompt and "heal-main | check | none" in prompt)
check("応答の中の JSON を拾う（前後に文があっても）",
      rules.parse_agent_json('確認しました。\n{"cause": "eth1 が down", "action": "heal-main", "reason": "主回線"}\n以上')
      == {"cause": "eth1 が down", "action": "heal-main", "reason": "主回線"})
check("JSON が無ければ action=none で本文を理由に残す", rules.parse_agent_json("わかりません")["action"] == "none"
      and rules.parse_agent_json("わかりません")["reason"] == "わかりません")
check("壊れた JSON でも落ちない", rules.parse_agent_json("{bad json")["action"] == "none")
check("cause は 1000 字で切る", len(rules.parse_agent_json(json.dumps({"cause": "x" * 5000}))["cause"]) == 1000)
check("heal-main は sudo lab heal-main", rules.normalize_action("heal-main") == ("heal-main", "sudo lab heal-main"))
check("check は sudo lab check", rules.normalize_action("check") == ("check", "sudo lab check"))
check("許可リストに無い処置は none でコマンド空（rm -rf / も fail-main も）",
      rules.normalize_action("rm -rf /") == ("none", "") and rules.normalize_action("fail-main") == ("none", "")
      and rules.normalize_action("") == ("none", ""))
check("ALLOWED_ACTIONS は lab/lab.sh のサブコマンド", all(f"  {a})" in read("lab", "lab.sh") for a in rules.ALLOWED_ACTIONS))
check("修復案が無ければ起こす", rules.should_start(anomaly, None) and rules.should_start(anomaly, {}))
check("同じ first_seen の修復案があれば起こさない", not rules.should_start(anomaly, {"first_seen": 1700000000, "status": "verified"}))
check("first_seen が違えば（別の発生）起こす", rules.should_start(anomaly, {"first_seen": 1600000000}))
check("anomaly_id が無ければ起こさない", not rules.should_start({}, None))
check("link_down 以外（trap など）は起こさない", not rules.should_start({**anomaly, "kind": "trap"}, None) and rules.START_KINDS == {"link_down"})
check("resolved の異常は起こさない（status が無い古い行は open 扱い）",
      not rules.should_start({**anomaly, "status": "resolved"}, None) and rules.should_start({k: v for k, v in anomaly.items() if k != "status"}, None))
check("ワークフロー id と修復案の id は発生ごと（<anomaly_id>#<first_seen>）",
      rules.workflow_id("a#b#c", 5) == "investigate-a#b#c#5" and rules.proposal_id("a#b#c", 5) == "a#b#c#5"
      and rules.proposal_id("a#b#c", None) == "a#b#c#0" and rules.workflow_id("a#b#c", "7") == "investigate-a#b#c#7")
check("event_from_message は (anomaly_id, first_seen) を読み、first_seen が読めなければ 0",
      rules.event_from_message(json.dumps({"detail": {"anomaly_id": "r1#link_down#eth1", "first_seen": 1700000000}})) == ("r1#link_down#eth1", 1700000000)
      and rules.event_from_message(json.dumps({"detail": json.dumps({"anomaly_id": "x", "first_seen": "12"})})) == ("x", 12)
      and rules.event_from_message(json.dumps({"detail": {"anomaly_id": "x", "first_seen": "bad"}})) == ("x", 0)
      and rules.event_from_message("garbage") == ("", 0))
# rules.py に boto3 / temporalio を持ち込むと、このテストも Temporal のサンドボックスも動かなくなる（分割の理由そのもの）
check("rules.py は標準ライブラリ（json / re）しか読まない",
      set(re.findall(r"^import (\w+)", read("workflow", "rules.py"), re.M)) == {"json", "re"})

# ---- workflow/awsio.py の AWS 呼び出し（差し替えで記録）
gq = lambda: [kw["gremlinQuery"] for n, op, kw in calls if op == "execute_gremlin_query"]
check("Gremlin の文字列は ' と \\ をエスケープし、改行・制御文字は \\n / \\uXXXX にする（Neptune は生の改行を受けない）",
      awsio._q("a'b\\c") == "'a\\'b\\\\c'" and awsio._q("x\ny\tz\x01") == "'x\\ny\\tz\\u0001'"
      and awsio._q(3) == "3" and awsio._q(True) == "true")
check("GraphSON の型付き値（g:List / g:Map）を素の値に戻す",
      awsio._un({"@type": "g:List", "@value": [{"@type": "g:Map", "@value": ["a", {"@type": "g:Int64", "@value": 1}]}]}) == [{"a": 1}])
calls.clear(); clients.clear()
fake["execute_gremlin_query"] = {"result": {"data": [{"id": "a#b#c", "label": "anomaly", "status": "open", "first_seen": 1}]}}
rows = awsio.list_open_anomalies()
check("open の異常を Neptune（label anomaly）から last_seen の新しい順に読み、id を anomaly_id にする",
      rows == [{"anomaly_id": "a#b#c", "status": "open", "first_seen": 1}]
      and gq()[-1] == "g.V().hasLabel('anomaly').has('status','open').order().by('last_seen',desc).limit(50).elementMap()"
      and clients[-1][0] == "neptunedata" and clients[-1][1]["endpoint_url"] == "https://nep:8182")
check("read_anomaly は 1 件、無ければ {}", awsio.read_anomaly("a#b#c")["anomaly_id"] == "a#b#c" and gq()[-1] == "g.V('a#b#c').hasLabel('anomaly').elementMap()")
fake["execute_gremlin_query"] = {"result": {"data": []}}
check("read_proposal は無ければ {}", awsio.read_proposal("p1") == {})
calls.clear()
awsio.update_proposal("p1", {"status": "applied", "apply_output": "ok\nline2"})
q = gq()[-1]
check("update_proposal は property(single, …) で status / apply_output / updated_at を書く（改行はエスケープ）",
      q.startswith("g.V('p1').hasLabel('proposal').property(single,'status','applied')") and "property(single,'apply_output','ok\\nline2')" in q
      and "property(single,'updated_at'," in q and q.endswith(".id()") and "has('status'" not in q)
awsio.update_proposal("p1", {"status": "approved"}, "pending")
check("update_proposal(only_status) は has('status', …) を同じ 1 本の Gremlin に入れる（読んでから書くあいだに割り込まれない）",
      gq()[-1].startswith("g.V('p1').hasLabel('proposal').has('status','pending').property(single,"))
check("書けなければ（空の結果）False", awsio.update_proposal("p1", {"status": "approved"}, "pending") is False)
calls.clear()
awsio.write_proposal({"proposal_id": "p1", "status": "pending", "first_seen": 1, "nothing": None, "empty": ""})
q = gq()[-1]
check("write_proposal は None と空文字を落とし、無ければ作ってから property(single, …) で書く",
      q.startswith("g.V('p1').fold().coalesce(unfold(),addV('proposal').property(id,'p1'))") and "nothing" not in q and "'empty'" not in q
      and "property(single,'first_seen',1)" in q)
fake["execute_gremlin_query"] = {"result": {"data": [True]}}
check("write_proposal(only_new) は無いときだけ作る（作れたら True）",
      awsio.write_proposal({"proposal_id": "p1", "status": "pending"}, True) is True
      and gq()[-1].startswith("g.V('p1').fold().coalesce(unfold().constant(false),addV('proposal').property(id,'p1')"))
fake["execute_gremlin_query"] = {"result": {"data": [False]}}
check("既にあれば例外にせず False（人が決めた status を pending に戻さない）", awsio.write_proposal({"proposal_id": "p1"}, True) is False)
fake["execute_gremlin_query"] = {"result": {"data": []}}
# 証跡（S3 Tables の proposal_events）
cp = awsio.catalog_properties()
check("PyIceberg は S3 Tables の Iceberg REST に SigV4（署名名 s3tables）でつなぐ",
      cp["type"] == "rest" and cp["uri"] == "https://s3tables.ap-northeast-1.amazonaws.com/iceberg" and cp["rest.signing-name"] == "s3tables"
      and cp["rest.sigv4-enabled"] == "true" and cp["warehouse"] == os.environ["AUDIT_TABLE_BUCKET_ARN"])
ev = rules.proposal_event("approved", {"proposal_id": "p1", "anomaly_id": "a", "device_id": "hq-ce-01", "decided_by": "山田 (web)"}, 1700000000, "x" * 5000)
rows = awsio.audit_rows([ev], rules.PROPOSAL_EVENT_COLUMNS)
check("audit_rows は timestamptz を UTC の datetime に、ほかは文字列にする",
      rows[0]["event_time"].isoformat() == "2023-11-14T22:13:20+00:00" and rows[0]["decided_by"] == "山田 (web)"
      and list(rows[0]) == [n for n, _ in rules.PROPOSAL_EVENT_COLUMNS])
check("proposal_event の event_id は <proposal_id>#<event>、created の status は pending、detail は 4000 字で切る",
      ev["event_id"] == "p1#approved" and ev["status"] == "approved" and len(ev["detail"]) == 4000
      and rules.proposal_event("created", {"proposal_id": "p1"}, 1)["status"] == "pending")
try:
    rules.proposal_event("deleted", {}, 1); bad = False
except ValueError:
    bad = True
check("知らない出来事は ValueError（証跡の event を増やすときは PROPOSAL_EVENTS に足す）", bad)
check("status に出てくる出来事は全部 PROPOSAL_EVENTS にある", set(proposals.STATUSES) - {"pending"} <= set(rules.PROPOSAL_EVENTS))
check("append_proposal_events は空なら何もしない（pyiceberg を読まない）", awsio.append_proposal_events([], rules.PROPOSAL_EVENT_COLUMNS) is None)

class FakeBody:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
calls.clear()
fake["invoke_agent_runtime"] = {"response": FakeBody({"status": "success", "response": '{"cause":"c","action":"check","reason":"r"}'})}
clients.clear()
text = awsio.ask_agent("q")
check("ask_agent は読み取り 150 秒・botocore の再送なし（やり直しは Temporal に任せる）",
      clients[-1][0] == "bedrock-agentcore" and clients[-1][1]["config"] == {"read_timeout": 150, "connect_timeout": 10, "retries": {"max_attempts": 1}})
kw = calls[-1][2]
check("Runtime を InvokeAgentRuntime（qualifier DEFAULT、JSON の prompt、33 字以上の runtimeSessionId）で呼ぶ",
      calls[-1][:2] == ("bedrock-agentcore", "invoke_agent_runtime") and kw["qualifier"] == "DEFAULT"
      and json.loads(kw["payload"]) == {"prompt": "q"} and len(kw["runtimeSessionId"]) >= 33 and "action" in text)
fake["invoke_agent_runtime"] = {"response": FakeBody({"status": "error", "message": "x"})}
try:
    awsio.ask_agent("q"); bad = False
except RuntimeError:
    bad = True
check("Runtime が error を返したら例外（Temporal が再試行する）", bad)
# 分割しても worker.py からは awsio / rules 経由で全部に届く（Temporal のサンドボックスを通すため imports_passed_through で囲む）
check("worker.py は awsio / rules を imports_passed_through で読む",
      re.search(r"with workflow\.unsafe\.imports_passed_through\(\):\n\s*import awsio\n\s*import rules", read("workflow", "worker.py")) is not None)

# ---- proposals.py（Neptune の label proposal。agent/graph.py の list_records / get_record / update_record 経由）
calls.clear()
fake["execute_gremlin_query"] = {"result": {"data": ["p1"]}}
r = proposals.decide("p1", "approved", "web")
q = gq()[-1]
check("承認は pending のときだけ書く 1 本の Gremlin（has と property が同じ文）",
      r["status"] == "approved" and q.startswith("g.V('p1').hasLabel('proposal').has('status','pending').property(single,'status','approved')")
      and "property(single,'decided_by','web')" in q and "property(single,'decided_at'," in q)
fake["execute_gremlin_query"] = {"result": {"data": []}}
check("pending でなければエラーの文で返す（例外にしない）", "pending ではない" in proposals.decide("p1", "rejected")["error"])
check("approved / rejected 以外は弾く", "error" in proposals.decide("p1", "applied"))
check("proposal_id が空なら弾く", "error" in proposals.decide("", "approved"))
fake["execute_gremlin_query"] = {"result": {"data": [{"id": "p1", "label": "proposal", "status": "pending", "created_at": 1700000000}]}}
r = proposals.list_proposals("pending")
check("一覧は status で絞って updated_at の新しい順に読み、JST の列を足す",
      r["count"] == 1 and r["proposals"][0]["proposal_id"] == "p1" and r["proposals"][0]["created_at_jst"].startswith("2023-11-15")
      and "has('status','pending')" in gq()[-1] and "order().by('updated_at',desc)" in gq()[-1])
proposals.list_proposals("all")
check("all は status で絞らない", "has('status'" not in gq()[-1])
check("get_proposal は 1 件", proposals.get_proposal("p1")["proposal_id"] == "p1" and gq()[-1] == "g.V('p1').hasLabel('proposal').elementMap()")
os.environ["NEPTUNE_ENDPOINT"] = ""
proposals.graph.ENDPOINT.cached = ""
check("Neptune が無ければ案内だけ返す", "terraform/pipeline/graph" in proposals.list_proposals()["error"] and "error" in proposals.decide("p1", "approved")
      and proposals.get_proposal("p1") == {})
os.environ["NEPTUNE_ENDPOINT"] = "nep:8182"

# エージェントのツール（読むだけ。2026-09-18）
calls.clear()
r = proposals.run_tool("list_proposals", {})
check("ツールの既定は all（履歴）", "has('status'" not in gq()[-1] and r["status"] == "all")
proposals.run_tool("list_proposals", {"device_id": "hq-ce-01"})
check("device_id は Gremlin の中で絞る（絞ってから limit を数える）",
      gq()[-1].index("has('device_id','hq-ce-01')") < gq()[-1].index("limit("))
check("承認・却下はツールに出さない（人が画面の承認タブで決める）",
      set(proposals.TOOLS) == {"list_proposals"} and "承認や却下はこのツールではできない" in proposals.TOOL_SPECS[0]["toolSpec"]["description"])
fake["execute_gremlin_query"] = {"result": {"data": []}}

# ---- mcp_client.py
check("JSON の応答はそのまま", mcp_client.parse_response("application/json", '{"result": {"tools": []}}') == {"result": {"tools": []}})
sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"tools___list_devices"}]}}\n\n'
check("SSE は data: 行の JSON-RPC を採る", mcp_client.parse_response("text/event-stream", sse)["result"]["tools"][0]["name"] == "tools___list_devices")
check("空の応答は {}", mcp_client.parse_response("application/json", "") == {})
specs, names = mcp_client.to_tool_specs([{"name": "tools___neighbors", "description": "d", "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string"}}, "required": ["device_id"]}}])
check("Gateway の <target>___<tool> を短い名前にして Converse の toolSpec にする",
      specs[0]["toolSpec"]["name"] == "neighbors" and names == {"neighbors": "tools___neighbors"}
      and specs[0]["toolSpec"]["inputSchema"]["json"]["required"] == ["device_id"])
mcp_client._cache.update({"url": "", "checked": 0.0, "specs": [], "names": {}, "listed": 0.0})
check("Gateway の URL が無ければツール一覧は空（app.py はコンテナ内の関数に戻す）", mcp_client.tool_specs() == [] and not mcp_client.has("neighbors"))
check("Gateway に無いツールの call はエラーの辞書", "error" in mcp_client.call("neighbors", {}))

# ---- tools.json と Python の TOOL_SPECS
tools = json.loads(read("tools", "tools.json"))
py_specs = {s["toolSpec"]["name"]: s["toolSpec"] for s in topology.TOOL_SPECS + anomalies.TOOL_SPECS + evidence.TOOL_SPECS + proposals.TOOL_SPECS}
check("tools.json の 9 つは topology / anomalies / evidence / proposals の TOOL_SPECS と同じ名前", {t["name"] for t in tools} == set(py_specs) and len(tools) == 9)
check("evidence のツールは search_logs / query_metrics / query_history", {s["toolSpec"]["name"] for s in evidence.TOOL_SPECS} == {"search_logs", "query_metrics", "query_history"})
check("handler は topology / anomalies / evidence / proposals のツールを名前で振り分ける",
      "MODULES = (topology, anomalies, evidence, proposals)" in read("tools", "handler.py"))
for t in tools:
    js = py_specs[t["name"]]["inputSchema"]["json"]
    check(f"{t['name']} の引数と必須が Python と同じ",
          set(t["inputSchema"]["properties"]) == set(js.get("properties", {})) and set(t["inputSchema"].get("required", [])) == set(js.get("required", [])))
    check(f"{t['name']} の説明が Python と同じ", t["description"] == py_specs[t["name"]]["description"])
check("tools.json の型は string / integer だけ（Gateway の inline schema が受ける形）",
      all(p["type"] in ("string", "integer") for t in tools for p in t["inputSchema"]["properties"].values()))

# ---- tools/handler.py
class Ctx:
    client_context = types.SimpleNamespace(custom={"bedrockAgentCoreToolName": "tools___list_devices"})
check("Lambda は client_context のツール名から <target>___ を外す", handler.tool_name(Ctx()) == "list_devices")
out = handler.handler({"site": "hq"}, Ctx())
check("list_devices を静的トポロジで答える（devices.json / yaml のどちらでも）", isinstance(out, dict) and out.get("count", 0) >= 1)
check("知らないツールはエラーの辞書", "error" in handler.dispatch("nope", {}))

# ---- Terraform
tf = ""
tf_files = sorted(n for n in os.listdir(TF_DIR) if n.endswith(".tf"))
for name in tf_files:
    tf += read("terraform", "workflow", name) + "\n"
check("ファイルは versions / providers / variables / locals / proposals / iam / ecs / gateway / outputs",
      set(tf_files) == {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "proposals.tf", "iam.tf", "ecs.tf", "gateway.tf", "events.tf", "outputs.tf"})
check("graph と analytics の state は try で読む（無くても apply できる）",
      '"${path.module}/../pipeline/graph/terraform.tfstate"' in tf and '"${path.module}/../pipeline/analytics/terraform.tfstate"' in tf
      and re.search(r'try\(data\.terraform_remote_state\.analytics', tf) is not None)
for root in ("base/core", "pipeline/lab", "base/ecr", "agent"):
    check(f"{root} の state をローカルから読む", f'"${{path.module}}/../{root}/terraform.tfstate"' in tf)
main_out = read("terraform", "base", "core", "outputs.tf")
lab_out = read("terraform", "pipeline", "lab", "outputs.tf"); ecr_out = read("terraform", "base", "ecr", "outputs.tf"); agent_out = read("terraform", "agent", "outputs.tf")
for out in ("vpc_id", "instance_subnet_id", "internal_security_group_id", "runtime_role_name", "web_role_name"):
    check(f"main の出力 {out} がある", f'output "{out}"' in main_out and f"outputs.{out}" in tf)
check("agent の出力 agent_runtime_arn を try で読み、無ければ precondition で止まる（terraform/agent を先に apply）",
      'output "agent_runtime_arn"' in agent_out and re.search(r'try\(data\.terraform_remote_state\.agent\.outputs\.agent_runtime_arn, ""\)', tf) is not None
      and 'condition     = local.runtime_arn != ""' in tf and "terraform/agent を先に apply" in tf)
graph_out = read("terraform", "pipeline", "graph", "outputs.tf"); analytics_out = read("terraform", "pipeline", "analytics", "outputs.tf")
check("graph の出力 cluster_endpoint / cluster_resource_id を読む（SG は 2026-09-26 に土台の internal 1 つになった）",
      all(f'output "{o}"' in graph_out and f"outputs.{o}" in tf for o in ("cluster_endpoint", "cluster_resource_id")) and "neptune_security_group_id" not in tf)
check("analytics の出力（テーブルバケット・namespace・proposal_events）を読む",
      all(f'output "{o}"' in analytics_out and f"outputs.{o}" in tf for o in ("table_bucket_arn", "table_namespace", "proposal_events_table_name")))
check("stream の state は読まない（異常の表は無くなり、異常は Neptune から読む）", "pipeline/stream/terraform.tfstate" not in tf)
check("lab の出力 lab_instance_id がある", 'output "lab_instance_id"' in lab_out and "outputs.lab_instance_id" in tf)
check("ecr の出力 worker_repository_url / temporal_repository_url がある",
      all(f'output "{o}"' in ecr_out and f"outputs.{o}" in tf for o in ("worker_repository_url", "temporal_repository_url")))
check("ECS のタスクは Fargate の ARM64", 'cpu_architecture        = "ARM64"' in tf and '"FARGATE"' in tf)
check("temporal コンテナは start-dev を SQLite で、0.0.0.0 で待つ", '"server", "start-dev", "--ip", "0.0.0.0"' in tf and "--db-filename" in tf)
check("worker は temporal の後に起き、localhost:7233 につなぐ", '"localhost:7233"' in tf and 'condition = "START"' in tf)
for env in ("NEPTUNE_ENDPOINT", "AUDIT_TABLE_BUCKET_ARN", "AUDIT_NAMESPACE", "PROPOSAL_EVENTS_TABLE", "ANOMALY_QUEUE_URL", "AGENT_RUNTIME_ARN", "LAB_INSTANCE_ID", "POLL_INTERVAL", "APPROVAL_TIMEOUT_MINUTES", "VERIFY_ATTEMPTS", "PARAM_PREFIX"):
    check(f"worker の環境変数 {env} を渡す", f'name = "{env}"' in tf or f'name  = "{env}"' in tf or re.search(rf'name\s*=\s*"{env}"', tf) is not None)
check("タスクロールは Runtime の InvokeAgentRuntime と lab への ssm:SendCommand（AWS-RunShellScript だけ）",
      '"bedrock-agentcore:InvokeAgentRuntime"' in tf and '"ssm:SendCommand"' in tf and "document/AWS-RunShellScript" in tf)
check("DynamoDB はもう使わない（異常・修復案の「いま」は Neptune、証跡は S3 Tables）",
      "aws_dynamodb" not in tf and '"dynamodb:' not in tf and "ANOMALY_TABLE" not in tf and "PROPOSAL_TABLE" not in tf)
task_doc = re.search(r'data "aws_iam_policy_document" "task" \{[\s\S]*?\n\}\n', tf)
check("タスクロールは Neptune の読み書きと、証跡テーブルの PutTableData / UpdateTableMetadataLocation",
      task_doc is not None and re.search(r'sid\s*=\s*"Neptune"', task_doc.group(0)) and '"neptune-db:WriteDataViaQuery"' in task_doc.group(0)
      and re.search(r'sid\s*=\s*"AuditTable"', task_doc.group(0)) and '"s3tables:PutTableData"' in task_doc.group(0)
      and '"s3tables:UpdateTableMetadataLocation"' in task_doc.group(0))
check("タスクと Lambda は土台の internal SG を使い、SG もポートごとのルールも作らない（2026-09-26）",
      re.search(r'security_groups\s*=\s*\[local\.internal_sg_id\]', tf) is not None and re.search(r'security_group_ids\s*=\s*\[local\.internal_sg_id\]', tf) is not None
      and 'resource "aws_security_group"' not in tf and "aws_vpc_security_group_" not in tf and "neptune_sg_id" not in tf)
check("graph と analytics が無ければ precondition で止まる（ワーカーが起きてから Neptune / 証跡に届かず落ちるより先に）",
      'local.neptune_endpoint != ""' in tf and 'local.audit_bucket_arn != ""' in tf and 'local.proposal_events_table_name != ""' in tf)
check("Runtime と Web のロールに Gateway の権限を足す", 'for_each = local.reader_role_names' in tf and '"bedrock-agentcore:InvokeGateway"' in tf)
# 承認・却下を書けるのはコードの上では web だけ（decide はツールにしない）。Neptune の IAM は頂点ごとに絞れないので、線はコードで引く
check("修復案を決める専用の IAM（decide_access）はもう無い", "decide_access" not in tf)
check("Gateway は AWS_IAM 認可の MCP で、2025-06-18 を話す", 'authorizer_type = "AWS_IAM"' in tf and 'protocol_type   = "MCP"' in tf and '"2025-06-18"' in tf)
check("Gateway のターゲットは tools.json から inline schema を作る", 'jsondecode(file("${path.module}/../../tools/tools.json"))' in tf and 'dynamic "inline_payload"' in tf)
check("tools Lambda は python3.13 arm64 で、handler.py / toolkit / topology / anomalies / proposals / graph / data を zip にする",
      'runtime          = "python3.13"' in tf and 'architectures    = ["arm64"]' in tf
      and all(f"../../{p}" in tf for p in ("tools/handler.py", "agent/toolkit.py", "agent/topology.py", "agent/anomalies.py", "agent/evidence.py", "agent/proposals.py", "agent/graph.py", "agent/data/topology.json", "agent/data/devices.yaml")))
# 入れ忘れても apply も plan も通り、実行時に ModuleNotFoundError になる。だから「入っている」ではなく「足りていないものが無い」を見る:
# zip に入れたモジュールが import する agent/ のモジュールが、全部 tools_files に並んでいるか
zipped = set(re.findall(r'"\.\./\.\./agent/(\w+)\.py"', tf))
needed = set()
for src in [("tools", "handler.py")] + [("agent", m + ".py") for m in zipped]:
    needed |= {i for i in re.findall(r"^import (\w+)$", read(*src), re.M) if os.path.exists(os.path.join(ROOT, "agent", i + ".py"))}
check(f"tools.zip は入れたモジュールが import する agent/ のモジュールを全部入れる（足りない: {sorted(needed - zipped)}）", zipped and not (needed - zipped))
check("tools Lambda は VPC の中（Neptune / OpenSearch / Prometheus に届く）で、OPENSEARCH_ENDPOINT / PROMETHEUS_QUERY_URL を渡す",
      re.search(r'resource "aws_lambda_function" "tools"[\s\S]*?vpc_config \{', tf) is not None
      and all(v in tf for v in ("OPENSEARCH_ENDPOINT", "OPENSEARCH_INDEX", "PROMETHEUS_QUERY_URL")))
# 修復案は読むだけ（Neptune の読み取りだけ。承認は画面の承認タブで人が決める）
neptune_read = re.search(r'sid\s*=\s*"NeptuneRead"[\s\S]*?\n  \}', tf)  # ステートメント 1 つぶん（terraform fmt の桁揃えに依存しないよう粗く取る）
check("tools Lambda のロールの Neptune は読むだけ（WriteDataViaQuery は付けない）",
      neptune_read is not None and "WriteDataViaQuery" not in neptune_read.group(0) and "DeleteDataViaQuery" not in neptune_read.group(0)
      and "neptune-db:ReadDataViaQuery" in neptune_read.group(0))
check("tools Lambda のロールに aoss:APIAccessAll と aps:QueryMetrics、コレクションの data access policy",
      '"aoss:APIAccessAll"' in tf and '"aps:QueryMetrics"' in tf and 'resource "aws_opensearchserverless_access_policy" "tools"' in tf)
check("EventBridge のルールは <接頭辞>.spark / AnomalyOpened を SQS（anomalies）へ、DLQ は 5 回で",
      re.search(r'resource "aws_cloudwatch_event_rule" "anomalies"[\s\S]*?source\s*=\s*\["\$\{local\.name_prefix\}\.spark"\][\s\S]*?"detail-type"\s*=\s*\["AnomalyOpened"\]', tf) is not None
      and 'resource "aws_sqs_queue" "anomalies"' in tf and 'resource "aws_sqs_queue" "anomalies_dlq"' in tf
      and re.search(r'redrive_policy[\s\S]*?maxReceiveCount\s*=\s*5', tf) is not None
      and 'resource "aws_cloudwatch_event_target" "anomalies"' in tf)
check("キューのポリシーは events.amazonaws.com の SendMessage をそのルールに絞る", '"sqs:SendMessage"' in tf and "events.amazonaws.com" in tf and "aws_cloudwatch_event_rule.anomalies.arn" in tf)
check("タスクロールは SQS の ReceiveMessage / DeleteMessage", '"sqs:ReceiveMessage", "sqs:DeleteMessage"' in tf)
check("sqs のエンドポイントは無い（SQS へは NAT Gateway 経由。2026-09-26）", 'resource "aws_vpc_endpoint"' not in tf and "create_sqs_endpoint" not in tf)
check("output に anomaly_queue_url / anomaly_rule_name / tools_function_name", all(f'output "{o}"' in tf for o in ("anomaly_queue_url", "anomaly_rule_name", "tools_function_name")))
check("Gateway の URL を SSM の gateway-url に書く", '"${local.param_prefix}/gateway-url"' in tf)
check("aws_iam_role の description は ASCII だけ",
      all(d.isascii() for d in re.findall(r'resource "aws_iam_role"[\s\S]*?description\s*=\s*"([^"]*)"', tf)))
check("Fargate のタスクは 1 vCPU / 2 GB が既定（≒ $0.05/h）", 'default     = 1024' in tf and 'default     = 2048' in tf)
check("ログの保持期間を書く", "retention_in_days = var.log_retention_days" in tf)
check("mcp_client は SigV4 のサービス名 bedrock-agentcore で署名する", '"bedrock-agentcore"' in read("agent", "mcp_client.py"))
check("agent/app.py は Gateway のツールを先に、無ければコンテナ内の関数を使う", "mcp_client.tool_specs() or TOOL_SPECS" in read("agent", "app.py") and "mcp_client.has(name)" in read("agent", "app.py"))
check("agent/Dockerfile は toolkit.py / mcp_client.py / proposals.py を入れる",
      all(f"{m}.py" in read("agent", "Dockerfile").split("COPY app.py")[1].split("\n")[0] for m in ("toolkit", "mcp_client", "proposals")))
check("workflow/Dockerfile は非 root で worker.py を打つ", "USER worker" in read("workflow", "Dockerfile") and '["python", "worker.py"]' in read("workflow", "Dockerfile"))
# 1 つずつ COPY すると、足したファイルを入れ忘れて起動時に ModuleNotFoundError になる（分割で 3 本になった）
check("workflow/Dockerfile は *.py をまとめて入れる", "COPY *.py ./" in read("workflow", "Dockerfile"))
check("workflow/requirements.txt は temporalio / boto3 / pyiceberg[pyarrow]（証跡の append）を固定する",
      all(r in read("workflow", "requirements.txt") for r in ("temporalio==", "boto3>=", "pyiceberg[pyarrow]==")))
for _f in ("worker.py", "awsio.py", "rules.py"):
    ast.parse(read("workflow", _f))
ecr_tf = read("terraform", "base", "ecr", "main.tf")
check("terraform/base/ecr は worker / temporal のリポジトリを作る", '"worker", "temporal"' in ecr_tf and 'resource "aws_ecr_repository" "workflow"' in ecr_tf)

# ---- web（app.py は画面の組み立てだけ。タブの中身は分けてある）
web = read("web", "app.py")
web_srcs = sorted(n for n in os.listdir(os.path.join(ROOT, "web")) if n.endswith(".py"))
check("web は app / config / chat / topology_view / incident_view に分かれる",
      set(web_srcs) == {"app.py", "config.py", "chat.py", "topology_view.py", "incident_view.py"})
# user_data は $APP/src/app.py の 1 行目で置き間違いを見るので、app.py の import gradio は行頭のまま動かさない
check("app.py には行頭の import gradio がある（user_data の置き間違い検出が見ている）",
      re.search(r"^import gradio as gr$", web, re.M) is not None
      and 'grep -q "^import gradio"' in read("terraform", "base", "core", "templates", "web_user_data.sh.tftpl"))
incident = read("web", "incident_view.py")
check("Web に「承認」タブがあり、名前と「読んだ」のチェックを添えて proposals.decide で approved / rejected を書く",
      'gr.Tab("承認")' in web and 'iv.decide_proposal(i, "approved", s, w, ok), [pr_id, pr_status, pr_who, pr_ok]' in web
      and 'iv.decide_proposal(i, "rejected", s, w, ok), [pr_id, pr_status, pr_who, pr_ok]' in web
      and "pr_id.change(lambda _: False, [pr_id], [pr_ok])" in web
      and "import proposals" in incident and "proposals.decide(" in incident)
check("承認・却下の結果はボタンの下の pr_result に出す（表の上の pr_msg は 30 秒ごとの描き直しが上書きし、押しても何も起きないように見える）",
      web.count("[pr_id, pr_status, pr_who, pr_ok],\n                          [pr_result, pr_table, pr_id])") == 2
      and "[pr_id, pr_status, pr_who, pr_ok], pr_out)" not in web)
# 入れ忘れても apply は通り、EC2 の起動時に ModuleNotFoundError になる（tools.zip と同じ事故）。
# web/*.py は upload_web_command が web/ ごと上げるので、確かめるのは agent/ から借りるモジュールの側
web_shared = set()
for n in web_srcs:
    web_shared |= {i for i in re.findall(r"^import (\w+)", read("web", n), re.M) if os.path.exists(os.path.join(ROOT, "agent", i + ".py"))}
uploaded = set(re.search(r"for f in ([\w ]+); do", main_out).group(1).split())
check(f"main の upload_web_command は Web が import する agent のモジュールを全部上げる（足りない: {sorted(web_shared - uploaded)}）",
      web_shared and not (web_shared - uploaded))

# ---- ops
up = read("ops", "up.sh"); down = read("ops", "down.sh"); chk = read("ops", "check.sh")
check("up.sh の WORKFLOW=1 は AGENT と PIPELINE が要り、SKIP_LAB / SKIP_STREAM / SKIP_ANALYTICS があれば止まる",
      re.search(r'if \[ -n "\$WORKFLOW" \]; then\n\s*if \[ -z "\$AGENT" \]; then[\s\S]*?if \[ -z "\$PIPELINE" \]; then[\s\S]*?SKIP_LAB[\s\S]*?SKIP_STREAM[\s\S]*?SKIP_ANALYTICS', up) is not None)
check("deploy-env.sh の読めるキーは機能の 3 つ + CREATE_KB + TF_VERBOSE で、古いキー（PHASE / SINKS / WITH_*）は持たない",
      (lambda keys: all(k in keys for k in ("PIPELINE", "AGENT", "WORKFLOW", "CREATE_KB", "TF_VERBOSE"))
       and not any(k in keys for k in ("PHASE", "SINKS", "WITH_LAB", "WITH_STREAM")))(read("ops", "deploy-env.sh").split('DEPLOY_ENV_KEYS="')[1].split('"')[0].split())
      and not re.search(r'\bPHASE\b|\bWITH_LAB\b|\bWITH_STREAM\b', up))
# ---- starter: SQS のメッセージから anomaly_id
check("anomaly_id_from_message は detail が dict でも JSON 文字列でも読む",
      rules.anomaly_id_from_message(json.dumps({"detail": {"anomaly_id": "r1#link_down#eth1"}})) == "r1#link_down#eth1"
      and rules.anomaly_id_from_message(json.dumps({"detail": json.dumps({"anomaly_id": "r1#link_down#eth1"})})) == "r1#link_down#eth1")
check("anomaly_id_from_message はごみを空にする",
      rules.anomaly_id_from_message("garbage") == "" and rules.anomaly_id_from_message("[1]") == ""
      and rules.anomaly_id_from_message(json.dumps({"detail": "x"})) == "" and rules.anomaly_id_from_message(json.dumps({"detail": {}})) == "")
check("starter は ANOMALY_QUEUE_URL があれば SQS（20 秒の long polling）、無ければテーブルを見る",
      all(hasattr(worker, f) for f in ("start_for", "starter_queue", "starter_table"))
      and all(hasattr(awsio, f) for f in ("receive_messages", "delete_message"))
      and "WaitTimeSeconds=20" in read("workflow", "awsio.py")
      and "starter_queue if awsio.ANOMALY_QUEUE_URL else starter_table" in read("workflow", "worker.py"))
check("up.sh は workflow ルートを足し、費用に 5 セント足す（Fargate だけ。sqs のエンドポイントは無くなった）", 'ROOTS="$ROOTS workflow"' in up and 'COST_CENTS=$((COST_CENTS + 5))' in up)
check("up.sh は worker を buildx でビルドし、temporalio/temporal を ECR にミラーする",
      '--push workflow/' in up and 'docker pull --platform linux/arm64 "temporalio/temporal:$TEMPORAL_TAG"' in up and "$PREFIX-temporal:$TEMPORAL_TAG" in up)
check("up.sh の TEMPORAL_TAG は terraform/workflow の temporal_image_tag の既定値と同じ",
      re.search(r'^TEMPORAL_TAG=(\S+)', up, re.M).group(1) == re.search(r'variable "temporal_image_tag"[\s\S]*?default\s*=\s*"([^"]+)"', tf).group(1))
check("up.sh は workflow を apply して services-stable を待ち、Temporal UI のポートフォワーディングを案内する",
      'tf_apply workflow -var "worker_image_tag=$IMAGE_TAG"' in up and 'aws ecs wait services-stable' in up and 'AWS-StartPortForwardingSessionToRemoteHost' in up)
check("down.sh は workflow を最初に消す（必須変数はダミーで渡す）",
      down.index('destroy_lambda_root workflow') < down.index('destroy_root pipeline/analytics') and 'worker_image_tag=${IMAGE_TAG:-destroy}' in down)
# .py を名指しで並べると、ファイルを足したときに構文検査から漏れる（分割で 7 本増えた）。find に任せているかを見る
check("check.sh は workflow ルートとこのテストを見て、.py は名指しせず find で全部見る",
      "workflow)" in chk and "tests/test_workflow.py" in chk
      and re.search(r"find [\w /]*\bworkflow\b [^\n]*-name '\*\.py'", chk) is not None and "ast.parse(" in chk)
check("deploy.env.example は AGENT=1 / PIPELINE=0 / WORKFLOW=0 を既定にし、CREATE_KB を説明する（古い PHASE の行は載せない）",
      re.search(r"^AGENT=1\n^PIPELINE=0\n^WORKFLOW=0$", read("deploy.env.example"), re.M) is not None
      and re.search(r"^#CREATE_KB=0$", read("deploy.env.example"), re.M) is not None and re.search(r"^#?\s*PHASE=", read("deploy.env.example"), re.M) is None
      and "workflow" in read("deploy.env.example"))
check("down.sh は VPC の Lambda を持つルート（workflow / graph）を消す間、その関数の available な ENI だけを裏で消す（2026-09-18）",
      'destroy_lambda_root workflow "$PREFIX-tools"' in down and 'destroy_lambda_root pipeline/graph "$PREFIX-graph-status"' in down
      and 'Values=AWS Lambda VPC ENI-$1-*" Name=status,Values=available' in down and "kill -0 $$" in down)
_envsh = read("ops", "deploy-env.sh")
check("terraform の出力は tf_logged で絞り、全文を ops/logs に残す。TF_VERBOSE=1 で全部出す。up.sh と down.sh の両方が通す",
      "tf_logged()" in _envsh and 'tee "$logf"' in _envsh and 'if [ -n "$TF_VERBOSE" ]' in _envsh
      and 'tf_logged "$root" apply' in read("ops", "up.sh") and 'tf_logged "$root" destroy' in down
      and re.search(r"^set -e?uo pipefail", down, re.M) is not None)
# TF_VERBOSE=1 のときログを残さないと、down.sh が DependencyViolation と掴んでいる SG を読めず打ち直しが効かない
check("tf_logged は TF_VERBOSE=1 の枝でも全文を ops/logs に残す", _envsh[_envsh.index("tf_logged() {"):].count('tee "$logf"') == 2)
check("down.sh は 1 ルートが消えなくても止まらず、残りを消してから最後にまとめて出す（止まると後ろの EC2 が動いたまま残る）",
      "FAILED_ROOTS=" in down and 'FAILED_ROOTS="$FAILED_ROOTS $root"' in down
      and re.search(r'if \[ -n "\$FAILED_ROOTS" \]; then[\s\S]*?exit 1', down) is not None
      and re.search(r'destroy_root\(\)[\s\S]*?\n\}', down).group(0).count("die ") == 0)
# ---- terraform/agent と terraform/base/core の分担
agent_files = set(n for n in os.listdir(os.path.join(ROOT, "terraform", "agent")) if n.endswith(".tf"))
check("terraform/agent のファイルは versions / providers / variables / locals / runtime / kb / outputs（network.tf は 2026-09-26 に無くなった）",
      agent_files == {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "runtime.tf", "kb.tf", "outputs.tf"})
agent_tf = "".join(read("terraform", "agent", n) for n in sorted(agent_files))
main_tf = "".join(read("terraform", "base", "core", n) for n in sorted(os.listdir(os.path.join(ROOT, "terraform", "base", "core"))) if n.endswith(".tf"))
check("Runtime / ガードレール / KB は terraform/agent にあり、terraform/base/core には無い。agent にエンドポイントは無く、Runtime は土台の internal SG を使う",
      all(r in agent_tf for r in ('resource "aws_bedrockagentcore_agent_runtime" "agent"', 'resource "aws_bedrock_guardrail" "this"', 'resource "aws_bedrockagent_knowledge_base" "kb"'))
      and 'resource "aws_vpc_endpoint"' not in agent_tf and 'resource "aws_security_group"' not in agent_tf
      and re.search(r'runtime_sg_id\s*=\s*data\.terraform_remote_state\.main\.outputs\.internal_security_group_id', agent_tf) is not None
      and not any(r in main_tf for r in ("aws_bedrockagentcore_agent_runtime", "aws_bedrock_guardrail", "aws_bedrockagent_knowledge_base", "aws_opensearchserverless")))
check("KB は create_knowledge_base（既定 false）の count で作り、Runtime は KB があるときだけ KNOWLEDGE_BASE_ID を受ける",
      re.search(r'variable "create_knowledge_base"[\s\S]*?default\s*=\s*false', agent_tf) is not None and 'resource "aws_bedrockagent_knowledge_base" "kb" {\n  count = local.kb ? 1 : 0' in agent_tf
      and re.search(r'local\.kb \? \{\n\s*KNOWLEDGE_BASE_ID', agent_tf) is not None)
check("Runtime の ARN は agent が SSM に書き、web はそれを読む（main は runtime_arn を user_data に渡さない）",
      'resource "aws_ssm_parameter" "runtime_arn"' in agent_tf and 'name        = "${local.param_prefix}/runtime-arn"' in agent_tf
      and "runtime_arn" not in read("terraform", "base", "core", "templates", "web_user_data.sh.tftpl")
      and 'toolkit.Param("RUNTIME_ARN", "runtime-arn")' in read("web", "chat.py")
      and 'ssm:GetParameter' in main_tf)
check("agent は web のロールに InvokeAgentRuntime を付け、main の runtime ロールにポリシーを足す",
      'role = local.web_role_name' in agent_tf and 'bedrock-agentcore:InvokeAgentRuntime' in agent_tf and 'role = local.runtime_role_name' in agent_tf
      and 'output "runtime_role_arn"' in main_out and 'resource "aws_iam_role" "runtime"' in main_tf)
check("down.sh は Runtime の ENI が残るあいだ VPC・サブネット・internal の SG を残して他（NAT Gateway も）を消す",
      "InterfaceType=='agentic_ai'" in down and "Name=tag:Name,Values=$PREFIX-vpc" in down and "tf base/core output -raw vpc_id" not in down and "Runtime の ENI の確認:" in down
      and '""|data.*|aws_vpc.this|aws_subnet.*|aws_security_group.internal) ;;' in down and "aws_security_group.runtime" not in down
      and down.index("InterfaceType=='agentic_ai'") < down.index("destroy_root base/core") < down.index("destroy_root base/ecr"))
check("down.sh は agent を lab の後、main の前に消し、ロググループ名を agent の state から読む",
      down.index("destroy_root pipeline/lab") < down.index("destroy_root agent") < down.index("destroy_root base/core") and "tf agent output -raw runtime_log_group_name" in down)
check("up.sh は main の後に agent を apply し、CREATE_KB のときだけ手順書を取り込む",
      up.index("tf_apply base/core") < up.index('tf_apply agent "${AGENT_VARS[@]}"') < up.index("start-ingestion-job")
      and re.search(r'if \[ -n "\$CREATE_KB" \]; then\nlog "4-3\. 手順書を置いて取り込む', up) is not None and 'AGENT_VARS+=(-var create_knowledge_base=true)' in up)

# ---- 2026-09-18 実機: wait_condition の timeout は asyncio.TimeoutError で、握らないとワークフロー自体が失敗して承認が拾えない
wsrc = read("workflow", "worker.py")
check("承認待ちの wait_condition は TimeoutError を握って表を見直す（漏らすとワークフロー失敗）",
      "except asyncio.TimeoutError" in wsrc and wsrc.index("wait_condition(") < wsrc.index("except asyncio.TimeoutError"))
check("承認タブの注記はワークフローが Temporal であることを言い、表は折り返し、id は表から選べる",
      "Temporal" in web and "wrap=True" in web and "pr_id = gr.Dropdown(" in web and "proposal_detail" in web)

# ---- ops/up.sh が Web を立てる手順（2026-09-19 実機: app.py だけ置いて chat が無く、起動のたびに落ちていたのに「Web が動いている」と出た）
up = read("ops", "up.sh")
web_imports = {m for m in re.findall(r"^(?:import|from) (\w+)", read("web", "app.py"), re.M) if os.path.exists(os.path.join(ROOT, "web", m + ".py"))}
check(f"up.sh 4-2 は web/*.py を全部置く（app.py が import する {sorted(web_imports)} を含む）",
      web_imports and 'for f in web/*.py; do aws s3 cp --only-show-errors "$f" "s3://$KB_BUCKET/web/${f#web/}"; done' in up)
check("Web の起動確認は is-active（落ちて再起動するまでの数秒も active）ではなく 8080 を聞いているかで見る",
      "ss -ltn 'sport = :8080' | grep -q LISTEN" in up and "systemctl is-active --quiet $PREFIX-web.service" not in up)
check("lab の状態の照合は 1 つの空白で区切った lab=active containers=N をそのまま探す（空白を 2 つ要る形だと合わない）",
      '*" lab=active containers=$LAB_NODES "*)' in up)

# ---- ワーカーの振る舞い（2026-09-24 のレビュー: 発生ごとの id・承認のあいだに閉じた異常・apply の失敗・SQS の消し方）
import asyncio, datetime, logging  # noqa: E402
_saved = {k: getattr(awsio, k) for k in ("read_anomaly", "read_proposal", "write_proposal", "update_proposal", "append_proposal_events",
                                         "receive_messages", "delete_message")}
audited = []  # 証跡（proposal_events）に足した行
awsio.append_proposal_events = lambda rows, columns: audited.extend(rows)
FS = 1700000000
AID = anomaly["anomaly_id"]

def run_wf(script, first_seen=FS):
    """InvestigateAnomaly.run を、アクティビティを script（名前 → 返り値 / 例外 / 関数）に差し替えて回す。(結果, 呼んだアクティビティ)"""
    seen = []
    async def execute_activity(fn, *a, args=None, **opts):
        params = list(args) if args is not None else list(a)
        seen.append((fn.__name__, params, opts))
        r = script[fn.__name__]
        r = r(*params) if callable(r) else r
        if isinstance(r, BaseException):
            raise r
        return r
    async def wait_condition(fn, timeout=None):
        raise asyncio.TimeoutError
    t_workflow.execute_activity = execute_activity; t_workflow.wait_condition = wait_condition
    t_workflow.now = datetime.datetime.now; t_workflow.info = lambda: types.SimpleNamespace(workflow_id="wf-1")
    t_workflow.logger = logging.getLogger("wf")
    worker.VERIFY_INTERVAL = 0
    return asyncio.run(worker.InvestigateAnomaly().run(AID, first_seen)), seen

finding = {"cause": "c", "action": "heal-main", "command": "sudo lab heal-main", "reason": "r", "agent_response": "{}"}
base = {"get_anomaly": anomaly, "investigate": finding, "put_proposal": f"{AID}#{FS}", "get_decision": "approved",
        "record_decision": lambda pid, d, via=False: d, "set_status": None, "still_open": True, "apply_on_lab": {"status": "Success", "output": "ok"}, "anomaly_resolved": True}
res, seen = run_wf(base)
names = [n for n, _, _ in seen]
check("承認→打つ→閉じたら verified。確かめは同じ発生（anomaly_id + first_seen）で見る",
      res == "verified" and names.index("still_open") < names.index("apply_on_lab")
      and [p for n, p, _ in seen if n == "anomaly_resolved"][0] == [AID, FS] and [p for n, p, _ in seen if n == "still_open"][0] == [AID, FS])
check("investigate の start_to_close は 4 分（AgentCore の読み取り 150 秒 1 回分が収まる）",
      [o for n, _, o in seen if n == "investigate"][0]["start_to_close_timeout"] == datetime.timedelta(minutes=4))
res, seen = run_wf({**base, "still_open": False})
check("承認のあいだに異常が閉じていれば打たずに obsolete",
      res == "obsolete" and "apply_on_lab" not in [n for n, _, _ in seen]
      and [p[1] for n, p, _ in seen if n == "set_status"] == ["obsolete"])
res, seen = run_wf({**base, "apply_on_lab": ActivityError("activity failed", cause=RuntimeError("SSM に届かない"))})
st = [p for n, p, _ in seen if n == "set_status"]
check("apply_on_lab の失敗（ActivityError）はワークフローを落とさず failed を書く（approved のまま残さない）",
      res == "failed" and st[-1][1] == "failed" and "SSM に届かない" in st[-1][2]["apply_output"] and "anomaly_resolved" not in [n for n, _, _ in seen])
res, seen = run_wf({**base, "get_anomaly": {**anomaly, "status": "resolved"}})
check("起こしたあとで閉じていれば調べずに obsolete", res == "obsolete" and [n for n, _, _ in seen] == ["get_anomaly"])
res, seen = run_wf({**base, "get_anomaly": {**anomaly, "first_seen": FS + 60}})
check("開き直して別の発生になっていれば調べずに obsolete", res == "obsolete" and [n for n, _, _ in seen] == ["get_anomaly"])
res, seen = run_wf({**base, "get_decision": "rejected"})
check("却下なら何もしない（判断は record_decision で証跡に残す）", res == "rejected" and "apply_on_lab" not in [n for n, _, _ in seen]
      and "still_open" not in [n for n, _, _ in seen] and [p for n, p, _ in seen if n == "record_decision"] == [[f"{AID}#{FS}", "rejected", False]])
res, seen = run_wf({**base, "record_decision": "rejected"})
check("シグナルで approved が来ても、web が先に rejected を書いていれば（record_decision が返す方）打たない",
      res == "rejected" and "apply_on_lab" not in [n for n, _, _ in seen])
_timeout, worker.APPROVAL_TIMEOUT_MINUTES = worker.APPROVAL_TIMEOUT_MINUTES, 0  # 待たずに時間切れにする
res, seen = run_wf({**base, "get_decision": "pending"})
worker.APPROVAL_TIMEOUT_MINUTES = _timeout
check("時間切れは expired を書く（証跡は set_status が残す）", res == "expired" and [p[1] for n, p, _ in seen if n == "set_status"] == ["expired"])

# アクティビティ（awsio を差し替え）
awsio.read_anomaly = lambda aid: {**anomaly, "first_seen": FS + 60}
check("anomaly_resolved は開き直して first_seen が変わったら「この発生は閉じた」", asyncio.run(worker.anomaly_resolved(AID, FS)) is True)
check("still_open は first_seen が違えば False", asyncio.run(worker.still_open(AID, FS)) is False and asyncio.run(worker.still_open(AID, FS + 60)) is True)
awsio.read_anomaly = lambda aid: anomaly
check("anomaly_resolved は同じ発生が open なら False", asyncio.run(worker.anomaly_resolved(AID, FS)) is False)
written = []
awsio.write_proposal = lambda item, only_new=False: written.append((item, only_new)) or True
pid = asyncio.run(worker.put_proposal(anomaly, finding, "wf-1"))
check("put_proposal は <anomaly_id>#<first_seen> を only_new で書き、証跡に created を足す",
      pid == f"{AID}#{FS}" and written[-1][0]["proposal_id"] == pid and written[-1][1] is True
      and audited[-1]["event_id"] == f"{pid}#created" and audited[-1]["status"] == "pending" and audited[-1]["detail"] == "r")
awsio.write_proposal = lambda item, only_new=False: False
awsio.read_proposal = lambda p: {"proposal_id": p, "workflow_id": "wf-1", "status": "approved"}
check("既にある修復案が自分の書いたもの（書けたあとで再試行）なら、それを使って進む", asyncio.run(worker.put_proposal(anomaly, finding, "wf-1")) == pid)
awsio.read_proposal = lambda p: {"proposal_id": p, "workflow_id": "wf-other"}
try:
    asyncio.run(worker.put_proposal(anomaly, finding, "wf-1")); err = None
except ApplicationError as e:
    err = e
check("別のワークフローの修復案なら再試行しない失敗（上書きしない）", err is not None and err.non_retryable)

# 人の判断と状態の移り変わりは、頂点に書いたうえで証跡にも 1 行ずつ残す
updated = []
awsio.update_proposal = lambda p, fields, only_status=None: updated.append((p, fields, only_status)) or True
awsio.read_proposal = lambda p: {"proposal_id": p, "anomaly_id": AID, "status": "approved", "decided_by": "山田 (web)", "command": "sudo lab heal-main"}
audited.clear()
check("record_decision（web が決めた）は頂点を書かず、決めた人ごと証跡に残す",
      asyncio.run(worker.record_decision(pid, "approved")) == "approved" and updated == []
      and audited[-1]["event_id"] == f"{pid}#approved" and audited[-1]["decided_by"] == "山田 (web)" and audited[-1]["command"] == "sudo lab heal-main")
awsio.read_proposal = lambda p: {"proposal_id": p, "status": "rejected", "decided_by": "鈴木 (web)"}
check("シグナルで決めたときは pending のときだけ頂点に書き、効いた方（web が先なら web の判断）を返して残す",
      asyncio.run(worker.record_decision(pid, "approved", True)) == "rejected"
      and updated[-1][1]["status"] == "approved" and updated[-1][1]["decided_by"] == "temporal-signal" and updated[-1][2] == "pending"
      and audited[-1]["event_id"] == f"{pid}#rejected")
awsio.read_proposal = lambda p: {"proposal_id": p, "status": "applied"}
asyncio.run(worker.set_status(pid, "applied", {"apply_output": "Success: ok"}))
check("set_status は頂点を書き、証跡に apply_output を detail として残す",
      updated[-1] == (pid, {"status": "applied", "apply_output": "Success: ok"}, None)
      and audited[-1]["event_id"] == f"{pid}#applied" and audited[-1]["detail"] == "Success: ok")

# starter（SQS のメッセージ 1 通ずつ）
class FakeTemporal:
    def __init__(self, exc=None):
        self.exc, self.started = exc, []
    async def start_workflow(self, fn, args=None, id=None, task_queue=None):
        self.started.append((args, id))
        if self.exc:
            raise self.exc
msg = lambda fs: json.dumps({"detail": {"anomaly_id": AID, "first_seen": fs}})
awsio.read_proposal = lambda p: {}
tc = FakeTemporal()
asyncio.run(worker.handle_message(tc, msg(FS)))
check("メッセージの発生と表の発生が同じなら、その発生のワークフローを起こす",
      tc.started == [([AID, FS], f"investigate-{AID}#{FS}")])
tc = FakeTemporal()
asyncio.run(worker.handle_message(tc, msg(FS - 60)))
asyncio.run(worker.handle_message(tc, "garbage"))
awsio.read_anomaly = lambda aid: {}
asyncio.run(worker.handle_message(tc, msg(FS)))
check("古い発生・読めない本文・表に無い異常は起こさない（例外にもしない = 消す）", tc.started == [])
awsio.read_anomaly = lambda aid: anomaly
deleted = []
awsio.receive_messages = lambda: [{"Body": msg(FS), "ReceiptHandle": "r1"}]
awsio.delete_message = lambda h: deleted.append(h)
asyncio.run(worker.starter_queue(FakeTemporal(WorkflowAlreadyStarted())))
check("もう起きている（WorkflowAlreadyStartedError = 同じ発生の重複配達）なら消す（残すと DLQ で本物の失敗と混ざる）", deleted == ["r1"])
deleted.clear()
asyncio.run(worker.starter_queue(FakeTemporal(RuntimeError("temporal に届かない"))))
check("それ以外の失敗は消さずに残す（可視性タイムアウトのあとで配り直し、5 回で DLQ）", deleted == [])
awsio.read_anomaly = lambda aid: (_ for _ in ()).throw(OSError("Neptune に届かない"))
asyncio.run(worker.starter_queue(FakeTemporal()))
check("Neptune に届かないときも消さない", deleted == [])
for k, v in _saved.items():
    setattr(awsio, k, v)

# ---- web/incident_view.py の承認（gradio / pandas / config は差し替えて読む。config は環境変数と env ファイルを読むので本物は使わない）
_gr = types.ModuleType("gradio"); _gr.update = lambda **k: ("update", k)
_pd = types.ModuleType("pandas"); _pd.DataFrame = lambda rows, columns=None: rows
_web_mods = {"gradio": _gr, "pandas": _pd, "config": types.ModuleType("config")}
_prev = {k: sys.modules.get(k) for k in _web_mods}
sys.modules.update(_web_mods)
sys.path.insert(0, os.path.join(ROOT, "web"))
import incident_view as iv  # noqa: E402
for k, v in _prev.items():
    if v is None:
        sys.modules.pop(k, None)
    else:
        sys.modules[k] = v
decided = []
iv.proposals.decide = lambda pid, d, decided_by="": decided.append((pid, d, decided_by)) or {"proposal_id": pid, "status": d}
iv.proposals.list_proposals = lambda status="pending", limit=100: {"proposals": [{"proposal_id": "p1", "status": status}]}
nothing = ("update", {})
check("名前が無ければ書かない（表と選択もそのまま）",
      iv.decide_proposal("p1", "rejected", "pending", "  ", True)[1:] == (nothing, nothing) and decided == [])
ab = lambda ok, name: iv.approve_button(ok, name)[1]  # テストの gr.update は ("update", kwargs) を返す
check("承認ボタンは名前とチェックがそろうまで押せず、足りないものを文字で出す",
      ab(False, "")["interactive"] is False and "名前とチェックが要る" in ab(False, "")["value"]
      and ab(True, " ")["interactive"] is False and "名前が要る" in ab(True, " ")["value"]
      and "チェックが要る" in ab(False, "yamada")["value"]
      and ab(True, "yamada") == {"value": "③ 承認して直す", "interactive": True})
check("承認のチェックは承認ボタンの真上（同じ Column）にあり、チェックと名前が変わるたびにボタンを描き直す",
      re.search(r"with gr\.Column\(scale=2\):\n\s+pr_ok = gr\.Checkbox\(label=iv\.APPROVE_CHECK_LABEL.*\n\s+pr_approve = gr\.Button\(", web) is not None
      and "pr_ok.change(iv.approve_button, [pr_ok, pr_who], [pr_approve])" in web
      and "pr_who.change(iv.approve_button, [pr_ok, pr_who], [pr_approve])" in web)
check("状態は表示だけ日本語で、ラジオの値・一覧に渡す値は英語のまま（Neptune の status と同じ）",
      set(iv.PROPOSAL_STATUS_JA) == set(proposals.STATUSES) | {"all"} and set(iv.ANOMALY_STATUS_JA) == {"open", "resolved"}
      and ("承認待ち", "pending") in iv.status_choices(iv.PROPOSAL_STATUS_JA)
      and "gr.Radio(iv.status_choices(iv.PROPOSAL_STATUS_JA), value=\"pending\"" in web
      and "gr.Radio(iv.status_choices(iv.ANOMALY_STATUS_JA), value=\"open\"" in web
      and iv.proposal_table("pending")[0].endswith("件（承認待ち）") and iv.proposal_table("pending")[1][0]["状態"] == "承認待ち")
check("承認は「読んだ」のチェックが無ければ書かない", iv.decide_proposal("p1", "approved", "pending", "yamada", False)[1:] == (nothing, nothing) and decided == [])
check("proposal_id が空なら書かない", iv.decide_proposal("", "approved", "pending", "yamada", True)[1:] == (nothing, nothing) and decided == [])
r = iv.decide_proposal("p1", "approved", "pending", "  山田   太郎 ", True)
check("名前は空白を詰めて「<名前> (web)」で decided_by に残し、選択は空に戻す（続けて押しても別の行に書かない）",
      decided == [("p1", "approved", "山田 太郎 (web)")] and r[2] == ("update", {"choices": ["p1"], "value": None}))
iv.decide_proposal("p1", "rejected", "pending", "x" * 100)
check(f"却下はチェック無しで通り、名前は {iv.APPROVER_MAX} 字で切る", decided[-1] == ("p1", "rejected", "x" * iv.APPROVER_MAX + " (web)"))

# ---- Temporal UI（8233）: 2026-09-24 のレビューではポートごとの SG ルールの抜けで UI が開かなかった。2026-09-26 に SG を internal 1 つにしたので、ルール自体が無い
check("Temporal UI（8233）にポートごとの SG ルールは無く、Web の EC2 とタスクは同じ internal SG",
      "task_ui_from_web" not in tf and "web_to_task_ui" not in tf and "web_sg_id" not in tf and "instance_security_group_id" not in tf
      and 'output "internal_security_group_id"' in main_out and "outputs.internal_security_group_id" in tf)
check("修復案の status に obsolete がある（tools.json の説明も）", "obsolete" in proposals.STATUSES and all("obsolete" in t["description"] for t in tools if t["name"] == "list_proposals"))
print(f"通過 {passed} / 失敗 0")
