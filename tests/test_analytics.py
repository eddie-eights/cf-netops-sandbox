"""terraform/pipeline/analytics と spark/snmp_sinks.py の模擬テスト（AWS に触れない）。
terraform/pipeline/analytics が main と stream の state を読み、S3 Tables のテーブルと EMR Serverless と格納先（sinks）を作ること、
Spark のスクリプトが Kafka（MSK の IAM 認証）を格納先ごとに読んで Iceberg / OpenSearch Serverless / Prometheus に流すこと、
テーブルの列がスクリプトと一致すること、remote write の protobuf と snappy が手で復号できることを見る。
実行は python3 tests/test_analytics.py（依存は無い。pyspark も botocore も要らない。スクリプトは import するが pyspark は関数の中で読む）。"""
import ast, importlib.util, io, json, os, re, struct, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "spark", "snmp_sinks.py")
TF_DIR = os.path.join(ROOT, "terraform", "pipeline", "analytics")
UP = os.path.join(ROOT, "ops", "up.sh")
DOWN = os.path.join(ROOT, "ops", "down.sh")
CHECK = os.path.join(ROOT, "ops", "check.sh")
ENV_EXAMPLE = os.path.join(ROOT, "deploy.env.example")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

# terraform/pipeline/analytics は関心ごとにファイルが分かれているので、ルートの .tf を全部つないで見る
tf = ""
tf_files = sorted(n for n in os.listdir(TF_DIR) if n.endswith(".tf"))
for name in tf_files:
    with open(os.path.join(TF_DIR, name), encoding="utf-8") as f:
        tf += f.read() + "\n"
with open(SRC, encoding="utf-8") as f:
    src = f.read()
with open(UP, encoding="utf-8") as f:
    up = f.read()
with open(DOWN, encoding="utf-8") as f:
    down = f.read()
with open(CHECK, encoding="utf-8") as f:
    checksh = f.read()
with open(ENV_EXAMPLE, encoding="utf-8") as f:
    env_example = f.read()

# ---- 他のルートとのつながり
check("ファイルは versions / providers / variables / locals / network / tables / emr / sinks / access / outputs",
      set(tf_files) == {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "network.tf", "tables.tf", "emr.tf", "sinks.tf", "access.tf", "outputs.tf"})
check("main の state をローカルから読む", re.search(r'data "terraform_remote_state" "main"[\s\S]*?backend\s*=\s*"local"', tf, re.S) is not None
      and '"${path.module}/../../base/core/terraform.tfstate"' in tf)
check("stream の state をローカルから読む", re.search(r'data "terraform_remote_state" "stream"[\s\S]*?backend\s*=\s*"local"', tf, re.S) is not None
      and '"${path.module}/../stream/terraform.tfstate"' in tf)
for out in ("vpc_id", "runtime_subnet_ids", "endpoint_security_group_id", "kb_bucket_name"):
    check(f"main の output {out} を使う", f"data.terraform_remote_state.main.outputs.{out}" in tf)
for out in ("msk_cluster_arn", "msk_security_group_id", "bootstrap_brokers"):
    check(f"stream の output {out} を try で読む（無ければ precondition で止める）",
          re.search(r'try\(data\.terraform_remote_state\.stream\.outputs\.' + out + r',\s*""\)', tf) is not None)
check("stream が無いときは「terraform/pipeline/stream を先に apply する」と出る",
      re.search(r'precondition\s*\{[\s\S]*?msk_cluster_arn\s*!=\s*""[\s\S]*?terraform/pipeline/stream を先に apply する', tf, re.S) is not None)
# main / stream の outputs.tf に本当にその output があるか
for root, outs in (("base/core", ("vpc_id", "runtime_subnet_ids", "endpoint_security_group_id", "kb_bucket_name")),
                   ("pipeline/stream", ("msk_cluster_arn", "msk_security_group_id", "bootstrap_brokers"))):
    with open(os.path.join(ROOT, "terraform", root, "outputs.tf"), encoding="utf-8") as f:
        other = f.read()
    for out in outs:
        check(f"terraform/{root} に output {out} がある", re.search(r'^output "' + out + r'"', other, re.M) is not None)

# ---- ネットワーク（NAT が無いので S3 Tables の API はエンドポイント。EMR Serverless は inbound 0.0.0.0/0 の SG を拒否する）
check("s3tables の interface エンドポイントを 2 AZ に置く",
      re.search(r'resource "aws_vpc_endpoint" "s3tables"[\s\S]*?service_name\s*=\s*"com\.amazonaws\.\$\{var\.region\}\.s3tables"[\s\S]*?vpc_endpoint_type\s*=\s*"Interface"[\s\S]*?slice\(local\.subnet_ids, 0, 2\)', tf, re.S) is not None)
check("s3tables エンドポイントは private DNS", re.search(r'resource "aws_vpc_endpoint" "s3tables".*?private_dns_enabled\s*=\s*true', tf, re.S) is not None)
check("EMR の SG に cidr の inbound が無い（自分自身からだけ）",
      re.search(r'aws_vpc_security_group_ingress_rule" "emr_self"[\s\S]*?referenced_security_group_id\s*=\s*aws_security_group\.emr\.id', tf, re.S) is not None
      and not any("cidr_ipv" in b for b in re.findall(r'resource "aws_vpc_security_group_ingress_rule" "[a-z_]+" \{(.*?)\n\}', tf, re.S)
                  if re.search(r'^\s*security_group_id\s*=\s*aws_security_group\.emr\.id', b, re.M)))
