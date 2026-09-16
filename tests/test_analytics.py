"""terraform/analytics と spark/snmp_to_iceberg.py の模擬テスト（AWS に触れない）。
terraform/analytics が main と stream の state を読み、S3 Tables のテーブルと EMR Serverless を作ること、
Spark のスクリプトが Kafka（MSK の IAM 認証）を読んで Iceberg に追記すること、テーブルの列がスクリプトと一致することを見る。
実行は python3 tests/test_analytics.py（依存は無い。pyspark も要らない。スクリプトは ast で読むだけ）。"""
import ast, io, os, re, types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "spark", "snmp_to_iceberg.py")
TF_DIR = os.path.join(ROOT, "terraform", "analytics")
UP = os.path.join(ROOT, "ops", "up.sh")
DOWN = os.path.join(ROOT, "ops", "down.sh")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

# terraform/analytics は関心ごとにファイルが分かれているので、ルートの .tf を全部つないで見る
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

# ---- 他のルートとのつながり
check("ファイルは versions / providers / variables / locals / network / tables / emr / access / outputs",
      set(tf_files) == {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "network.tf", "tables.tf", "emr.tf", "access.tf", "outputs.tf"})
check("main の state をローカルから読む", re.search(r'data "terraform_remote_state" "main"[\s\S]*?backend\s*=\s*"local"', tf, re.S) is not None
      and '"${path.module}/../main/terraform.tfstate"' in tf)
check("stream の state をローカルから読む", re.search(r'data "terraform_remote_state" "stream"[\s\S]*?backend\s*=\s*"local"', tf, re.S) is not None
      and '"${path.module}/../stream/terraform.tfstate"' in tf)
for out in ("vpc_id", "runtime_subnet_ids", "endpoint_security_group_id", "kb_bucket_name"):
    check(f"main の output {out} を使う", f"data.terraform_remote_state.main.outputs.{out}" in tf)
for out in ("msk_cluster_arn", "msk_security_group_id", "bootstrap_brokers"):
    check(f"stream の output {out} を try で読む（無ければ precondition で止める）",
          re.search(r'try\(data\.terraform_remote_state\.stream\.outputs\.' + out + r',\s*""\)', tf) is not None)
check("stream が無いときは「terraform/stream を先に apply する」と出る",
      re.search(r'precondition\s*\{[\s\S]*?msk_cluster_arn\s*!=\s*""[\s\S]*?terraform/stream を先に apply する', tf, re.S) is not None)
# main / stream の outputs.tf に本当にその output があるか
for root, outs in (("main", ("vpc_id", "runtime_subnet_ids", "endpoint_security_group_id", "kb_bucket_name")),
                   ("stream", ("msk_cluster_arn", "msk_security_group_id", "bootstrap_brokers"))):
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
check("既定のテーブル名は snmp_metrics", re.search(r'variable "table_name"[\s\S]*?default\s*=\s*"snmp_metrics"', tf, re.S) is not None)

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

# ---- output（ops/up.sh と README a-3 がそのまま使う）
for out in ("application_id", "runtime_role_arn", "table_identifier", "job_driver_json", "configuration_overrides_json", "list_job_runs_command", "list_tables_command"):
    check(f"output {out} がある", re.search(r'^output "' + out + r'"', tf, re.M) is not None)
check("job_driver は S3 Tables のカタログを spark-submit の --conf で渡す",
      "software.amazon.s3tables.iceberg.S3TablesCatalog" in tf and "org.apache.iceberg.spark.SparkCatalog" in tf
      and "IcebergSparkSessionExtensions" in tf)
check("job_driver の引数は bootstrap / テーブル / checkpoint の 3 つ",
      re.search(r'entryPointArguments\s*=\s*\[local\.bootstrap,\s*local\.iceberg_table,\s*"s3://\$\{local\.bucket\}/\$\{local\.checkpoint\}/"\]', tf) is not None)
check("job_driver は jars を s3://<バケット>/analytics/jars/ から読む", "spark.jars=s3://${local.bucket}/${local.jars_prefix}/*.jar" in tf
      and re.search(r'jars_prefix\s*=\s*"\$\{local\.s3_prefix\}/jars"', tf) is not None and re.search(r's3_prefix\s*=\s*"analytics"', tf) is not None)
check("ジョブは 2 vCPU（driver 1 + executor 1、動的割り当て無し）",
      "spark.driver.cores=1" in tf and "spark.executor.cores=1" in tf and "spark.executor.instances=1" in tf and "spark.dynamicAllocation.enabled=false" in tf)
check("ドライバーのログは CloudWatch、EMR の managed storage は使わない",
      re.search(r'cloudWatchLoggingConfiguration\s*=\s*\{\s*enabled\s*=\s*true', tf) is not None
      and re.search(r'managedPersistenceMonitoringConfiguration\s*=\s*\{\s*enabled\s*=\s*false', tf) is not None)

