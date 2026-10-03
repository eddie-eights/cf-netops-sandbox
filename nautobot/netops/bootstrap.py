"""Nautobot の web コンテナが、migrate（上流の入口 /docker-entrypoint.sh の post_upgrade）のあと、uwsgi を起こす前に 1 回走らせる。
何度走っても同じ結果になる（タスクが作り直されるたびに走る）。途中で失敗しても web は上げる（ログに出して次へ。画面から直せるように）。

  1. 管理者: NAUTOBOT_SUPERUSER_NAME / NAUTOBOT_SUPERUSER_PASSWORD（SSM の SecureString を ECS が渡す）で作り、パスワードを合わせる。
     上流の NAUTOBOT_CREATE_SUPERUSER は API トークンも要るので使わない
  2. custom field: Device の asn、Cable の link_role / bandwidth_mbps（nb_map.py の対応付けが読む）
  3. 最初の seed: 機器が 1 台も無いときだけ、イメージに入れた lab の定義（lab_seed.json = lab/lab_topology.py の出力）から
     Location / Role / Device / Interface / IPAddress / Service / Cable を作る。あとは Nautobot が正で、lab を変えてもここは入れ直さない
  4. Job: JOBS_ROOT の netops_jobs の 2 つを登録して有効にし、JobHook（netops-sync）を張る
  5. 起動時の同期: Telegraf の dialin の一覧と Neptune の物理層を今の Nautobot に合わせる（nb_sync.sync）
"""
import contextlib
import ipaddress
import json
import logging
import os

import nautobot

nautobot.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import transaction  # noqa: E402
from nautobot.dcim.models import Cable, Device, DeviceType, Interface, Location, LocationType, Manufacturer  # noqa: E402
from nautobot.extras.context_managers import web_request_context  # noqa: E402
from nautobot.extras.jobs import get_jobs  # noqa: E402
from nautobot.extras.models import CustomField, JobHook, JobQueue, Role, Status  # noqa: E402
from nautobot.extras.models import Job as JobModel  # noqa: E402
from nautobot.extras.utils import refresh_job_model_from_job_class  # noqa: E402
from nautobot.ipam.models import IPAddress, Prefix, Service, get_default_namespace  # noqa: E402

import nb_map  # noqa: E402
import nb_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="bootstrap %(levelname)s %(message)s")
log = logging.getLogger("netops.bootstrap")