check("MSK の SG に EMR からの 9098 を開ける",
      re.search(r'"msk_from_emr"[\s\S]*?security_group_id\s*=\s*local\.msk_sg_id[\s\S]*?from_port\s*=\s*9098[\s\S]*?referenced_security_group_id\s*=\s*aws_security_group\.emr\.id', tf, re.S) is not None)
check("main のエンドポイント SG に EMR からの 443 を開ける",
      re.search(r'"endpoints_from_emr"[\s\S]*?security_group_id\s*=\s*local\.endpoint_sg_id[\s\S]*?from_port\s*=\s*443', tf, re.S) is not None)

# ---- S3 Tables のテーブル（列はスクリプトと同じでなければ append が落ちる）
TABLE_COLUMNS = ["ts", "topic", "measurement", "agent_host", "host", "tags_json", "fields_json", "ingested_at"]
schema = re.search(r'resource "aws_s3tables_table" "snmp_metrics"(.*?)\n\}\n', tf, re.S)
check("aws_s3tables_table snmp_metrics がある", schema is not None)
fields = re.findall(r'field\s*\{\s*name\s*=\s*"([a-z_]+)"\s*type\s*=\s*"([a-z]+)"\s*required\s*=\s*(true|false)', schema.group(1))
check("テーブルの列は ts / topic / measurement / agent_host / host / tags_json / fields_json / ingested_at の順", [f[0] for f in fields] == TABLE_COLUMNS)
coltypes = dict((f[0], f[1]) for f in fields)
check("ts と ingested_at は timestamp、それ以外は string",
      coltypes["ts"] == "timestamp" and coltypes["ingested_at"] == "timestamp" and all(coltypes[c] == "string" for c in TABLE_COLUMNS if c not in ("ts", "ingested_at")))
check("必須は ts / topic / ingested_at だけ", sorted(f[0] for f in fields if f[2] == "true") == ["ingested_at", "topic", "ts"])
check("format は ICEBERG", re.search(r'format\s*=\s*"ICEBERG"', schema.group(1)) is not None)
check("namespace とテーブル名はアンダースコアだけ（ハイフン不可）",
      re.search(r'variable "namespace"[\s\S]*?regex\("\^\[a-z0-9\]\[a-z0-9_\]', tf, re.S) is not None
      and re.search(r'variable "table_name"[\s\S]*?regex\("\^\[a-z0-9\]\[a-z0-9_\]', tf, re.S) is not None)
check("既定のテーブル名は snmp_metrics", re.search(r'variable "table_name"[\s\S]*?default\s*=\s*"snmp_metrics"', tf, re.M) is not None)

# ---- EMR Serverless（器だけ。ジョブは ops/up.sh が起こす）
check("EMR Serverless は spark / ARM64", re.search(r'aws_emrserverless_application" "spark"[\s\S]*?type\s*=\s*"spark"[\s\S]*?architecture\s*=\s*"ARM64"', tf, re.S) is not None)
check("使わなければ止まる（auto_stop）", re.search(r'auto_stop_configuration\s*\{\s*enabled\s*=\s*true', tf) is not None)
check("release は emr-7.5 以上（S3 Tables の下限）", re.search(r'default\s*=\s*"emr-7\.(5|[6-9]|1[0-9])\.[0-9]+"', tf) is not None)
check("ロググループに retention がある", re.search(r'aws_cloudwatch_log_group" "emr"[\s\S]*?retention_in_days', tf, re.S) is not None)
check("runtime role は emr-serverless から assume（SourceAccount の条件付き）",
      re.search(r'"emr-serverless\.amazonaws\.com"', tf) is not None and '"aws:SourceAccount"' in tf)
for act in ("s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation", "s3tables:PutTableData", "kafka-cluster:ReadData", "kafka-cluster:Connect"):
    check(f"runtime role に {act}", f'"{act}"' in tf)
check("Kafka のトピック ARN は cluster → topic の置き換え", 'replace(local.msk_cluster_arn, ":cluster/", ":topic/")' in tf)

# ---- 格納先（var.sinks。Kafka から 4 つに分ける設計のうち Spark の 3 本。Splunk は MSK Connect で後回し）
check("variable sinks は list、既定 3 つ（iceberg / opensearch / prometheus。2026-09-17 ユーザー決定）、3 つのどれかに絞る",
      re.search(r'variable "sinks"[\s\S]*?type\s*=\s*list\(string\)[\s\S]*?default\s*=\s*\["iceberg",\s*"opensearch",\s*"prometheus"\][\s\S]*?validation', tf, re.S) is not None
      and re.search(r'variable "sinks"[\s\S]*?\["iceberg",\s*"opensearch",\s*"prometheus"\]', tf, re.S) is not None)
check("variable metric_topics / log_topics（既定 metrics / traps + logs、空を拒否）",
      re.search(r'variable "metric_topics"[\s\S]*?default\s*=\s*\["metrics"\][\s\S]*?validation', tf, re.S) is not None
      and re.search(r'variable "log_topics"[\s\S]*?default\s*=\s*\["traps",\s*"logs"\][\s\S]*?validation', tf, re.S) is not None)
check("splunk のリソースと変数は Terraform に無い（MSK Connect で後回し。注記と description だけ）",
      re.search(r'(resource|variable|output) "[^"]*splunk', tf, re.I) is None and "sink_splunk" not in tf)
