"""フェーズ 3（terraform/workflow、workflow/worker.py、agent/proposals.py、agent/mcp_client.py、tools/）の模擬テスト。
AWS にも Temporal にも触れない。temporalio と boto3 を差し替えて worker.py を読み、純粋な関数（プロンプト・JSON の読み取り・
許可リスト・二重起動の判定）と、proposals.decide の条件、mcp_client の応答の読み取り、tools.json と Python の TOOL_SPECS の一致、
Terraform と ops スクリプトのつながりを見る。実行は python3 tests/test_workflow.py（依存は無い）。"""
import ast, json, os, re, sys, types

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
boto3.client = lambda name, region_name=None: FakeClient(name)
boto3.Session = lambda region_name=None: types.SimpleNamespace(get_credentials=lambda: None)
botocore = types.ModuleType("botocore"); botocore_exc = types.ModuleType("botocore.exceptions")
botocore_exc.ClientError = ClientError; botocore_exc.BotoCoreError = BotoCoreError
botocore_auth = types.ModuleType("botocore.auth"); botocore_auth.SigV4Auth = object
botocore_req = types.ModuleType("botocore.awsrequest"); botocore_req.AWSRequest = object
sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.exceptions": botocore_exc,
                    "botocore.auth": botocore_auth, "botocore.awsrequest": botocore_req})

# ---- 差し替え: temporalio（デコレータは素通し）
def _passthrough(*a, **k):
    if len(a) == 1 and callable(a[0]) and not k:
        return a[0]
    return lambda f: f
t_activity = types.ModuleType("temporalio.activity"); t_activity.defn = _passthrough
t_workflow = types.ModuleType("temporalio.workflow")
t_workflow.defn = _passthrough; t_workflow.run = _passthrough; t_workflow.signal = _passthrough
t_workflow.execute_activity = None; t_workflow.wait_condition = None; t_workflow.now = None; t_workflow.info = None
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
import worker  # noqa: E402
import proposals  # noqa: E402
import mcp_client  # noqa: E402
import anomalies  # noqa: E402
import topology  # noqa: E402
import evidence  # noqa: E402
import handler  # noqa: E402

# ---- worker.py の純粋な関数
anomaly = {"anomaly_id": "hq-ce-01#link_down#eth1", "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1",
           "first_seen": 1700000000, "detail": "ifOperStatus down", "first_seen_jst": "2023-11-15 07:13:20"}
prompt = worker.build_prompt(anomaly)
check("プロンプトに機器・種別・対象が入る", all(s in prompt for s in ("hq-ce-01", "link_down", "eth1")))
check("プロンプトは JSON 1 個を求め、action の 3 択を示す", '"action"' in prompt and "heal-main | check | none" in prompt)
check("応答の中の JSON を拾う（前後に文があっても）",
      worker.parse_agent_json('確認しました。\n{"cause": "eth1 が down", "action": "heal-main", "reason": "主回線"}\n以上')
      == {"cause": "eth1 が down", "action": "heal-main", "reason": "主回線"})
check("JSON が無ければ action=none で本文を理由に残す", worker.parse_agent_json("わかりません")["action"] == "none"
      and worker.parse_agent_json("わかりません")["reason"] == "わかりません")
check("壊れた JSON でも落ちない", worker.parse_agent_json("{bad json")["action"] == "none")
check("cause は 1000 字で切る", len(worker.parse_agent_json(json.dumps({"cause": "x" * 5000}))["cause"]) == 1000)
check("heal-main は sudo lab heal-main", worker.normalize_action("heal-main") == ("heal-main", "sudo lab heal-main"))
check("check は sudo lab check", worker.normalize_action("check") == ("check", "sudo lab check"))
check("許可リストに無い処置は none でコマンド空（rm -rf / も fail-main も）",
      worker.normalize_action("rm -rf /") == ("none", "") and worker.normalize_action("fail-main") == ("none", "")
      and worker.normalize_action("") == ("none", ""))
