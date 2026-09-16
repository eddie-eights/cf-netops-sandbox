"""Kafka（MSK、IAM 認証）の metrics / traps トピックを読み、S3 Tables の Iceberg テーブルに追記し続ける Spark Structured Streaming のジョブ。

EMR Serverless の上で動く（terraform/analytics）。起動は ops/up.sh の a-3（start-job-run）で、引数は 3 つ:
  1. bootstrap servers（MSK の SASL/IAM、9098。terraform/stream の output bootstrap_brokers）
  2. テーブル名（catalog.namespace.table。terraform/analytics の output table_identifier）
  3. checkpoint の場所（s3://<バケット>/analytics/checkpoint/）
Kafka と S3 Tables の jar、カタログの設定は spark-submit の --conf で渡す（terraform/analytics の output job_driver_json）。

Telegraf の JSON 出力（outputs.kafka の data_format = "json"、json_timestamp_units = "1s"）は
  {"fields": {…}, "name": "<measurement>", "tags": {"agent_host": "…", "host": "…", …}, "timestamp": <秒>}
の形。列に分けるのは timestamp / name / agent_host / host だけで、tags と fields は JSON 文字列のまま入れる
（機器やメトリクスが増えてもテーブルの列を変えないため。terraform/analytics/tables.tf の列と同じ）。
"""
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

TOPICS = "metrics,traps"

# Telegraf の JSON のうち、列に分ける部分だけ型を書く。tags / fields は文字列のまま
SCHEMA = T.StructType([
    T.StructField("timestamp", T.LongType()),
    T.StructField("name", T.StringType()),
    T.StructField("tags", T.MapType(T.StringType(), T.StringType())),
    T.StructField("fields", T.MapType(T.StringType(), T.StringType())),
])


def build(spark, bootstrap, table, checkpoint):
    """Kafka → 列 → Iceberg の writeStream を組み立てて返す（テストでは start せずに中身だけ見る）"""
    raw = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", TOPICS)
        .option("startingOffsets", "earliest")
        # MSK の IAM 認証（aws-msk-iam-auth の -all jar。AWS ドキュメント「Connect to MSK with IAM」の 4 項目）
        .option("kafka.security.protocol", "SASL_SSL")
        .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
        .option("kafka.sasl.jaas.config", "software.amazon.msk.auth.iam.IAMLoginModule required;")
        .option("kafka.sasl.client.callback.handler.class", "software.amazon.msk.auth.iam.IAMClientCallbackHandler")
        .load()
    )
    parsed = raw.select(
        F.col("topic"),
        F.from_json(F.col("value").cast("string"), SCHEMA).alias("m"),
    )
    rows = parsed.select(
        F.to_timestamp(F.from_unixtime(F.col("m.timestamp"))).alias("ts"),
        F.col("topic"),
        F.col("m.name").alias("measurement"),
        F.col("m.tags")["agent_host"].alias("agent_host"),
        F.col("m.tags")["host"].alias("host"),
        F.to_json(F.col("m.tags")).alias("tags_json"),
        F.to_json(F.col("m.fields")).alias("fields_json"),
        F.current_timestamp().alias("ingested_at"),
    ).where(F.col("ts").isNotNull())
    return (
        rows.writeStream.format("iceberg")
        .outputMode("append")
        .option("checkpointLocation", checkpoint)
        .trigger(processingTime="60 seconds")
        .toTable(table)
    )


def main(argv):
    if len(argv) != 4:
        sys.stderr.write("使い方: snmp_to_iceberg.py <bootstrap servers> <catalog.namespace.table> <checkpoint の s3:// URI>\n")
        return 2
    bootstrap, table, checkpoint = argv[1], argv[2], argv[3]
    spark = SparkSession.builder.appName("snmp_to_iceberg").getOrCreate()
    query = build(spark, bootstrap, table, checkpoint)
    query.awaitTermination()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
