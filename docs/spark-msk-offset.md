# Spark と MSK — offset は誰が持っているか

← [README](../README.md)

検証日 2026-09-18 / Spark 4.2.0（Apache Spark の `docs/latest/streaming/*`）。**この PoC が動かすのは EMR Serverless の emr-7.13.0 = Spark 3.5.6** で、ここに書いた offset と `group.id` の扱いは 3.5 系でも同じ。

## 結論

- Spark は **読んだ位置（offset）を自分の checkpoint に書く**。Kafka にはコミットしない。
- `group.id` は **クエリごとに自動生成**される（接頭辞だけ `groupIdPrefix` で変えられる）。
- だから **Kafka 側のコンシューマグループ監視には何も出てこない**。lag は自分で出す。

---

## 絵にすると

```
    MSK                        Spark のジョブ
 ┌────────┐   ① どこまで読める？   ┌──────────────┐
 │ metrics│ ────────────────────▶ │ latest offset │
 │ topic  │   ② 100〜200 を読む    │ を聞くだけ    │
 └────────┘ ◀──────────────────── └──────────────┘
      ✕ コミットは返さない              │
                                       │ ③ 処理して書く
                                       ▼ ④「200 まで済」を書く
                              s3://.../ckpt/  ← ここが正本
```

再起動すると **checkpoint を読んで 201 から続ける**。Kafka には「誰がどこまで読んだか」の記録が残らない。

---

## なぜ Kafka にコミットしないのか

**正本が 2 つあると壊れるから。**

「データを書いた」と「offset を進めた」がズレると、重複か欠損が出る。
Spark は **出力と offset を同じ checkpoint で一緒に管理**することで、
障害から復旧したときに「どこから書き直せばいいか」を 1 か所で決められるようにしている。

Kafka にもコミットしてしまうと、Kafka 側の位置と checkpoint の位置が食い違い、
どちらが正しいのか分からなくなる。

---

## 何が起きるか（実務での引っかかり）

| やろうとすること | 結果 |
| --- | --- |
| `kafka-consumer-groups.sh --describe` で lag を見る | **グループが無い**（毎回名前が変わる）ので見えない |
| MSK の CloudWatch のコンシューマグループ系メトリクス | 同じ理由で出てこない |
| Kafka lag exporter / Burrow などの監視ツール | 同じ理由で対象外 |
| `kafka.group.id` を自分で指定する | 名前は付くが **Spark はやはりコミットしない**ので lag は動かない |

> 「Spark は動いているのに、Kafka の監視画面には何のコンシューマも出てこない」
> —— 壊れているのではなく、こういう作りになっている。

---

## では lag はどう見るか

### 1. Spark 側の進捗を見る（まずこれ）

```python
q = df.writeStream...start()
print(q.lastProgress)
# inputRowsPerSecond   … 入ってくる速さ
# processedRowsPerSecond … 処理できている速さ
# sources[0].endOffset … このバッチでどこまで読んだか
```

**`inputRowsPerSecond` > `processedRowsPerSecond` が続いたら追いつけていない。**
Spark UI の Streaming タブでも同じものがグラフで見える。

### 2. event time の遅れを見る（運用でいちばん役に立つ）

```python
# 「いま」と「処理したデータの時刻」の差を毎バッチ出す
def handle(batch_df, batch_id):
    delay = batch_df.selectExpr("unix_timestamp() - max(unix_timestamp(event_time))").first()[0]
    # CloudWatch へ put_metric_data する
```

**何秒前のデータを処理しているか**が分かる。Kafka の lag より直接的で、アラートも作りやすい。

### 3. Kafka の最新 offset と比べる（正確に出したいとき）

`endOffset`（Spark が読んだ位置）と、トピックの最新 offset を自分で取って引き算する。
手間はかかるが、これが本来の意味の lag。

---

## MSK 側で気をつけること

| 項目 | 内容 |
| --- | --- |
| **パーティション数** | **1 パーティション = 1 タスク**。Spark の並列度の上限になる。executor のコア数と 1:1 が目安 |
| **流量の制御** | `maxOffsetsPerTrigger` で 1 バッチの上限を決める。**自動のバックプレッシャーは無い** |
| **初回起動** | `startingOffsets=earliest` で溜まった分を 1 バッチで読むと落ちる。必ず上限を付ける |
| **トピックの変更** | `subscribe` するトピックを変えると **checkpoint は作り直し**。だから「機器ごとにトピック」を作らない |
| **キーの偏り** | `device_id` をそのままキーにすると偏る。複合キーにするか、順序を捨てて分散させる |
| **パーティション上限** | ブローカーのサイズで決まる（kafka.t3.small = 300、m5.large = 1000） |

### 接続（IAM 認証）

```python
(spark.readStream.format("kafka")
   .option("kafka.bootstrap.servers", "b-1...:9098,b-2...:9098")
   .option("subscribe", "metrics")
   .option("startingOffsets", "latest")
   .option("maxOffsetsPerTrigger", 100000)
   .option("kafka.security.protocol", "SASL_SSL")
   .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
   .load())
```

- IAM 認証は `aws-msk-iam-auth` の jar と、`sasl.jaas.config` / コールバックハンドラの指定が要る
  （上の断片はその 2 行を省いてある。実際に動かしている全文は `spark/snmp_sinks.py`）
- Spark は **VPC の中**から MSK に届く必要がある（EMR Serverless / Glue は VPC 設定、セキュリティグループで 9098 を許可）

---

## 1 行でまとめ

**Kafka は「置いてあるログ」、どこまで読んだかを覚えているのは Spark の checkpoint。
だから監視も復旧も Kafka 側ではなく Spark 側で組む。**