check("ALLOWED_ACTIONS は lab/lab.sh のサブコマンド", all(f"  {a})" in read("lab", "lab.sh") for a in worker.ALLOWED_ACTIONS))
check("修復案が無ければ起こす", worker.should_start(anomaly, None) and worker.should_start(anomaly, {}))
check("同じ first_seen の修復案があれば起こさない", not worker.should_start(anomaly, {"first_seen": 1700000000, "status": "verified"}))
check("first_seen が違えば（別の発生）起こす", worker.should_start(anomaly, {"first_seen": 1600000000}))
check("anomaly_id が無ければ起こさない", not worker.should_start({}, None))
check("ワークフロー id は investigate-<anomaly_id>", worker.workflow_id("a#b#c") == "investigate-a#b#c")
check("DynamoDB の型付けは N / S / BOOL で、空文字は - にする",
      worker._typed(1) == {"N": "1"} and worker._typed(True) == {"BOOL": True} and worker._typed("x") == {"S": "x"} and worker._typed("") == {"S": "-"})

# ---- worker.py の AWS 呼び出し（差し替えで記録）
calls.clear()
fake["query"] = {"Items": [{"anomaly_id": {"S": "a#b#c"}, "status": {"S": "open"}, "first_seen": {"N": "1"}}]}
rows = worker.list_open_anomalies()
check("open の異常を status-last_seen-index で新しい順に読む", rows == [{"anomaly_id": "a#b#c", "status": "open", "first_seen": 1}]
      and calls[-1][2]["IndexName"] == "status-last_seen-index" and calls[-1][2]["ScanIndexForward"] is False and calls[-1][2]["TableName"] == "anom")
calls.clear()
worker.update_proposal("p1", {"status": "applied", "apply_output": "ok"})
kw = calls[-1][2]
check("update_proposal は status / apply_output / updated_at を SET する",
      calls[-1][:2] == ("dynamodb", "update_item") and kw["TableName"] == "prop" and kw["UpdateExpression"].startswith("SET ")
      and set(kw["ExpressionAttributeNames"].values()) == {"status", "apply_output", "updated_at"})
calls.clear()
worker.write_proposal({"proposal_id": "p1", "status": "pending", "first_seen": 1, "nothing": None})
check("write_proposal は None を落として put_item する", calls[-1][1] == "put_item" and "nothing" not in calls[-1][2]["Item"]
      and calls[-1][2]["Item"]["first_seen"] == {"N": "1"})

class FakeBody:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
calls.clear()
fake["invoke_agent_runtime"] = {"response": FakeBody({"status": "success", "response": '{"cause":"c","action":"check","reason":"r"}'})}
text = worker.ask_agent("q")
kw = calls[-1][2]
check("Runtime を InvokeAgentRuntime（qualifier DEFAULT、JSON の prompt、33 字以上の runtimeSessionId）で呼ぶ",
      calls[-1][:2] == ("bedrock-agentcore", "invoke_agent_runtime") and kw["qualifier"] == "DEFAULT"
      and json.loads(kw["payload"]) == {"prompt": "q"} and len(kw["runtimeSessionId"]) >= 33 and "action" in text)
fake["invoke_agent_runtime"] = {"response": FakeBody({"status": "error", "message": "x"})}
try:
    worker.ask_agent("q"); bad = False
except RuntimeError:
    bad = True
check("Runtime が error を返したら例外（Temporal が再試行する）", bad)

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
proposals._cache["table"] = ""
check("テーブルが無ければ案内だけ返す", "error" in proposals.list_proposals() and "error" in proposals.decide("p1", "approved"))
os.environ["PROPOSAL_TABLE"] = "prop"

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
py_specs = {s["toolSpec"]["name"]: s["toolSpec"] for s in topology.TOOL_SPECS + anomalies.TOOL_SPECS + evidence.TOOL_SPECS}
check("tools.json の 8 つは topology / anomalies / evidence の TOOL_SPECS と同じ名前", {t["name"] for t in tools} == set(py_specs) and len(tools) == 8)
check("evidence のツールは search_logs / query_metrics / query_history", {s["toolSpec"]["name"] for s in evidence.TOOL_SPECS} == {"search_logs", "query_metrics", "query_history"})
check("handler は evidence のツールも呼ぶ", "evidence.run_tool" in read("tools", "handler.py"))
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
      '"${path.module}/../graph/terraform.tfstate"' in tf and '"${path.module}/../analytics/terraform.tfstate"' in tf
      and re.search(r'try\(data\.terraform_remote_state\.analytics', tf) is not None)
