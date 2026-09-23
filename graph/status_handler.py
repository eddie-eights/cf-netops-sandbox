"""terraform/pipeline/graph の Lambda（<prefix>-graph-status）。Spark の検知（EventBridge の <接頭辞>.spark / AnomalyOpened と AnomalyResolved）を受けて、
Neptune の機器と回線の動的な状態（property status）を書く。設計の「動的なステータス反映（トラップ / ログ → Lambda → Neptune の属性を UP → DOWN）」。

  AnomalyOpened   kind=link_down → 機器 device_id のインタフェース target が付く回線（辺）を DOWN
                  それ以外（trap）  → 機器（頂点）を ALARM（トラップは機器が落ちた印ではないので DOWN にしない）
  AnomalyResolved 同じ要素を UP に戻す（trap の解消は、機器が ALARM のときだけ UP。IF の分からない linkDown の DOWN は上書きしない）

zip には agent/graph.py を同梱する（Gremlin の組み立てと boto3 の neptunedata はそちら）。エンドポイントは環境変数 NEPTUNE_ENDPOINT。
トポロジに無い機器やインタフェースは捨てずに「未登録」の頂点として Neptune に残し（graph.set_status）、WARNING で UNREGISTERED を
ログに出す（登録漏れの印。CloudWatch Logs Insights で `filter @message like /UNREGISTERED/` と探す。lab に足した機器は
ops/sync-graph.sh --replace で登録すると、未登録の頂点は置き換わる）。
"""
import json
import logging

import graph

log = logging.getLogger()
log.setLevel(logging.INFO)

STATUS_OF = {"AnomalyOpened": "DOWN", "AnomalyResolved": "UP"}


def apply(detail_type: str, detail: dict) -> dict:
    """1 イベントを Neptune に反映する。戻り値は graph.set_status の結果（ignored のときは理由）"""
    status = STATUS_OF.get(detail_type)
    if status is None:
        return {"ignored": f"detail-type {detail_type}"}
    device_id = str(detail.get("device_id") or "")
    if not device_id or device_id == "?":
        return {"ignored": "device_id が無い"}
    kind, target = detail.get("kind"), str(detail.get("target") or "")
    if kind == "link_down" and target and target != "?":
        return graph.set_status(device_id, target, status)
    if kind == "link_down":
        return graph.set_status(device_id, "", status)   # どのインタフェースか分からない linkDown は機器に付ける
    if status == "DOWN":
        return graph.set_status(device_id, "", "ALARM")
    # trap は TTL で閉じる（spark/snmp_sinks.py の TRAP_TTL）。そのあいだに機器が DOWN になっていたら、それは linkDown の印なので残す
    return graph.set_status(device_id, "", "UP", only_if="ALARM")


def handler(event, context=None):
    detail = event.get("detail") or {}
    if isinstance(detail, str):
        detail = json.loads(detail)
    r = apply(event.get("detail-type", ""), detail)
    if r.get("unregistered"):
        log.warning("UNREGISTERED 未登録の機器・インタフェースの異常（トポロジに登録する）: %s %s -> %s", event.get("detail-type"),
                    json.dumps(detail, ensure_ascii=False), json.dumps(r, ensure_ascii=False))
    else:
        log.info("%s %s -> %s", event.get("detail-type"), json.dumps(detail, ensure_ascii=False), json.dumps(r, ensure_ascii=False))
    return r