check("locals に sink_iceberg / sink_opensearch / sink_prometheus",
      all(re.search(r'sink_' + s + r'\s*=\s*contains\(var\.sinks,\s*"' + s + r'"\)', tf) for s in ("iceberg", "opensearch", "prometheus")))
check("OpenSearch Serverless は TIMESERIES のコレクション <prefix>-logs（count で作る）",
      re.search(r'resource "aws_opensearchserverless_collection" "logs"[\s\S]*?count\s*=\s*local\.sink_opensearch \? 1 : 0[\s\S]*?type\s*=\s*"TIMESERIES"', tf, re.S) is not None
      and re.search(r'logs_collection\s*=\s*"\$\{local\.name_prefix\}-logs"', tf) is not None)
check("OpenSearch のコレクションは公開せず VPC エンドポイントからだけ",
      re.search(r'resource "aws_opensearchserverless_vpc_endpoint" "logs"', tf) is not None
      and re.search(r'"logs_network"[\s\S]*?AllowFromPublic\s*=\s*false[\s\S]*?SourceVPCEs\s*=\s*\[aws_opensearchserverless_vpc_endpoint\.logs\[0\]\.id\]', tf, re.S) is not None)
check("OpenSearch のデータアクセスは EMR の実行ロールだけ、snmp-logs のインデックスに WriteDocument / CreateIndex",
      re.search(r'resource "aws_opensearchserverless_access_policy" "logs"[\s\S]*?"aoss:CreateIndex"[\s\S]*?"aoss:WriteDocument"[\s\S]*?Principal\s*=\s*\[aws_iam_role\.emr\.arn\]', tf, re.S) is not None
      and re.search(r'opensearch_index\s*=\s*"snmp-logs"', tf) is not None)
check("Prometheus はワークスペース <prefix>-metrics と aps-workspaces の interface エンドポイント（2 AZ）",
      re.search(r'resource "aws_prometheus_workspace" "metrics"[\s\S]*?count\s*=\s*local\.sink_prometheus \? 1 : 0[\s\S]*?alias\s*=\s*local\.metrics_workspace', tf, re.S) is not None
      and re.search(r'metrics_workspace\s*=\s*"\$\{local\.name_prefix\}-metrics"', tf) is not None
      and re.search(r'resource "aws_vpc_endpoint" "aps"[\s\S]*?count\s*=\s*local\.sink_prometheus \? 1 : 0[\s\S]*?"com\.amazonaws\.\$\{var\.region\}\.aps-workspaces"[\s\S]*?slice\(local\.subnet_ids, 0, 2\)', tf, re.S) is not None)
check("runtime role に aoss:APIAccessAll と aps:RemoteWrite（格納先を選んだときだけ。for-if で count 0 のときの index を避ける）",
      re.search(r'"aoss:APIAccessAll"[\s\S]*?local\.sink_opensearch \? aws_opensearchserverless_collection\.logs\[0\]\.arn : ""[\s\S]*?\] : s if local\.sink_opensearch\]', tf, re.S) is not None
      and re.search(r'"aps:RemoteWrite"[\s\S]*?local\.sink_prometheus \? aws_prometheus_workspace\.metrics\[0\]\.arn : ""[\s\S]*?\] : s if local\.sink_prometheus\]', tf, re.S) is not None)
check("remote write の URL は prometheus_endpoint + api/v1/remote_write",
      re.search(r'prometheus_remote_write_url\s*=\s*local\.sink_prometheus \? "\$\{aws_prometheus_workspace\.metrics\[0\]\.prometheus_endpoint\}api/v1/remote_write" : ""', tf) is not None)

# ---- output（ops/up.sh と docs/pipeline.md の a-3 がそのまま使う）
for out in ("application_id", "runtime_role_arn", "table_identifier", "job_driver_json", "configuration_overrides_json", "list_job_runs_command", "list_tables_command",
            "sinks", "opensearch_collection_endpoint", "prometheus_workspace_id", "prometheus_remote_write_url", "prometheus_query_url",
            "anomaly_table_name", "opensearch_collection_name", "opensearch_collection_arn", "opensearch_index", "prometheus_workspace_arn", "events_endpoint_id"):
    check(f"output {out} がある", re.search(r'^output "' + out + r'"', tf, re.M) is not None)
check("job_driver は S3 Tables のカタログを spark-submit の --conf で渡す",
      "software.amazon.s3tables.iceberg.S3TablesCatalog" in tf and "org.apache.iceberg.spark.SparkCatalog" in tf
      and "IcebergSparkSessionExtensions" in tf)
args_block = re.search(r'entryPointArguments\s*=\s*concat\((.*?)\n\s*\)\n', tf, re.S)
check("job_driver の引数は concat（共通 + 格納先ごとの for-if）", args_block is not None)
for a in ("--bootstrap", "--checkpoint", "--sinks", "--region", "--metric-topics", "--log-topics", "--anomaly-table", "--device-map", "--event-bus"):
    check(f"job_driver の共通の引数に {a}", f'"{a}"' in args_block.group(1))
check("job_driver の格納先の引数は選んだときだけ（for a in [...] : a if local.sink_*）",
      re.search(r'\["--iceberg-table",\s*local\.iceberg_table\] : a if local\.sink_iceberg', args_block.group(1)) is not None
      and re.search(r'\["--opensearch-endpoint",\s*local\.opensearch_endpoint,\s*"--opensearch-index",\s*local\.opensearch_index\] : a if local\.sink_opensearch', args_block.group(1)) is not None
      and re.search(r'\["--prometheus-url",\s*local\.prometheus_remote_write_url\] : a if local\.sink_prometheus', args_block.group(1)) is not None)
