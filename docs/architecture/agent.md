# 構成: エージェント（agent）

← [構成](README.md)

`terraform/agent`（`AGENT=1`。既定）。AgentCore Runtime、ガードレール、KB（`CREATE_KB=1` のときだけ）。

## チャットの経路

```mermaid
flowchart LR
  PC["利用者の PC<br/>localhost:8080"] -->|"SSM（ssmmessages、TLS）"| WEB["Web の EC2<br/>Gradio 127.0.0.1:8080<br/>チャット / トポロジ / 承認"]
  WEB -->|"invoke_agent_runtime"| RT["AgentCore Runtime<br/>agent/app.py"]
  RT -->|"Retrieve（HYBRID + Rerank、20 件 → 5 件）"| KB["ナレッジベース<br/>CREATE_KB=1 のときだけ"]
  RT -->|"Converse + Guardrail"| LLM["Nova 2 Lite（jp.）"]
  RT -->|"ツール（最大 5 往復）"| TOOLS["list_devices / neighbors / blast_radius / topology_graph / layers<br/>search_logs / query_metrics / query_history / list_proposals"]
  RT -.->|"WORKFLOW=1"| GW["Gateway（MCP）→ tools Lambda"]
```

- Web の EC2 は土台（[core.md](core.md)）。EC2 にパブリック IP も受信ルールも無い。SSM Agent が内側から ssmmessages へつなぐ。
- Runtime を呼ぶのは EC2 のインスタンスロール。ブラウザに AWS の認証情報は置かない。
- ツールは Neptune（トポロジ 3 層と修復案。無ければトポロジは `agent/data/` の静的な 8 台と層）、OpenSearch Serverless、Prometheus を読む。異常の一覧を返すツールは無い（2026-10-02 にやめた。アラートは Grafana と Splunk の画面で見る）。
- Gateway（MCP）と tools Lambda は WORKFLOW で作る（[workflow.md](workflow.md)）。
- `agent/app.py` は Runtime のコンテナで動き、EC2 には置かない（[README.md](README.md) の「どのファイルがどこで動くか」）。