for root in ("main", "stream", "lab", "ecr"):
    check(f"{root} の state をローカルから読む", f'"${{path.module}}/../{root}/terraform.tfstate"' in tf)
main_out = read("terraform", "main", "outputs.tf"); stream_out = read("terraform", "stream", "outputs.tf")
lab_out = read("terraform", "lab", "outputs.tf"); ecr_out = read("terraform", "ecr", "outputs.tf")
for out in ("vpc_id", "instance_subnet_id", "endpoint_security_group_id", "agent_runtime_arn", "runtime_role_name", "web_role_name"):
    check(f"main の出力 {out} がある", f'output "{out}"' in main_out and f"outputs.{out}" in tf)
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
check("Gateway は AWS_IAM 認可の MCP で、2025-06-18 を話す", 'authorizer_type = "AWS_IAM"' in tf and 'protocol_type   = "MCP"' in tf and '"2025-06-18"' in tf)
check("Gateway のターゲットは tools.json から inline schema を作る", 'jsondecode(file("${path.module}/../../tools/tools.json"))' in tf and 'dynamic "inline_payload"' in tf)
check("tools Lambda は python3.13 arm64 で、handler.py / topology / anomalies / graph / data を zip にする",
      'runtime          = "python3.13"' in tf and 'architectures    = ["arm64"]' in tf
      and all(f"../../{p}" in tf for p in ("tools/handler.py", "agent/topology.py", "agent/anomalies.py", "agent/evidence.py", "agent/graph.py", "agent/data/topology.json", "agent/data/devices.yaml")))
check("tools Lambda は VPC の中（Neptune / OpenSearch / Prometheus に届く）で、OPENSEARCH_ENDPOINT / PROMETHEUS_QUERY_URL / ANOMALY_TABLE を渡す",
      re.search(r'resource "aws_lambda_function" "tools"[\s\S]*?vpc_config \{', tf) is not None
      and all(v in tf for v in ("OPENSEARCH_ENDPOINT", "OPENSEARCH_INDEX", "PROMETHEUS_QUERY_URL", "ANOMALY_TABLE")))
check("tools Lambda のロールに aoss:APIAccessAll と aps:QueryMetrics、コレクションの data access policy",
      '"aoss:APIAccessAll"' in tf and '"aps:QueryMetrics"' in tf and 'resource "aws_opensearchserverless_access_policy" "tools"' in tf)