check("--sinks は var.sinks をカンマでつなぐ", 'join(",", var.sinks)' in args_block.group(1))
check("--checkpoint は s3://<バケット>/analytics/checkpoint/（格納先ごとに下を切るのはスクリプト）",
      '"s3://${local.bucket}/${local.checkpoint}/"' in args_block.group(1))
check("job_driver は jars を s3://<バケット>/analytics/jars/ から読む", "spark.jars=s3://${local.bucket}/${local.jars_prefix}/*.jar" in tf
      and re.search(r'jars_prefix\s*=\s*"\$\{local\.s3_prefix\}/jars"', tf) is not None and re.search(r's3_prefix\s*=\s*"analytics"', tf) is not None)
check("ジョブは 2 vCPU（driver 1 + executor 1、動的割り当て無し）",
      "spark.driver.cores=1" in tf and "spark.executor.cores=1" in tf and "spark.executor.instances=1" in tf and "spark.dynamicAllocation.enabled=false" in tf)
check("ドライバーのログは CloudWatch、EMR の managed storage は使わない",
      re.search(r'cloudWatchLoggingConfiguration\s*=\s*\{\s*enabled\s*=\s*var\.cloudwatch_logging', tf) is not None
      and re.search(r'managedPersistenceMonitoringConfiguration\s*=\s*\{\s*enabled\s*=\s*false', tf) is not None)

# ---- Spark のスクリプト（読み書きの形は文字列で見る。pyspark は関数の中で import するので、モジュールは pyspark 無しで読める）
tree = ast.parse(src, SRC)
funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
check("parse_args / sink_topics / read_rows / build / main がある", {"parse_args", "sink_topics", "read_rows", "build", "main"} <= set(funcs))
check("検知の関数（parse_device_map / device / events / anomaly_key / make_detect_sender）がある",
      {"parse_device_map", "device", "events", "anomaly_key", "anomaly_detail", "make_detect_sender"} <= set(funcs))
check("job_driver は --anomaly-table に stream の anomalies テーブル、--device-map と --event-bus に変数を渡す",
      re.search(r'"--anomaly-table",\s*local\.anomaly_table,\s*"--device-map",\s*var\.device_map,\s*"--event-bus",\s*var\.event_bus', tf) is not None)
check("precondition は stream の anomaly_table_name も見る", 'local.anomaly_table != ""' in tf and "anomaly_table_name が読めない" in tf)
check("runtime role は anomalies テーブルに dynamodb:UpdateItem、既定のバスに events:PutEvents",
      re.search(r'"dynamodb:UpdateItem"[\s\S]*?Resource = local\.anomaly_table_arn', tf) is not None
      and re.search(r'"events:PutEvents"[\s\S]*?Resource = local\.event_bus_arn', tf) is not None)
check("events のエンドポイント（Interface、2 AZ、private DNS）を持つ",
      re.search(r'resource "aws_vpc_endpoint" "events"[\s\S]*?"com\.amazonaws\.\$\{var\.region\}\.events"[\s\S]*?vpc_endpoint_type\s*=\s*"Interface"[\s\S]*?slice\(local\.subnet_ids, 0, 2\)[\s\S]*?private_dns_enabled\s*=\s*true', tf, re.S) is not None)
check("build の引数は spark / args（格納先ごとに Kafka を読む）", [a.arg for a in funcs["build"].args.args] == ["spark", "args"])
check("pyspark はモジュールの先頭で import しない（テストと引数の検査を pyspark 無しで動かすため）",
      not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "pyspark" in ast.dump(n) for n in tree.body))
check("既定のトピックは metrics（メトリクス）と traps / logs（ログ。logs は FRR のログ）", re.search(r'^METRIC_TOPICS\s*=\s*"metrics"', src, re.M) is not None
      and re.search(r'^LOG_TOPICS\s*=\s*"traps,logs"', src, re.M) is not None)
check("SINKS は iceberg / opensearch / prometheus（Terraform の validation と同じ）", re.search(r'^SINKS\s*=\s*\("iceberg", "opensearch", "prometheus"\)', src, re.M) is not None)
check("Kafka を readStream で読み、購読は引数（格納先ごと）", '.readStream.format("kafka")' in src and '.option("subscribe", topics)' in src)
for k, v in (("kafka.security.protocol", "SASL_SSL"), ("kafka.sasl.mechanism", "AWS_MSK_IAM"),
             ("kafka.sasl.jaas.config", "software.amazon.msk.auth.iam.IAMLoginModule required;"),
             ("kafka.sasl.client.callback.handler.class", "software.amazon.msk.auth.iam.IAMClientCallbackHandler")):
    check(f"MSK の IAM 認証: {k}", f'.option("{k}", "{v}")' in src)
check("Iceberg に append で書き、toTable で名前を渡す", '.writeStream.queryName("iceberg").format("iceberg")' in src and '.outputMode("append")' in src and ".toTable(table)" in src)
check("checkpoint は格納先ごと（iceberg/ と <name>/）", '.option("checkpointLocation", checkpoint + "iceberg/")' in src
      and '.option("checkpointLocation", checkpoint + name + "/")' in src)