SEED = os.environ.get("NETOPS_SEED", os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab_seed.json"))
JOBS = ("netops_jobs.SyncTopology", "netops_jobs.SyncOnChange")
HOOK = "netops-sync"
SWITCH_TYPE, VM_TYPE = ("Nokia", "SR Linux"), ("Generic", "Linux VM")
CUSTOM_FIELDS = (("asn", "ASN", "integer", Device), ("link_role", "Link role", "text", Cable), ("bandwidth_mbps", "Bandwidth (Mbps)", "integer", Cable))


def superuser():
    name, password = os.environ.get("NAUTOBOT_SUPERUSER_NAME", "admin"), os.environ.get("NAUTOBOT_SUPERUSER_PASSWORD", "")
    if not password:
        log.warning("NAUTOBOT_SUPERUSER_PASSWORD が無い。管理者は作らない")
        return
    user, created = get_user_model().objects.get_or_create(username=name)
    user.is_superuser = user.is_staff = user.is_active = True
    user.set_password(password)
    user.save()
    log.info("管理者 %s を%s", name, "作った" if created else "合わせた")


def custom_fields():
    # custom field を足すと Nautobot は既存の行に既定値を配る Job（ProvisionCustomField）を積む。積むのに「誰の変更か」が要るので、管理者の変更として行う
    # （管理者がいなければそのまま作る。Job は積めずに ERROR が出るが、field はできる）
    user = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("username").first()
    with web_request_context(user, context_detail="netops-bootstrap") if user else contextlib.nullcontext():
        for key, label, kind, model in CUSTOM_FIELDS:
            field, _ = CustomField.objects.get_or_create(key=key, defaults={"label": label, "type": kind})
            field.content_types.add(ContentType.objects.get_for_model(model))


def seed():
    if Device.objects.exists():
        log.info("機器がもう入っている。seed は飛ばす")
        return
    with open(SEED, encoding="utf-8") as f:
        plan = nb_map.seed_plan(json.load(f))
    active, connected = Status.objects.get(name="Active"), Status.objects.get(name="Connected")
    namespace, device_ct = get_default_namespace(), ContentType.objects.get_for_model(Device)
    with transaction.atomic():
        site_type, _ = LocationType.objects.get_or_create(name="Site")
        site_type.content_types.add(device_ct)
        sites = {s: Location.objects.get_or_create(name=s, location_type=site_type, defaults={"status": active})[0] for s in plan["sites"]}
        roles = {}
        for r in plan["roles"]:
            roles[r], _ = Role.objects.get_or_create(name=r)
            roles[r].content_types.add(device_ct)
        types = {}
        for vm, (maker, model) in ((False, SWITCH_TYPE), (True, VM_TYPE)):
            manufacturer, _ = Manufacturer.objects.get_or_create(name=maker)
            types[vm], _ = DeviceType.objects.get_or_create(manufacturer=manufacturer, model=model)
        for p in plan["prefixes"]:
            net = ipaddress.ip_network(p)
            if not Prefix.objects.filter(namespace=namespace, network=str(net.network_address), prefix_length=net.prefixlen).exists():
                Prefix(prefix=p, namespace=namespace, status=active, type="network").validated_save()
        interfaces = {}
        for d in plan["devices"]:
            device = Device(name=d["name"], device_type=types[d["vm"]], role=roles[d["role"]], location=sites[d["site"]], status=active)
            if d["asn"] is not None:
                device._custom_field_data = {"asn": int(d["asn"])}
            device.validated_save()
            for i in d["interfaces"]:
                interface = Interface(device=device, name=i["name"], type=i["type"], status=active, mgmt_only=i["mgmt"],
                                      lag=interfaces.get((d["name"], i["lag"])) if i["lag"] else None)
                interface.validated_save()
                interfaces[(d["name"], i["name"])] = interface
                if not i["address"]:
                    continue
                host = ipaddress.ip_address(i["address"])
                ip = IPAddress(address=f"{host}/{host.max_prefixlen}", namespace=namespace, status=active)
                ip.validated_save()
                interface.add_ip_addresses(ip)
                if i["mgmt"] and host.version == 4:
                    device.primary_ip4 = ip
                    device.validated_save()
            for name, protocol, port in d["services"]:
                Service(device=device, name=name, protocol=protocol, ports=[port]).validated_save()
        for c in plan["cables"]:
            cable = Cable(termination_a=interfaces[(c["a"], c["a_if"])], termination_b=interfaces[(c["b"], c["b_if"])], status=connected)
            cable._custom_field_data = {k: v for k, v in (("link_role", c["role"]), ("bandwidth_mbps", c["bandwidth_mbps"])) if v}
            cable.validated_save()
    log.info("lab の定義から seed した（%d 台 / %d 本）", len(plan["devices"]), len(plan["cables"]))


def jobs():
    found, models = get_jobs(reload=True), {}
    for path in JOBS:
        if path not in found:
            raise RuntimeError(f"Job {path} が JOBS_ROOT から読めない")
        models[path], _ = refresh_job_model_from_job_class(JobModel, found[path], JobQueue)
        if models[path] is None:
            raise RuntimeError(f"Job {path} を登録できなかった")
        if not models[path].enabled:
            models[path].enabled = True
            models[path].save()
    # 画面で止めたり変えたりしていても、起動のたびに決まった形へ戻す
    hook, _ = JobHook.objects.update_or_create(
        name=HOOK, defaults={"job": models[JOBS[1]], "type_create": True, "type_update": True, "type_delete": True, "enabled": True})
    hook.content_types.set(ContentType.objects.get_for_models(Device, Interface, Cable, IPAddress, Service, Location, Role).values())
    log.info("Job と JobHook（%s）を用意した", HOOK)


def first_sync():
    log.info("起動時の同期: %s", nb_sync.sync(log))


if __name__ == "__main__":
    steps_skip = set()
    for step in (superuser, custom_fields, seed, jobs, first_sync):
        if step in steps_skip:
            log.warning("%s は飛ばす（seed が失敗した）", step.__name__)
            continue
        try:
            step()
        except Exception:  # どれが落ちても web は上げる（画面と Job の結果から直せるように）
            log.exception("%s が失敗した", step.__name__)
            if step is seed:   # 中身が入っていないまま同期しない（Job の用意だけして終わる）
                steps_skip.add(first_sync)