# ---- Spark のスクリプト（pyspark 無しで ast と文字列で見る）
tree = ast.parse(src, SRC)
funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
check("build と main がある", {"build", "main"} <= set(funcs))
check("build の引数は spark / bootstrap / table / checkpoint", [a.arg for a in funcs["build"].args.args] == ["spark", "bootstrap", "table", "checkpoint"])
check("TOPICS は metrics,traps（Telegraf の 2 トピック）", re.search(r'^TOPICS\s*=\s*"metrics,traps"$', src, re.M) is not None)
check("Kafka を readStream で読む", '.readStream.format("kafka")' in src and '.option("subscribe", TOPICS)' in src)
for k, v in (("kafka.security.protocol", "SASL_SSL"), ("kafka.sasl.mechanism", "AWS_MSK_IAM"),
             ("kafka.sasl.jaas.config", "software.amazon.msk.auth.iam.IAMLoginModule required;"),
             ("kafka.sasl.client.callback.handler.class", "software.amazon.msk.auth.iam.IAMClientCallbackHandler")):
    check(f"MSK の IAM 認証: {k}", f'.option("{k}", "{v}")' in src)
check("Iceberg に append で書き、toTable で名前を渡す", '.writeStream.format("iceberg")' in src and '.outputMode("append")' in src and ".toTable(table)" in src)
check("checkpoint を渡す", '.option("checkpointLocation", checkpoint)' in src)
check("60 秒ごとのマイクロバッチ", 'processingTime="60 seconds"' in src)
# select の各行は「….alias("列")」か、そのままの列名「F.col("topic")」
block = src.split("rows = parsed.select(")[1].split(").where(")[0]
aliases = [a or b for a, b in re.findall(r'(?:\.alias\("([a-z_]+)"\)|^\s*F\.col\("([a-z_]+)"\)),\s*$', block, re.M)]
check("スクリプトの列は tables.tf の列と同じ順", aliases == TABLE_COLUMNS)
check("timestamp が無い行は捨てる", '.where(F.col("ts").isNotNull())' in src)
check("tags / fields は JSON 文字列のまま", 'F.to_json(F.col("m.tags")).alias("tags_json")' in src and 'F.to_json(F.col("m.fields")).alias("fields_json")' in src)

# main を pyspark 無しで動かす（引数が足りなければ 2 で返り、SparkSession には触らない）
ns = {}
main_src = ast.Module(body=[funcs["main"]], type_ignores=[])
quiet = types.SimpleNamespace(stderr=io.StringIO())  # 使い方の表示は捨てる
exec(compile(main_src, SRC, "exec"), {"sys": quiet, "SparkSession": None, "build": None}, ns)
check("引数が 3 つでなければ 2 を返す", ns["main"](["x"]) == 2 and ns["main"](["x", "a", "b"]) == 2 and ns["main"](["x", "a", "b", "c", "d"]) == 2)

# ---- ops/up.sh / ops/down.sh とのつながり
check("up.sh は 6 本の jar を置く", len(re.findall(r'^\s*"\$MAVEN/', up, re.M)) == 6)
for jar in ("spark-sql-kafka-0-10_2.12", "spark-token-provider-kafka-0-10_2.12", "kafka-clients", "commons-pool2", "aws-msk-iam-auth", "s3-tables-catalog-for-iceberg-runtime"):
    check(f"up.sh の jar に {jar}", jar in up)
check("up.sh の SPARK_VERSION は emr_release_label の Spark（3.5.6）", re.search(r'^SPARK_VERSION=3\.5\.6$', up, re.M) is not None
      and "7.13.0 = Spark 3.5.6" in tf)
check("up.sh は analytics を stream の後に apply し、job を STREAMING で起こす",
      up.index("tf_apply stream") < up.index("tf_apply analytics") < up.index("--mode STREAMING"))
check("up.sh は動いているジョブがあれば起こさない", "--states SUBMITTED PENDING SCHEDULED RUNNING" in up)
check("up.sh は PHASE=2 で SKIP_STREAM=1 なら analytics も飛ばす", re.search(r'SKIP_STREAM=1 なので analytics も作らない[^\n]*\n\s*SKIP_ANALYTICS=1', up) is not None)
check("down.sh は job を cancel → stop-application → destroy analytics → destroy graph の順",
      down.index("cancel-job-run") < down.index("stop-application") < down.index("destroy_root analytics") < down.index("destroy_root graph") < down.index("destroy_root stream"))

# outputs の JSON が本当に JSON になる形か（jsonencode の中身の構造を軽く見る）
check("job_driver_json は sparkSubmit の 3 キー", all(k in tf for k in ("entryPoint ", "entryPointArguments", "sparkSubmitParameters")))
print(f"通過 {passed} / 失敗 0")