check("60 秒ごとのマイクロバッチ", re.search(r'^TRIGGER\s*=\s*"60 seconds"', src, re.M) is not None and src.count("processingTime=TRIGGER") == 2)
check("HTTP の格納先は foreachBatch で driver から送る", ".foreachBatch(each_batch)" in src and "batch_df.collect()" in src)
check("SigV4 は botocore（EMR の実行ロールの認証情報）", "from botocore.auth import SigV4Auth" in src and "from botocore.awsrequest import AWSRequest" in src and 'h["x-amz-content-sha256"] = hashlib.sha256(body).hexdigest()' in src)
check("OpenSearch は _bulk に aoss の SigV4、Prometheus は remote write に aps の SigV4",
      re.search(r'sigv4_headers\("POST", url, body, "aoss", region', src) is not None
      and re.search(r'sigv4_headers\("POST", url, body, "aps", region', src) is not None
      and '"Content-Encoding": "snappy"' in src and '"X-Prometheus-Remote-Write-Version": "0.1.0"' in src)
# select の各行は「….alias("列")」か、そのままの列名「F.col("topic")」
block = src.split("rows = parsed.select(")[1].split(").where(")[0]
aliases = [a or b for a, b in re.findall(r'(?:\.alias\("([a-z_]+)"\)|^\s*F\.col\("([a-z_]+)"\)),\s*$', block, re.M)]
check("スクリプトの列は tables.tf の列と同じ順", aliases == TABLE_COLUMNS)
check("timestamp が無い行は捨てる", '.where(F.col("ts").isNotNull())' in src)
check("tags / fields は JSON 文字列のまま", 'F.to_json(F.col("m.tags")).alias("tags_json")' in src and 'F.to_json(F.col("m.fields")).alias("fields_json")' in src)

# ---- 純粋な関数を本当に動かす（引数の検査、トピックの振り分け、名前の規則、protobuf と snappy の手組み）
spec = importlib.util.spec_from_file_location("snmp_sinks", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def parse_error(argv):
    """argparse の p.error は SystemExit(2)。使い方の表示は捨てる"""
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        mod.parse_args(argv)
    except SystemExit as e:
        return e.code
    finally:
        sys.stderr = saved
    return None

base = ["--bootstrap", "b:9098", "--checkpoint", "s3://bucket/analytics/checkpoint"]
a = mod.parse_args(base + ["--sinks", "iceberg", "--iceberg-table", "s3tablesbucket.ns.t"])
check("parse_args: 既定は metrics / traps,logs、checkpoint に / を足す、sinks はリスト",
      a.metric_topics == "metrics" and a.log_topics == "traps,logs" and a.checkpoint == "s3://bucket/analytics/checkpoint/" and a.sinks == ["iceberg"])
a = mod.parse_args(base + ["--sinks", "iceberg, prometheus ,opensearch", "--iceberg-table", "t", "--prometheus-url", "https://p/api/v1/remote_write",
                           "--opensearch-endpoint", "https://o", "--metric-topics", " metrics , cpu ", "--log-topics", "traps,logs"])
check("parse_args: 空白を除いて 3 つ、トピックも空白を除く", a.sinks == ["iceberg", "prometheus", "opensearch"] and a.metric_topics == "metrics,cpu" and a.log_topics == "traps,logs"
      and a.opensearch_index == "snmp-logs")
check("parse_args: iceberg なのに --iceberg-table が無ければ 2 で止まる", parse_error(base + ["--sinks", "iceberg"]) == 2)
check("parse_args: opensearch / prometheus も同じ", parse_error(base + ["--sinks", "opensearch"]) == 2 and parse_error(base + ["--sinks", "prometheus"]) == 2)
check("parse_args: 知らない格納先と空の --sinks は 2", parse_error(base + ["--sinks", "splunk"]) == 2 and parse_error(base + ["--sinks", " , "]) == 2)
check("parse_args: 空の --metric-topics は 2", parse_error(base + ["--sinks", "iceberg", "--iceberg-table", "t", "--metric-topics", ","]) == 2)

check("sink_topics: iceberg は全部（重複無し）、prometheus はメトリクス、opensearch はログ",
      mod.sink_topics("iceberg", "metrics,cpu", "traps,logs") == "metrics,cpu,traps,logs"
      and mod.sink_topics("iceberg", "a,b", "b") == "a,b"
      and mod.sink_topics("prometheus", "metrics", "traps") == "metrics"
      and mod.sink_topics("opensearch", "metrics", "traps") == "traps")
try:
    mod.sink_topics("splunk", "m", "l")
    bad_sink = False
except ValueError:
    bad_sink = True
check("sink_topics: 知らない格納先は ValueError", bad_sink)

check("metric_name: snmp_<measurement>_<field>、使えない字は _、先頭の数字は _ を足す",
      mod.metric_name("interface", "ifInOctets") == "snmp_interface_ifInOctets"
      and mod.metric_name("if-x", "in.octets") == "snmp_if_x_in_octets"
      and mod.metric_name("1x", "y") == "snmp_1x_y" and mod.metric_name("", "") == "snmp__")
check("label_name: 使えない字は _、空と先頭の数字は _ を足す、__ 始まりは _ 1 つに",
      mod.label_name("agent_host") == "agent_host" and mod.label_name("if-name") == "if_name"
      and mod.label_name("") == "_" and mod.label_name("1a") == "_1a" and mod.label_name("__meta") == "_meta")

rec = {"ts": 1700000000.5, "topic": "metrics", "measurement": "interface", "agent_host": "r1", "host": "h",
       "tags": {"agent_host": "r1", "ifName": "Gi0/1", "empty": "", "none": None}, "fields": {"ifInOctets": "123", "ifOperStatus": 1, "descr": "up", "flag": True}}
series = mod.prometheus_series([rec])
check("prometheus_series: 数値の field だけ（文字列は落とす、bool は 1/0）、ms は ts × 1000、空のタグは落とす",
      len(series) == 3 and all(ms == 1700000000500 for _, _, ms in series)
      and sorted(v for _, v, _ in series) == [1.0, 1.0, 123.0]
      and all(dict(l)["agent_host"] == "r1" and "empty" not in dict(l) and "none" not in dict(l) for l, _, _ in series)
      and sorted(dict(l)["__name__"] for l, _, _ in series) == ["snmp_interface_flag", "snmp_interface_ifInOctets", "snmp_interface_ifOperStatus"])
check("prometheus_series: トピックでは絞らない（購読で絞っている）", len(mod.prometheus_series([dict(rec, topic="cpu")])) == 3)
check("prometheus_series: labels は名前順のリスト（Prometheus はソート済みを要求する）", all(l == sorted(l) for l, _, _ in series))

def read_varint(b, i):
    n = shift = 0
    while True:
        c = b[i]; i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7

def decode_fields(b):
    """protobuf の (field, wire, value) を順に返す。wire 0 = varint、1 = 8 バイト、2 = 長さ付き"""
    i, out = 0, []
    while i < len(b):
        key, i = read_varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = read_varint(b, i)
        elif wire == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]; i += 8
        elif wire == 2:
            n, i = read_varint(b, i)
            v = b[i:i + n]; i += n
        else:
            raise AssertionError(wire)
        out.append((num, wire, v))
    return out

