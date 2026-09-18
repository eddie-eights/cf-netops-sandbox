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
fake = {"get_item": {}, "update_item": None, "query": {"Items": []}, "get_parameter": ClientError("ParameterNotFound")}
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
boto3.client = lambda name, **kw: FakeClient(name)
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
t_exc = types.ModuleType("temporalio.exceptions"); t_exc.WorkflowAlreadyStartedError = Exception
t_worker = types.ModuleType("temporalio.worker"); t_worker.Worker = object
t_root = types.ModuleType("temporalio")
sys.modules.update({"temporalio": t_root, "temporalio.activity": t_activity, "temporalio.workflow": t_workflow,
                    "temporalio.client": t_client, "temporalio.common": t_common, "temporalio.exceptions": t_exc,
                    "temporalio.worker": t_worker})

os.environ.update({"ANOMALY_TABLE": "anom", "PROPOSAL_TABLE": "prop", "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/x",
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
anomaly = {"anomaly_id": "hq-ce-01#link_down#eth1", "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1",
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
check("ワークフロー id は investigate-<anomaly_id>", rules.workflow_id("a#b#c") == "investigate-a#b#c")
# rules.py に boto3 / temporalio を持ち込むと、このテストも Temporal のサンドボックスも動かなくなる（分割の理由そのもの）
check("rules.py は標準ライブラリ（json / re）しか読まない",
      set(re.findall(r"^import (\w+)", read("workflow", "rules.py"), re.M)) == {"json", "re"})

# ---- workflow/awsio.py の AWS 呼び出し（差し替えで記録）
check("DynamoDB の型付けは N / S / BOOL で、空文字は - にする",
      awsio._typed(1) == {"N": "1"} and awsio._typed(True) == {"BOOL": True} and awsio._typed("x") == {"S": "x"} and awsio._typed("") == {"S": "-"})
calls.clear()
fake["query"] = {"Items": [{"anomaly_id": {"S": "a#b#c"}, "status": {"S": "open"}, "first_seen": {"N": "1"}}]}
rows = awsio.list_open_anomalies()
check("open の異常を status-last_seen-index で新しい順に読む", rows == [{"anomaly_id": "a#b#c", "status": "open", "first_seen": 1}]
      and calls[-1][2]["IndexName"] == "status-last_seen-index" and calls[-1][2]["ScanIndexForward"] is False and calls[-1][2]["TableName"] == "anom")
calls.clear()
awsio.update_proposal("p1", {"status": "applied", "apply_output": "ok"})
kw = calls[-1][2]
check("update_proposal は status / apply_output / updated_at を SET する",
      calls[-1][:2] == ("dynamodb", "update_item") and kw["TableName"] == "prop" and kw["UpdateExpression"].startswith("SET ")
      and set(kw["ExpressionAttributeNames"].values()) == {"status", "apply_output", "updated_at"})
calls.clear()
awsio.write_proposal({"proposal_id": "p1", "status": "pending", "first_seen": 1, "nothing": None})
check("write_proposal は None を落として put_item する", calls[-1][1] == "put_item" and "nothing" not in calls[-1][2]["Item"]
      and calls[-1][2]["Item"]["first_seen"] == {"N": "1"})

class FakeBody:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
calls.clear()
fake["invoke_agent_runtime"] = {"response": FakeBody({"status": "success", "response": '{"cause":"c","action":"check","reason":"r"}'})}
text = awsio.ask_agent("q")
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

# ---- proposals.py
calls.clear()
os.environ["PROPOSAL_TABLE"] = "prop"
r = proposals.decide("p1", "approved", "web")
kw = calls[-1][2]
check("承認は pending のときだけ通る ConditionExpression 付きの UpdateItem",
      r["status"] == "approved" and kw["ConditionExpression"] == "#s = :p" and kw["ExpressionAttributeValues"][":p"] == {"S": "pending"}
      and kw["ExpressionAttributeValues"][":d"] == {"S": "approved"} and "decided_by = :b" in kw["UpdateExpression"])
fake["update_item"] = ClientError("ConditionalCheckFailedException")
check("pending でなければエラーの文で返す（例外にしない）", "pending ではない" in proposals.decide("p1", "rejected")["error"])
fake["update_item"] = None
check("approved / rejected 以外は弾く", "error" in proposals.decide("p1", "applied"))
check("proposal_id が空なら弾く", "error" in proposals.decide("", "approved"))
fake["query"] = {"Items": [{"proposal_id": {"S": "p1"}, "status": {"S": "pending"}, "created_at": {"N": "1700000000"}}]}
r = proposals.list_proposals("pending")
check("一覧は status-updated_at-index を新しい順に読み、JST の列を足す",
      r["count"] == 1 and r["proposals"][0]["created_at_jst"].startswith("2023-11-15") and calls[-1][2]["IndexName"] == proposals.INDEX)
fake["scan"] = {"Items": []}
check("all は Scan", proposals.list_proposals("all")["count"] == 0 and calls[-1][1] == "scan")
os.environ["PROPOSAL_TABLE"] = ""
proposals.TABLE.cached = ""
check("テーブルが無ければ案内だけ返す", "error" in proposals.list_proposals() and "error" in proposals.decide("p1", "approved"))
os.environ["PROPOSAL_TABLE"] = "prop"

# エージェントのツール（読むだけ。2026-09-18）
calls.clear()
fake["scan"] = {"Items": [
    {"proposal_id": {"S": "p1"}, "device_id": {"S": "hq-ce-01"}, "status": {"S": "verified"}, "updated_at": {"N": "1700000000"}},
    {"proposal_id": {"S": "p2"}, "device_id": {"S": "br1-ce-01"}, "status": {"S": "rejected"}, "updated_at": {"N": "1700000900"}}]}
r = proposals.run_tool("list_proposals", {})
check("ツールの既定は all（履歴）で、新しい順に返す",
      calls[-1][1] == "scan" and [p["proposal_id"] for p in r["proposals"]] == ["p2", "p1"])
check("device_id で機器を絞れる", [p["proposal_id"] for p in proposals.run_tool("list_proposals", {"device_id": "hq-ce-01"})["proposals"]] == ["p1"])
check("承認・却下はツールに出さない（人が画面の承認タブで決める）",
      set(proposals.TOOLS) == {"list_proposals"} and "承認や却下はこのツールではできない" in proposals.TOOL_SPECS[0]["toolSpec"]["description"])

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
for root in ("base/core", "pipeline/stream", "pipeline/lab", "base/ecr", "agent"):
    check(f"{root} の state をローカルから読む", f'"${{path.module}}/../{root}/terraform.tfstate"' in tf)
main_out = read("terraform", "base", "core", "outputs.tf"); stream_out = read("terraform", "pipeline", "stream", "outputs.tf")
lab_out = read("terraform", "pipeline", "lab", "outputs.tf"); ecr_out = read("terraform", "base", "ecr", "outputs.tf"); agent_out = read("terraform", "agent", "outputs.tf")
for out in ("vpc_id", "instance_subnet_id", "endpoint_security_group_id", "runtime_role_name", "web_role_name"):
    check(f"main の出力 {out} がある", f'output "{out}"' in main_out and f"outputs.{out}" in tf)
check("agent の出力 agent_runtime_arn を try で読み、無ければ precondition で止まる（terraform/agent を先に apply）",
      'output "agent_runtime_arn"' in agent_out and re.search(r'try\(data\.terraform_remote_state\.agent\.outputs\.agent_runtime_arn, ""\)', tf) is not None
      and 'condition     = local.runtime_arn != ""' in tf and "terraform/agent を先に apply" in tf)
check("stream の出力 anomaly_table_name がある", 'output "anomaly_table_name"' in stream_out and "outputs.anomaly_table_name" in tf)
check("lab の出力 lab_instance_id がある", 'output "lab_instance_id"' in lab_out and "outputs.lab_instance_id" in tf)
check("ecr の出力 worker_repository_url / temporal_repository_url がある",
      all(f'output "{o}"' in ecr_out and f"outputs.{o}" in tf for o in ("worker_repository_url", "temporal_repository_url")))
check("ECS のタスクは Fargate の ARM64", 'cpu_architecture        = "ARM64"' in tf and '"FARGATE"' in tf)
check("temporal コンテナは start-dev を SQLite で、0.0.0.0 で待つ", '"server", "start-dev", "--ip", "0.0.0.0"' in tf and "--db-filename" in tf)
check("worker は temporal の後に起き、localhost:7233 につなぐ", '"localhost:7233"' in tf and 'condition = "START"' in tf)
for env in ("ANOMALY_TABLE", "ANOMALY_QUEUE_URL", "PROPOSAL_TABLE", "AGENT_RUNTIME_ARN", "LAB_INSTANCE_ID", "POLL_INTERVAL", "APPROVAL_TIMEOUT_MINUTES", "VERIFY_ATTEMPTS", "PARAM_PREFIX"):
    check(f"worker の環境変数 {env} を渡す", f'name = "{env}"' in tf or f'name  = "{env}"' in tf or re.search(rf'name\s*=\s*"{env}"', tf) is not None)
check("タスクロールは Runtime の InvokeAgentRuntime と lab への ssm:SendCommand（AWS-RunShellScript だけ）",
      '"bedrock-agentcore:InvokeAgentRuntime"' in tf and '"ssm:SendCommand"' in tf and "document/AWS-RunShellScript" in tf)
check("修復案テーブルは status-updated_at-index を持ち、SSM の proposal-table に名前を書く",
      'name            = "status-updated_at-index"' in tf and '"${local.param_prefix}/proposal-table"' in tf)
check("Runtime と Web のロールに修復案と Gateway の権限を足す", 'for_each = local.reader_role_names' in tf and '"bedrock-agentcore:InvokeGateway"' in tf)
# 承認・却下を書けるのは web だけ（チャットは読むだけ。HITL の線をコードだけでなく IAM でも引く。2026-09-18）
reader_doc = re.search(r'data "aws_iam_policy_document" "reader_access" \{[\s\S]*?\n\}\n', tf)
check("UpdateItem は web のロールにだけ付き、reader_access（Runtime も入る）には入れない",
      reader_doc is not None and '"dynamodb:UpdateItem"' not in reader_doc.group(0)
      and re.search(r'data "aws_iam_policy_document" "decide_access"[\s\S]*?"dynamodb:UpdateItem"', tf) is not None
      and re.search(r'resource "aws_iam_role_policy" "decide_access"[\s\S]*?role   = local\.web_role_name', tf) is not None)
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
check("tools Lambda は VPC の中（Neptune / OpenSearch / Prometheus に届く）で、OPENSEARCH_ENDPOINT / PROMETHEUS_QUERY_URL / ANOMALY_TABLE / PROPOSAL_TABLE を渡す",
      re.search(r'resource "aws_lambda_function" "tools"[\s\S]*?vpc_config \{', tf) is not None
      and all(v in tf for v in ("OPENSEARCH_ENDPOINT", "OPENSEARCH_INDEX", "PROMETHEUS_QUERY_URL", "ANOMALY_TABLE", "PROPOSAL_TABLE")))
# 修復案は読むだけ（UpdateItem は web ロールだけ。承認は画面の承認タブで人が決める）
proposals_read = re.search(r'sid\s*=\s*"ProposalsRead"[\s\S]*?\n  \}', tf)  # ステートメント 1 つぶん（terraform fmt の桁揃えに依存しないよう粗く取る）
check("tools Lambda のロールの修復案は Query / GetItem / Scan だけ（UpdateItem は付けない）",
      proposals_read is not None and "UpdateItem" not in proposals_read.group(0)
      and all(a in proposals_read.group(0) for a in ("dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan")))
check("tools Lambda のロールに aoss:APIAccessAll と aps:QueryMetrics、コレクションの data access policy",
      '"aoss:APIAccessAll"' in tf and '"aps:QueryMetrics"' in tf and 'resource "aws_opensearchserverless_access_policy" "tools"' in tf)
check("EventBridge のルールは <接頭辞>.spark / AnomalyOpened を SQS（anomalies）へ、DLQ は 5 回で",
      re.search(r'resource "aws_cloudwatch_event_rule" "anomalies"[\s\S]*?source\s*=\s*\["\$\{local\.name_prefix\}\.spark"\][\s\S]*?"detail-type"\s*=\s*\["AnomalyOpened"\]', tf) is not None
      and 'resource "aws_sqs_queue" "anomalies"' in tf and 'resource "aws_sqs_queue" "anomalies_dlq"' in tf
      and re.search(r'redrive_policy[\s\S]*?maxReceiveCount\s*=\s*5', tf) is not None
      and 'resource "aws_cloudwatch_event_target" "anomalies"' in tf)
check("キューのポリシーは events.amazonaws.com の SendMessage をそのルールに絞る", '"sqs:SendMessage"' in tf and "events.amazonaws.com" in tf and "aws_cloudwatch_event_rule.anomalies.arn" in tf)
check("タスクロールは SQS の ReceiveMessage / DeleteMessage", '"sqs:ReceiveMessage", "sqs:DeleteMessage"' in tf)
check("sqs のエンドポイントは create_sqs_endpoint で切れる", re.search(r'resource "aws_vpc_endpoint" "sqs"\s*\{\s*count = var\.create_sqs_endpoint \? 1 : 0', tf) is not None)
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
check("workflow/requirements.txt は temporalio と boto3 を固定する", "temporalio==" in read("workflow", "requirements.txt") and "boto3>=" in read("workflow", "requirements.txt"))
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
check("Web に「承認」タブがあり、proposals.decide で approved / rejected を書く",
      'gr.Tab("承認")' in web and 'decide_proposal(i, "approved", s)' in web and 'decide_proposal(i, "rejected", s)' in web
      and "import proposals" in incident and "proposals.decide(" in incident)
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
check("up.sh は workflow ルートを足し、費用に 6 セント足す（sqs のエンドポイント込み）", 'ROOTS="$ROOTS workflow"' in up and 'COST_CENTS=$((COST_CENTS + 6))' in up)
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
check("terraform/agent のファイルは versions / providers / variables / locals / network / runtime / kb / outputs",
      agent_files == {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "network.tf", "runtime.tf", "kb.tf", "outputs.tf"})
agent_tf = "".join(read("terraform", "agent", n) for n in sorted(agent_files))
main_tf = "".join(read("terraform", "base", "core", n) for n in sorted(os.listdir(os.path.join(ROOT, "terraform", "base", "core"))) if n.endswith(".tf"))
check("Runtime / ガードレール / KB / Runtime のエンドポイントは terraform/agent にあり、terraform/base/core には無い",
      all(r in agent_tf for r in ('resource "aws_bedrockagentcore_agent_runtime" "agent"', 'resource "aws_bedrock_guardrail" "this"', 'resource "aws_bedrockagent_knowledge_base" "kb"', 'resource "aws_vpc_endpoint" "runtime"'))
      and not any(r in main_tf for r in ("aws_bedrockagentcore_agent_runtime", "aws_bedrock_guardrail", "aws_bedrockagent_knowledge_base", "aws_opensearchserverless", 'resource "aws_vpc_endpoint" "runtime"')))
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
check("down.sh は Runtime の ENI が残るあいだ VPC・サブネット・Runtime の SG を残して他を消す",
      "InterfaceType=='agentic_ai'" in down and "Name=tag:Name,Values=$PREFIX-vpc" in down and "tf base/core output -raw vpc_id" not in down and "Runtime の ENI の確認:" in down
      and '""|data.*|aws_vpc.this|aws_subnet.*|aws_security_group.runtime) ;;' in down
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

print(f"通過 {passed} / 失敗 0")
