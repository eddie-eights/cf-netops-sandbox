"""Nautobot の中身を読んで、Telegraf の dialin の機器の一覧（SSM）と Neptune の物理層に合わせる。nautobot/jobs/netops_jobs.py の Job と、
起動時の bootstrap.py が呼ぶ。対応付けは nb_map.py。

  1. 機器の一覧: Service（gnmi / snmp）を持つ機器から作った文字列が SSM の今の値と違うときだけ書き換え、dialin のサービスを作り直す
     （ECS は起動時に secrets を読むので、書き換えただけでは反映されない）。空の一覧は書かない（Telegraf が起動できなくなる）
  2. Neptune: graph.sync_physical()（Gremlin）で物理層だけを差分で合わせる。status と上の層は残る。機器が 1 台も無いときは触らない（全部消えるので）

同時に 2 つ走ると古い読みが後から書くことがあるので、Redis のロック（Django の cache）の中で「読む → 書く」をする。

環境変数（terraform/pipeline/nautobot がコンテナに渡す）:
  DIALIN_GNMI_PARAMETER / DIALIN_SNMP_PARAMETER   一覧を書く SSM のパラメータ名（terraform/pipeline/stream の出力）。空なら 1 を飛ばす
  TELEGRAF_CLUSTER / TELEGRAF_DIALIN_SERVICE     作り直す ECS のサービス
  NEPTUNE_ENDPOINT                               <host>:8182。空なら 2 を飛ばす
"""
import os

import graph
import nb_map
import toolkit

LOCK = "netops-nautobot-sync"
LOCK_TIMEOUT = 900   # ロックを持ったまま落ちても、この秒数で外れる。Job の制限時間（Celery の hard limit 600 秒）より長くし、走っている途中で外れないようにする
LOCK_WAIT = 240      # 先に走っている同期を待つ秒数


def read() -> tuple[list[dict], list[dict]]:
    """Nautobot の DB から nb_map.to_graph() / targets() に渡す rows（機器）と cables（回線）を作る"""
    from nautobot.dcim.models import Cable, Device

    rows = []
    for d in (Device.objects.select_related("location", "role", "primary_ip4")
              .prefetch_related("interfaces__ip_addresses", "interfaces__lag", "services").order_by("name")):
        interfaces = []
        for i in d.interfaces.all():
            hosts = sorted((str(ip.host) for ip in i.ip_addresses.all()), key=lambda h: (":" in h, h))   # v4 が先
            interfaces.append({"name": i.name, "address": hosts[0] if hosts else "", "lag": i.lag.name if i.lag else ""})
        rows.append({
            "name": d.name, "site": d.location.name if d.location else "", "role": d.role.name if d.role else "",
            "mgmt_ip": str(d.primary_ip4.host) if d.primary_ip4 else "", "asn": d.cf.get("asn"), "interfaces": interfaces,
            "services": [{"name": s.name, "protocol": s.protocol, "ports": list(s.ports or [])} for s in d.services.all()],
        })
    cables = []
    for c in Cable.objects.prefetch_related("terminations__interface__device"):
        # 両端とも connector 1 の、機器に直に付いた Interface のものだけ（回路や電源のケーブル、片側だけのもの、Module の Interface（device が無い）は回線にしない）
        ends = {e.cable_end: e.interface for e in c.terminations.all()
                if e.connector == 1 and e.interface is not None and e.interface.device is not None}
        if set(ends) != {"A", "B"}:
            continue
        cables.append({"a": ends["A"].device.name, "a_if": ends["A"].name, "b": ends["B"].device.name, "b_if": ends["B"].name,
                       "role": c.cf.get("link_role"), "bandwidth_mbps": c.cf.get("bandwidth_mbps")})
    return rows, cables


def push_targets(rows: list[dict], log, force_redeploy: bool = False) -> dict:
    """dialin の一覧を SSM に合わせ、変わったら（か force_redeploy なら）dialin のサービスを作り直す"""
    names = {"gnmi-targets": os.environ.get("DIALIN_GNMI_PARAMETER", ""), "snmp-agents": os.environ.get("DIALIN_SNMP_PARAMETER", "")}
    cluster, service = os.environ.get("TELEGRAF_CLUSTER", ""), os.environ.get("TELEGRAF_DIALIN_SERVICE", "")
    if not all(names.values()) or not cluster or not service:
        log.warning("dialin の一覧の書き先が渡されていない（stream が dialin_targets_from_nautobot でない）。Telegraf の一覧は触らない")
        return {"skipped": True}
    ssm, changed = toolkit.client("ssm"), []
    for key, value in nb_map.targets(rows).items():
        if not value:
            log.warning("%s が空になる（Service を持つ機器が無い）。SSM は書き換えない", key)
            continue
        if ssm.get_parameter(Name=names[key])["Parameter"]["Value"] == value:
            continue
        ssm.put_parameter(Name=names[key], Value=value, Type="String", Overwrite=True)
        changed.append(key)
        log.info("%s を書き換えた: %s", names[key], value)
    if changed or force_redeploy:
        toolkit.client("ecs").update_service(cluster=cluster, service=service, forceNewDeployment=True)
        log.info("Telegraf の dialin（%s）を作り直す", service)
    return {"changed": changed, "redeployed": bool(changed or force_redeploy)}


def sync(log, force_redeploy: bool = False) -> dict:
    """Nautobot → Telegraf の一覧と Neptune。片方が失敗してももう片方はやり、最後に失敗をまとめて上げる（Job が失敗になる）"""
    from django.core.cache import cache

    with cache.lock(LOCK, timeout=LOCK_TIMEOUT, blocking_timeout=LOCK_WAIT):
        rows, cables = read()
        devices, links, warnings = nb_map.to_graph(rows, cables)
        for w in warnings:
            log.warning(w)
        out, errors = {"devices": len(devices), "links": len(links)}, []
        try:
            out["telegraf"] = push_targets(rows, log, force_redeploy)
        except Exception as e:  # SSM / ECS の失敗。Neptune は続ける
            errors.append(f"Telegraf の一覧: {e}")
        try:
            if not devices:
                # 空のまま合わせると Neptune の物理層が（status と上の層への辺ごと）全部消える。seed の失敗や入れ直しの途中を、消す指示とは読まない
                log.warning("Nautobot に機器が 1 台も無い。Neptune は触らない")
                out["neptune"] = {"skipped": True}
            elif graph.configured():
                out["neptune"] = graph.sync_physical(devices, links)
                log.info("Neptune の物理層を合わせた: %s", out["neptune"])
            else:
                log.warning("NEPTUNE_ENDPOINT が無い。Neptune は触らない")
        except Exception as e:
            errors.append(f"Neptune: {e}")
    if errors:
        raise RuntimeError(" / ".join(errors))
    return out