wr = mod.encode_write_request([([("__name__", "snmp_x_y"), ("host", "h")], 1.5, 1700000000500)])
top = decode_fields(wr)
check("encode_write_request: WriteRequest.timeseries(1) が 1 本", [(n, w) for n, w, _ in top] == [(1, 2)])
ts_fields = decode_fields(top[0][2])
labels = [decode_fields(v) for n, w, v in ts_fields if n == 1]
samples = [decode_fields(v) for n, w, v in ts_fields if n == 2]
check("encode_write_request: labels(1) は name(1) / value(2) の文字列、samples(2) は value(1) double / timestamp(2) varint",
      [(l[0][2], l[1][2]) for l in labels] == [(b"__name__", b"snmp_x_y"), (b"host", b"h")]
      and len(samples) == 1 and samples[0] == [(1, 1, 1.5), (2, 0, 1700000000500)])
check("_varint: 0 / 127 / 128 / 300", mod._varint(0) == b"\x00" and mod._varint(127) == b"\x7f" and mod._varint(128) == b"\x80\x01" and mod._varint(300) == b"\xac\x02")
check("_field_varint: 負の int64 は 2^64 を足して 10 バイト", len(mod._field_varint(2, -1)) == 1 + 10)

def snappy_decompress(b):
    n, i = read_varint(b, 0)
    out = bytearray()
    while i < len(b):
        tag = b[i]; i += 1
        assert tag & 3 == 0, "リテラル以外は出さない"
        ln = tag >> 2
        if ln < 60:
            ln += 1
        elif ln == 60:
            ln = b[i] + 1; i += 1
        elif ln == 61:
            ln = struct.unpack("<H", b[i:i + 2])[0] + 1; i += 2
        else:
            raise AssertionError(ln)
        out += b[i:i + ln]; i += ln
    assert len(out) == n
    return bytes(out)

small = b"x" * 10
big = bytes(range(256)) * 300  # 76800 > chunk 65536 → 2 要素
check("snappy_compress: 短いリテラルは 1 バイトのタグ、前置きは非圧縮長", mod.snappy_compress(small) == b"\x0a" + bytes([9 << 2]) + small and snappy_decompress(mod.snappy_compress(small)) == small)
check("snappy_compress: 60 以上は 61 のタグ + 2 バイトの長さ、chunk で分ける。復号すると元に戻る",
      snappy_decompress(mod.snappy_compress(big)) == big and mod.snappy_compress(big)[3] == 61 << 2 and snappy_decompress(mod.snappy_compress(b"")) == b"")

docs = mod.opensearch_docs([rec])
check("opensearch_docs: action 行と document 行の対、@timestamp は ISO の Z、数値の field は数値、文字列はそのまま",
      docs[0] == '{"index":{}}' and len(docs) == 2
      and json.loads(docs[1])["@timestamp"] == "2023-11-14T22:13:20.500000Z"
      and json.loads(docs[1])["fields"] == {"ifInOctets": 123.0, "ifOperStatus": 1.0, "descr": "up", "flag": 1.0}
      and json.loads(docs[1])["tags"]["ifName"] == "Gi0/1")

import datetime as dt
r = mod.row_to_record({"ts": dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=dt.timezone.utc), "topic": "traps", "measurement": "snmp_trap",
                       "agent_host": "r1", "host": "h", "tags_json": '{"a":"b"}', "fields_json": "not json"})
check("row_to_record: ts は epoch 秒、tags は辞書、壊れた JSON は空の辞書",
      r["ts"] == 1700000000.0 and r["tags"] == {"a": "b"} and r["fields"] == {} and r["topic"] == "traps")