check("EventBridge のルールは netops.spark / AnomalyOpened を SQS（anomalies）へ、DLQ は 5 回で",
      re.search(r'resource "aws_cloudwatch_event_rule" "anomalies"[\s\S]*?source\s*=\s*\["netops\.spark"\][\s\S]*?"detail-type"\s*=\s*\["AnomalyOpened"\]', tf) is not None
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
check("agent/Dockerfile は mcp_client.py と proposals.py を入れる", "mcp_client.py proposals.py" in read("agent", "Dockerfile"))
check("workflow/Dockerfile は非 root で worker.py を打つ", "USER worker" in read("workflow", "Dockerfile") and '["python", "worker.py"]' in read("workflow", "Dockerfile"))
check("workflow/requirements.txt は temporalio と boto3 を固定する", "temporalio==" in read("workflow", "requirements.txt") and "boto3>=" in read("workflow", "requirements.txt"))
ast.parse(read("workflow", "worker.py"))
ecr_tf = read("terraform", "ecr", "main.tf")
check("terraform/ecr は worker / temporal のリポジトリを作る", '"worker", "temporal"' in ecr_tf and 'resource "aws_ecr_repository" "workflow"' in ecr_tf)

# ---- web の承認タブ
web = read("web", "app.py")
check("Web に「承認」タブがあり、proposals.decide で approved / rejected を書く",
      'gr.Tab("承認")' in web and 'decide_proposal(i, "approved", s)' in web and 'decide_proposal(i, "rejected", s)' in web and "import proposals" in web)
check("main の upload_web_command は proposals.py も上げる", "for f in topology anomalies graph proposals" in main_out)

# ---- ops
up = read("ops", "up.sh"); down = read("ops", "down.sh"); chk = read("ops", "check.sh")
check("up.sh の PHASE=3 は WORKFLOW=1 で、SKIP_LAB / SKIP_STREAM / SKIP_ANALYTICS があれば止まる",
      re.search(r'\n  3\)\n[\s\S]*?SKIP_LAB[\s\S]*?SKIP_STREAM[\s\S]*?SKIP_ANALYTICS[\s\S]*?WORKFLOW=1 ;;', up) is not None and 'PHASE は 1 か 2 か 3' in up)
# ---- starter: SQS のメッセージから anomaly_id
check("anomaly_id_from_message は detail が dict でも JSON 文字列でも読む",
      worker.anomaly_id_from_message(json.dumps({"detail": {"anomaly_id": "r1#link_down#eth1"}})) == "r1#link_down#eth1"
      and worker.anomaly_id_from_message(json.dumps({"detail": json.dumps({"anomaly_id": "r1#link_down#eth1"})})) == "r1#link_down#eth1")
check("anomaly_id_from_message はごみを空にする",
      worker.anomaly_id_from_message("garbage") == "" and worker.anomaly_id_from_message("[1]") == ""
      and worker.anomaly_id_from_message(json.dumps({"detail": "x"})) == "" and worker.anomaly_id_from_message(json.dumps({"detail": {}})) == "")
check("starter は ANOMALY_QUEUE_URL があれば SQS（20 秒の long polling）、無ければテーブルを見る",
      all(hasattr(worker, f) for f in ("start_for", "starter_queue", "starter_table", "receive_messages", "delete_message"))
      and "WaitTimeSeconds=20" in read("workflow", "worker.py") and "starter_queue if ANOMALY_QUEUE_URL else starter_table" in read("workflow", "worker.py"))
check("up.sh は workflow ルートを足し、費用に 6 セント足す（sqs のエンドポイント込み）", 'ROOTS="$ROOTS workflow"' in up and 'COST_CENTS=$((COST_CENTS + 6))' in up)
check("up.sh は worker を buildx でビルドし、temporalio/temporal を ECR にミラーする",
      '--push workflow/' in up and 'docker pull --platform linux/arm64 "temporalio/temporal:$TEMPORAL_TAG"' in up and "$PREFIX-temporal:$TEMPORAL_TAG" in up)
check("up.sh の TEMPORAL_TAG は terraform/workflow の temporal_image_tag の既定値と同じ",
      re.search(r'^TEMPORAL_TAG=(\S+)', up, re.M).group(1) == re.search(r'variable "temporal_image_tag"[\s\S]*?default\s*=\s*"([^"]+)"', tf).group(1))
check("up.sh は workflow を apply して services-stable を待ち、Temporal UI のポートフォワーディングを案内する",
      'tf_apply workflow -var "worker_image_tag=$IMAGE_TAG"' in up and 'aws ecs wait services-stable' in up and 'AWS-StartPortForwardingSessionToRemoteHost' in up)
check("down.sh は workflow を最初に消す（必須変数はダミーで渡す）",
      down.index('destroy_root workflow') < down.index('destroy_root analytics') and 'worker_image_tag=${IMAGE_TAG:-destroy}' in down)
check("check.sh は workflow ルートとこのテストを見る", "workflow)" in chk and "tests/test_workflow.py" in chk and "workflow/worker.py" in chk)
check("deploy.env.example の PHASE の説明にフェーズ 3 がある", re.search(r"^#\s+3\s", read("deploy.env.example"), re.M) is not None and "workflow" in read("deploy.env.example"))

print(f"通過 {passed} / 失敗 0")
