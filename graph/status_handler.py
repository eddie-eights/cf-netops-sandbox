"""terraform/graph の Lambda（<prefix>-graph-status）。Spark の検知（EventBridge の netops.spark / AnomalyOpened と AnomalyResolved）を受けて、
Neptune の機器と回線の動的な状態（property status）を書く。設計の「動的なステータス反映（トラップ / ログ → Lambda → Neptune の属性を UP → DOWN）」。

  AnomalyOpened   kind=link_down → 機器 device_id のインタフェース target が付く回線（辺）を DOWN
                  それ以外（trap）  → 機器（頂点）を ALARM（トラップは機器が落ちた印ではないので DOWN にしない）
  AnomalyResolved 同じ要素を UP に戻す

zip には agent/graph.py を同梱する（Gremlin の組み立てと boto3 の neptunedata はそちら）。エンドポイントは環境変数 NEPTUNE_ENDPOINT。
トポロジに無い機器やインタフェースは updated 0 で終わる（エラーにしない。検知がトポロジの投入より先に来ることがある）。
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
    return graph.set_status(device_id, "", "ALARM" if status == "DOWN" else "UP")


def handler(event, context=None):
    detail = event.get("detail") or {}
    if isinstance(detail, str):
        detail = json.loads(detail)
    r = apply(event.get("detail-type", ""), detail)
    log.info("%s %s -> %s", event.get("detail-type"), json.dumps(detail, ensure_ascii=False), json.dumps(r, ensure_ascii=False))
    return r