check("row_to_record: naive な datetime は UTC とみなす", mod.row_to_record({"ts": dt.datetime(2023, 11, 14, 22, 13, 20)})["ts"] == 1700000000.0)
check("_number: 数値の文字列は float、それ以外は None", mod._number("1.5") == 1.5 and mod._number("up") is None and mod._number(None) is None and mod._number(False) == 0.0)

# ---- ops/up.sh / ops/down.sh / ops/check.sh / deploy.env.example とのつながり
check("up.sh は 6 本の jar を置く", len(re.findall(r'^\s*"\$MAVEN/', up, re.M)) == 6)
for jar in ("spark-sql-kafka-0-10_2.12", "spark-token-provider-kafka-0-10_2.12", "kafka-clients", "commons-pool2", "aws-msk-iam-auth", "s3-tables-catalog-for-iceberg-runtime"):
    check(f"up.sh の jar に {jar}", jar in up)
check("up.sh の SPARK_VERSION は emr_release_label の Spark（3.5.6）", re.search(r'^SPARK_VERSION=3\.5\.6$', up, re.M) is not None
      and "7.13.0 = Spark 3.5.6" in tf)
check("up.sh のスクリプトは spark/snmp_sinks.py", re.search(r'^SPARK_SCRIPT=spark/snmp_sinks\.py$', up, re.M) is not None and "snmp_to_iceberg" not in up)
check("up.sh は SINK_S3 / SINK_OPENSEARCH / SINK_PROMETHEUS（既定 1）を terraform/pipeline/analytics の sinks に組んで渡す",
      re.search(r'^SINK_S3="\$\{SINK_S3:-1\}"; SINK_OPENSEARCH="\$\{SINK_OPENSEARCH:-1\}"; SINK_PROMETHEUS="\$\{SINK_PROMETHEUS:-1\}"$', up, re.M) is not None
      and 'ANALYTICS_VARS=(-var "sinks=[$SINKS_TF]")' in up and 'tf_apply pipeline/analytics "${ANALYTICS_VARS[@]}"' in up)
check("up.sh は AGENT=0 でも CloudWatch へのログを切らない（logs のエンドポイントは土台の共用のもの。2026-09-18）",
      "cloudwatch_logging=false" not in up and re.search(r'variable "cloudwatch_logging" \{[^}]*default\s*=\s*true', tf) is not None)
_core = "".join(open(os.path.join(ROOT, "terraform", "base", "core", n), encoding="utf-8").read() for n in sorted(os.listdir(os.path.join(ROOT, "terraform", "base", "core"))) if n.endswith(".tf"))
check("土台が ecr.api / ecr.dkr / logs のエンドポイントを持ち、create_shared_endpoints で切れる",
      re.search(r'resource "aws_vpc_endpoint" "shared"[\s\S]*?"ecr-api"\s*=\s*"ecr\.api"[\s\S]*?"ecr-dkr"\s*=\s*"ecr\.dkr"[\s\S]*?"logs"\s*=\s*"logs"', _core) is not None
      and re.search(r'variable "create_shared_endpoints" \{[^}]*default\s*=\s*true', _core) is not None)
check("up.sh は AGENT か lab か analytics を作るときだけ共用のエンドポイントを作る",
      'if [ -n "$AGENT" ] || [ -z "$SKIP_LAB" ] || [ -z "$SKIP_ANALYTICS" ]; then SHARED_ENDPOINTS=1; fi' in up
      and "MAIN_VARS+=(-var create_shared_endpoints=true)" in up and "MAIN_VARS+=(-var create_shared_endpoints=false)" in up)
check("up.sh は前の配置（agent に ecr-api）が残っていたら土台の apply の前に止まる",
      up.index('aws_vpc_endpoint.runtime["ecr-api"]') < up.index("tf_apply base/core "))
# SINK_* の判定ブロックを up.sh から切り出して、bash で実際に動かす（die と flag_value は up.sh / deploy-env.sh と同じ意味の最小版）
import subprocess
_blk = up[up.index('SINK_S3="${SINK_S3:-1}"'):up.index('SINKS_TF="\\"$(printf')]
_blk += up[up.index('SINKS_TF="\\"$(printf'):].split("\n", 1)[0] + "\n"
_pre = ('die() { echo "DIE: $*"; exit 1; }\n'
        'flag_value() { local name="$1" v; v="${!name:-}"; case "$v" in 1|true|yes) printf -v "$name" %s 1 ;; ""|0|false|no) printf -v "$name" %s "" ;; *) die "$name は 1 か 0" ;; esac; }\n')
def _sinks(**env):
    r = subprocess.run(["bash", "-c", _pre + _blk + 'echo "OUT: $SINKS | $SINKS_TF"'], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], **env})
    return r.returncode, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
check("SINK_* が無ければ 3 つ全部", _sinks() == (0, 'OUT: iceberg,opensearch,prometheus | "iceberg","opensearch","prometheus"'))
check("SINK_OPENSEARCH=0 で opensearch だけ外れる", _sinks(SINK_OPENSEARCH="0") == (0, 'OUT: iceberg,prometheus | "iceberg","prometheus"'))
check("SINK_S3=0 SINK_PROMETHEUS=no で opensearch だけ残る", _sinks(SINK_S3="0", SINK_PROMETHEUS="no") == (0, 'OUT: opensearch | "opensearch"'))
check("SINK_S3=false で S3 Tables が外れる", _sinks(SINK_S3="false") == (0, 'OUT: opensearch,prometheus | "opensearch","prometheus"'))
_rc, _out = _sinks(SINK_S3="0", SINK_OPENSEARCH="0", SINK_PROMETHEUS="0")
check("SINK_* が全部 0 なら止まる", _rc == 1 and "全部 0" in _out)
_rc, _out = _sinks(SINK_S3="2")
check("SINK_S3=2 は止まる", _rc == 1 and "SINK_S3 は 1 か 0" in _out)
check("deploy-env.sh は SINK_* を読めるキーに持つ",
      all(re.search(rf'(?<![A-Z_]){k}(?![A-Z_])', open(os.path.join(ROOT, "ops", "deploy-env.sh"), encoding="utf-8").read()) for k in ("SINK_S3", "SINK_OPENSEARCH", "SINK_PROMETHEUS")))
