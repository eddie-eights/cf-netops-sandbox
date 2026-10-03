"""terraform/pipeline/graph の Lambda（<prefix>-graph-status）。Grafana と Splunk のアラート（SNS のトピック <接頭辞>-alerts）を受けて、
Neptune の機器と回線の動的な状態（property status）を書く。設計の「動的なステータス反映（トラップ / ログ → Lambda → Neptune の属性を UP → DOWN）」。

  firing    kind=link_down → 機器 device_id のインタフェース target が付く回線（辺）を DOWN
            kind=bgp_down  → 機器 device_id の BGP のセッション（頂点 bgp_session。target = 相手の IP）を DOWN（gNMI の session-state）
            kind=isis_down → 機器 device_id の IS-IS の隣接（頂点 isis_adjacency。target = サブインタフェース）を DOWN（gNMI の adjacency）
            それ以外（trap）  → 機器（頂点）を ALARM（トラップは機器が落ちた印ではないので DOWN にしない）
  resolved  同じ要素を UP に戻す（trap の解消は、機器が ALARM のときだけ UP。IF の分からない linkDown の DOWN は上書きしない）

メッセージの形は workflow/rules.py の alerts_from_message が読む（ワーカーの starter と同じ読み手。zip に rules.py として同梱する）。
1 通に何件か入っていることがある（Grafana はグループごとに 1 通）。同じ障害を Grafana と Splunk の両方が知らせても、書くのは同じ値なので害は無い。
zip には agent/graph.py も同梱する（Gremlin の組み立てと boto3 の neptunedata はそちら）。エンドポイントは環境変数 NEPTUNE_ENDPOINT。
トポロジに無い機器やインタフェースは捨てずに「未登録」の頂点として Neptune に残し（graph.set_status）、WARNING で UNREGISTERED を
ログに出す（登録漏れの印。CloudWatch Logs Insights で `filter @message like /UNREGISTERED/` と探す。lab に足した機器は
ops/sync-graph.sh --replace で登録すると、未登録の頂点は置き換わる）。
"""
import json
import logging

import graph
import rules

log = logging.getLogger()
log.setLevel(logging.INFO)

STATUS_OF = {"firing": "DOWN", "resolved": "UP"}
LAYER_KIND = {"bgp_down": "bgp", "isis_down": "isis"}   # アラートの kind → graph.set_layer_status の kind（頂点の id の真ん中）


def apply(alert: dict) -> dict:
    """アラート 1 件を Neptune に反映する。戻り値は graph.set_status の結果（ignored のときは理由）"""
    status = STATUS_OF.get(alert.get("status"))
    if status is None:
        return {"ignored": f"status {alert.get('status')}"}
    device_id = str(alert.get("device_id") or "")
    if not device_id or device_id == "?":
        return {"ignored": "device_id が無い"}
    kind, target = alert.get("kind"), str(alert.get("target") or "")
    if kind in LAYER_KIND:
        if not target or target == "?":
            return {"ignored": f"{kind} の target が無い"}
        return graph.set_layer_status(device_id, LAYER_KIND[kind], target, status)
    if kind == "link_down" and target and target != "?":
        return graph.set_status(device_id, target, status)
    if kind == "link_down":
        return graph.set_status(device_id, "", status)   # どのインタフェースか分からない linkDown は機器に付ける
    if status == "DOWN":
        return graph.set_status(device_id, "", "ALARM")
    # trap は TTL で閉じる（splunk/ の保存済みサーチ）。そのあいだに機器が DOWN になっていたら、それは linkDown の印なので残す
    return graph.set_status(device_id, "", "UP", only_if="ALARM")


def handler(event, context=None):
    """SNS からの呼び出し（Records[].Sns.Message）。1 件でも Neptune に書けなければ例外で落とし、Lambda の非同期の再試行に任せる（書き込みは繰り返して害が無い）"""
    results = []
    for rec in event.get("Records") or []:
        message = (rec.get("Sns") or {}).get("Message", "")
        alerts = rules.alerts_from_message(message)
        if not alerts:
            log.warning("読めないメッセージ（捨てる）: %s", str(message)[:300])
            continue
        for a in alerts:
            r = apply(a)
            line = json.dumps({k: a.get(k) for k in ("source", "status", "device_id", "kind", "target")}, ensure_ascii=False)
            if r.get("unregistered"):
                log.warning("UNREGISTERED 未登録の機器・インタフェースの異常（トポロジに登録する）: %s -> %s", line, json.dumps(r, ensure_ascii=False))
            else:
                log.info("%s -> %s", line, json.dumps(r, ensure_ascii=False))
            results.append(r)
    return results