check("up.sh は analytics を stream の後に apply し、job を STREAMING で起こす（名前は snmp-sinks）",
      up.index("tf_apply pipeline/stream") < up.index("tf_apply pipeline/analytics") < up.index("--name snmp-sinks --mode STREAMING"))
check("up.sh は s3tables / prometheus のエンドポイントと opensearch の OCU を SINK_* ごとに費用に足し、opensearch は analytics を作るときだけ OCU の注意を出す",
      re.search(r'COST_CENTS=\$\(\(COST_CENTS \+ 17\)\)\n\s*if \[ -n "\$SINK_S3" \]; then COST_CENTS=\$\(\(COST_CENTS \+ 3\)\); fi\n\s*if \[ -n "\$SINK_PROMETHEUS" \]; then COST_CENTS=\$\(\(COST_CENTS \+ 3\)\); fi\n\s*if \[ -n "\$SINK_OPENSEARCH" \]; then COST_CENTS=\$\(\(COST_CENTS \+ 33\)\); fi', up) is not None
      and re.search(r'\*,opensearch,\*\) if \[ -z "\$SKIP_ANALYTICS" \]; then printf', up) is not None)
check("up.sh は動いているジョブがあれば起こさない", "--states SUBMITTED PENDING SCHEDULED RUNNING" in up)
check("up.sh は PIPELINE=1 で SKIP_STREAM=1 なら analytics も飛ばす", re.search(r'SKIP_STREAM=1 なので analytics も作らない[^\n]*\n\s*SKIP_ANALYTICS=1', up) is not None)
check("down.sh は job を cancel → stop-application → destroy analytics → destroy graph の順",
      down.index("cancel-job-run") < down.index("stop-application") < down.index("destroy_root pipeline/analytics") < down.index("destroy_lambda_root pipeline/graph") < down.index("destroy_root pipeline/stream"))
# .py は名指しで並べず find で全部見る（名指しだとファイルを足したときに構文検査から漏れる）
check("check.sh は spark/ の .py を構文検査に入れ、spark/snmp_sinks.py がある",
      re.search(r"find [\w /]*\bspark\b [^\n]*-name '\*\.py'", checksh) is not None
      and os.path.isfile(os.path.join(ROOT, "spark", "snmp_sinks.py")) and "snmp_to_iceberg" not in checksh)
check("up.sh の WORKFLOW=1 は SKIP_ANALYTICS があれば止まる（Spark の検知が無いとワーカーが起きない）",
      re.search(r'if \[ -n "\$WORKFLOW" \]; then\n[\s\S]*?-n "\$SKIP_ANALYTICS"[\s\S]*?die "WORKFLOW は lab と stream と analytics が要る', up) is not None)
check("up.sh は PIPELINE=0 なら lab / stream / analytics / graph を全部飛ばす",
      re.search(r'else\n\s*SKIP_LAB=1; SKIP_STREAM=1; SKIP_ANALYTICS=1; SKIP_GRAPH=1\n', up) is not None)
check("deploy.env.example に SINK_S3 / SINK_OPENSEARCH / SINK_PROMETHEUS の行がある（既定 1）。カンマ区切りの SINKS は使わない",
      all(re.search(rf'^#{k}=1$', env_example, re.M) is not None for k in ("SINK_S3", "SINK_OPENSEARCH", "SINK_PROMETHEUS"))
      and re.search(r'^#?\s*SINKS=', env_example, re.M) is None)
# iceberg を外したら S3 Tables も s3tables のエンドポイントも作らない
for _res in ('resource "aws_s3tables_table_bucket" "tables"', 'resource "aws_s3tables_namespace" "netops"', 'resource "aws_s3tables_table" "snmp_metrics"', 'resource "aws_vpc_endpoint" "s3tables"'):
    check(f"{_res} は sink_iceberg の count", re.search(re.escape(_res) + r' \{\n  count = local\.sink_iceberg \? 1 : 0\n', tf) is not None)
check("実行ロールの S3TablesCatalog と Spark のカタログの設定は iceberg があるときだけ",
      re.search(r'Sid\s*=\s*"S3TablesCatalog"[\s\S]*?\}\] : s if local\.sink_iceberg\]', tf) is not None
      and re.search(r'warehouse=\$\{local\.table_bucket_arn\}",\n\s*\] : c if local\.sink_iceberg\]', tf) is not None
      and "aws_s3tables_table_bucket.tables.arn" not in tf)

# outputs の JSON が本当に JSON になる形か（jsonencode の中身の構造を軽く見る）
check("job_driver_json は sparkSubmit の 3 キー", all(k in tf for k in ("entryPoint ", "entryPointArguments", "sparkSubmitParameters")))
print(f"通過 {passed} / 失敗 0")
