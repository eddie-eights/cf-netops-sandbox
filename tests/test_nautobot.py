"""Nautobot 連携（nautobot/、terraform/pipeline/nautobot、ops/up.sh の NAUTOBOT=1）の模擬テスト。AWS にも Nautobot にも触れない。
- 対応付け（nautobot/netops/nb_map.py）: lab の定義 → seed_plan → Nautobot → to_graph / targets と一周すると、lab と同じ機器・回線・Telegraf の一覧に戻る
- 同期（nb_sync.push_targets）: 変わったときだけ SSM を書いて dialin を作り直す。空の一覧は書かない
- Web: Nautobot があるあいだは画面からの Neptune の編集を止める（web/topology_view.py）
- 配線: Dockerfile・Terraform・ops/up.sh・ops/down.sh の名前と順序がそろっている
実行は uv run --group dev python tests/test_nautobot.py。"""
import json, logging, os, re, subprocess, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "agent"), os.path.join(ROOT, "nautobot", "netops"), os.path.join(ROOT, "lab")]

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()

def lab_cli(*flags):
    return subprocess.run([sys.executable, os.path.join(ROOT, "lab", "lab_topology.py"), os.path.join(ROOT, "lab"), *flags],
                          check=True, capture_output=True, text=True).stdout.strip()

import nb_map

lab = json.loads(lab_cli())
plan = nb_map.seed_plan(lab)

# ---- seed_plan（lab → Nautobot に作るもの）
check("seed: 機器は lab と同じ台数で、site と role が全部挙がる",
      len(plan["devices"]) == len(lab["devices"]) and set(plan["sites"]) == {d["site"] for d in lab["devices"]}
      and set(plan["roles"]) == {d["role"] for d in lab["devices"]})
check("seed: 監視対象（enabled）の機器だけが gnmi と snmp の Service を持つ",
      all((d["services"] == [nb_map.GNMI_SERVICE, nb_map.SNMP_SERVICE]) == bool(l["enabled"]) and (d["services"] == []) != bool(l["enabled"])
          for d, l in zip(plan["devices"], lab["devices"])))
check("seed: LAG の親がメンバーより先に並び、親の type は lag",
      all([i["name"] for i in d["interfaces"]].index(i["lag"]) < n and
          next(p for p in d["interfaces"] if p["name"] == i["lag"])["type"] == "lag"
          for d in plan["devices"] for n, i in enumerate(d["interfaces"]) if i["lag"]))
check("seed: 管理 IP を持つ機器は、その IP のインタフェースがちょうど 1 つ mgmt",
      all(sum(i["mgmt"] for i in d["interfaces"]) == (1 if d["mgmt_ip"] else 0) for d in plan["devices"]))
check("seed: インタフェースの IP は全部、親の Prefix に入る",
      all(nb_map.parent_prefix(i["address"]) in plan["prefixes"] for d in plan["devices"] for i in d["interfaces"] if i["address"]))
check("parent_prefix: v4 は /24、v6 は /64", nb_map.parent_prefix("203.0.113.11") == "203.0.113.0/24"
      and nb_map.parent_prefix("2001:db8:1:2::5") == "2001:db8:1:2::/64")
check("interface_type", nb_map.interface_type("bond0", {"bond0"}) == "lag" and nb_map.interface_type("ethernet-1/1", set()) == "25gbase-x-sfp28"
      and nb_map.interface_type("mgmt0", set()) == "1000base-t")

# ---- 一周（seed したとおりに Nautobot が持っているとして、nb_sync.read() が返す形に直す）
def as_rows(plan):
    return [{"name": d["name"], "site": d["site"], "role": d["role"], "mgmt_ip": d["mgmt_ip"], "asn": d["asn"],
             "interfaces": [{"name": i["name"], "address": i["address"], "lag": i["lag"]} for i in d["interfaces"]],
             "services": [{"name": s[0], "protocol": s[1], "ports": [s[2]]} for s in d["services"]]} for d in plan["devices"]]
rows = as_rows(plan)
devices, links, warnings = nb_map.to_graph(rows, plan["cables"])
key = lambda l: (l["a"], l["b"], l["a_if"])
strip = lambda d: {k: (sorted(v, key=lambda i: i["name"]) if k == "interfaces" else v) for k, v in d.items() if k != "aliases"}
check("一周: 警告が出ない", warnings == [])
check("一周: 機器が lab と同じ（device_id / site / role / mgmt_ip / asn / enabled / interfaces）",
      devices == sorted((strip(d) for d in lab["devices"]), key=lambda d: d["device_id"]))
check("一周: 回線が lab と同じ（端・kind・role・bandwidth_mbps）", links == sorted(lab["links"], key=key))
t = nb_map.targets(rows)
check("一周: gNMI の一覧が lab/lab_topology.py --gnmi-targets と同じ文字列（最初の同期で Telegraf を作り直さない）",
      t["gnmi-targets"] == lab_cli("--gnmi-targets"))
check("一周: SNMP の一覧が lab/lab_topology.py --snmp-agents と同じ文字列", t["snmp-agents"] == lab_cli("--snmp-agents"))
check("targets のキーは terraform/pipeline/stream の出力のキーと同じ",
      tuple(t) == nb_map.TARGET_KEYS and all(f'"{k}"' in read("terraform", "pipeline", "stream", "telegraf.tf") for k in t))

# ---- to_graph / targets の規則
R = lambda name, role="leaf", ip="", services=(), ifs=(): {"name": name, "site": "s", "role": role, "mgmt_ip": ip, "asn": "",
    "interfaces": [{"name": n, "address": "", "lag": lag} for n, lag in ifs], "services": [{"name": s, "protocol": p, "ports": [port]} for s, p, port in services]}
check("kind: spine が入れば fabric、スイッチどうしは l2、VM は LAG のメンバーなら lag・そうでなければ l2",
      (nb_map.link_kind("leaf", "spine", "", ""), nb_map.link_kind("leaf", "leafsw", "", ""), nb_map.link_kind("host", "leaf", "bond0", ""),
       nb_map.link_kind("leaf", "upstream", "", "")) == ("fabric", "l2", "lag", "l2"))
d2, l2, w2 = nb_map.to_graph(
    [R("b", ifs=[("e1", "")]), R("a", "spine", ifs=[("e1", ""), ("e2", "")]), R("a"), R("")],
    [{"a": "b", "a_if": "e1", "b": "a", "b_if": "e1"}, {"a": "a", "a_if": "e1", "b": "b", "b_if": "e9"},
     {"a": "a", "a_if": "e1", "b": "a", "b_if": "e2"}, {"a": "a", "a_if": "e2", "b": "zz", "b_if": "e1"}])
check("to_graph: a < b にそろえ、同じ機器どうし・知らない機器・重なった回線・名前の無い機器と重複した機器を落として警告する",
      [d["device_id"] for d in d2] == ["a", "b"] and l2 == [{"a": "a", "a_if": "e1", "b": "b", "b_if": "e1", "kind": "fabric", "role": None, "bandwidth_mbps": None}]
      and len(w2) == 5)
check("to_graph: asn の空文字は None、Service が無ければ enabled でない", d2[0]["asn"] is None and d2[0]["enabled"] is False)
t2 = nb_map.targets([R("z", ip="10.0.0.2", services=[("gNMI", "tcp", 6030)]), R("y", ip="10.0.0.1", services=[("snmp", "udp", 161), ("gnmi", "udp", 1)]),
                     R("x", services=[("gnmi", "tcp", 57400)]), R("w", ip="10.0.0.9")])
check("targets: 名前の順、Service の名前は大文字小文字を問わず protocol は合わせる、ポートは Service の値、管理 IP の無い機器は入らない",
      t2 == {"gnmi-targets": '"10.0.0.2:6030"', "snmp-agents": '"udp://10.0.0.1:161"'})
check("targets: 機器が無ければ空文字（nb_sync が書かない）", nb_map.targets([]) == {"gnmi-targets": "", "snmp-agents": ""})
try:
    nb_map.targets([R("bad", ip='1.1.1.1", "x', services=[("gnmi", "tcp", 1)])])
    bad = False
except ValueError:
    bad = True
check("targets: IP でない管理 IP は通さない（telegraf.conf にそのまま入るので）", bad)

# ---- nb_sync.push_targets（boto3 を差し替える）
import toolkit
class Ssm:
    def __init__(self, values): self.values, self.puts = dict(values), []
    def get_parameter(self, Name): return {"Parameter": {"Value": self.values[Name]}}
    def put_parameter(self, **kw): self.puts.append(kw); self.values[kw["Name"]] = kw["Value"]
class Ecs:
    def __init__(self): self.calls = []
    def update_service(self, **kw): self.calls.append(kw)
import nb_sync
log = logging.getLogger("test")
log.setLevel(logging.CRITICAL)
def push(values, rows, force=False, env=True):
    ssm, ecs = Ssm(values), Ecs()
    toolkit.client = lambda name: {"ssm": ssm, "ecs": ecs}[name]
    for k, v in {"DIALIN_GNMI_PARAMETER": "/p/gnmi", "DIALIN_SNMP_PARAMETER": "/p/snmp", "TELEGRAF_CLUSTER": "c", "TELEGRAF_DIALIN_SERVICE": "s"}.items():
        os.environ[k] = v if env else ""
    return nb_sync.push_targets(rows, log, force), ssm, ecs
out, ssm, ecs = push({"/p/gnmi": t["gnmi-targets"], "/p/snmp": t["snmp-agents"]}, rows)
check("push_targets: 同じなら SSM も ECS も触らない", out == {"changed": [], "redeployed": False} and not ssm.puts and not ecs.calls)
out, ssm, ecs = push({"/p/gnmi": "old", "/p/snmp": t["snmp-agents"]}, rows)
check("push_targets: 変わったものだけ String で上書きし、dialin を 1 回作り直す",
      out == {"changed": ["gnmi-targets"], "redeployed": True}
      and ssm.puts == [{"Name": "/p/gnmi", "Value": t["gnmi-targets"], "Type": "String", "Overwrite": True}]
      and ecs.calls == [{"cluster": "c", "service": "s", "forceNewDeployment": True}])
out, ssm, ecs = push({"/p/gnmi": "old", "/p/snmp": "old"}, [])
check("push_targets: 空の一覧は書かない（Telegraf が起動できなくなる）", not ssm.puts and not ecs.calls)
out, ssm, ecs = push({"/p/gnmi": t["gnmi-targets"], "/p/snmp": t["snmp-agents"]}, rows, force=True)
check("push_targets: force_redeploy なら変わっていなくても作り直す", not ssm.puts and len(ecs.calls) == 1)
out, ssm, ecs = push({}, rows, env=False)
check("push_targets: 書き先が渡されていなければ何もしない", out == {"skipped": True} and not ecs.calls)

# ---- nb_sync.sync（Django の cache.lock と Nautobot の読みを差し替える）
import contextlib, types
import graph
fake = types.ModuleType("django.core.cache")
fake.cache = types.SimpleNamespace(lock=lambda *a, **kw: contextlib.nullcontext())
sys.modules.update({"django": types.ModuleType("django"), "django.core": types.ModuleType("django.core"), "django.core.cache": fake})
synced = []
graph.configured = lambda: True
graph.sync_physical = lambda devices, links: synced.append((len(devices), len(links))) or {"devices": len(devices)}
toolkit.client = lambda name: {"ssm": Ssm({"/p/gnmi": "old", "/p/snmp": "old"}), "ecs": Ecs()}[name]
nb_sync.read = lambda: ([], [])
out = nb_sync.sync(log)
check("sync: 機器が 1 台も無いときは Neptune を触らない（空で合わせると物理層が全部消える）", synced == [] and out["neptune"] == {"skipped": True})
nb_sync.read = lambda: (rows, plan["cables"])
out = nb_sync.sync(log)
check("sync: 機器があれば Neptune の物理層を合わせる", synced == [(len(lab["devices"]), len(lab["links"]))])
boot = read("nautobot", "netops", "bootstrap.py")
check("bootstrap: seed が失敗したら起動時の同期を飛ばし、JobHook は起動のたびに決まった形へ戻す",
      "steps_skip.add(first_sync)" in boot and "JobHook.objects.update_or_create" in boot)

# ---- 配線
docker = read("nautobot", "Dockerfile")
up, down, chk = read("ops", "up.sh"), read("ops", "down.sh"), read("ops", "check.sh")
tf = {n: read("terraform", "pipeline", "nautobot", n) for n in os.listdir(os.path.join(ROOT, "terraform", "pipeline", "nautobot")) if n.endswith(".tf")}
nb_tf, all_tf = tf["nautobot.tf"], "\n".join(tf.values())
ver = re.search(r"^NAUTOBOT_VERSION=(\S+)", up, re.M).group(1)
redis = re.search(r"^REDIS_TAG=(\S+)", up, re.M).group(1)
check("版: ops/up.sh の NAUTOBOT_VERSION = Dockerfile の ARG", f"ARG NAUTOBOT_VERSION={ver}\n" in docker)
check("版: ops/up.sh の REDIS_TAG = terraform の redis_image_tag の既定値",
      re.search(r'variable "redis_image_tag" \{[\s\S]*?default\s*=\s*"%s"' % re.escape(redis), tf["variables.tf"]) is not None)
check("Dockerfile が COPY するものを ops/up.sh の nautobot_context が集める",
      "netops/ graph.py toolkit.py lab_seed.json /opt/nautobot/netops/" in docker and "jobs/ /opt/nautobot/jobs/" in docker
      and 'cp -R nautobot/. "$1/" && cp agent/graph.py agent/toolkit.py "$1/"' in up and 'lab/lab_topology.py lab >"$1/lab_seed.json"' in up)
check("lab_seed.json はリポジトリに置かない（毎回 lab の定義から作る）", not os.path.exists(os.path.join(ROOT, "nautobot", "netops", "lab_seed.json")))
check("Job が import するモジュールが PYTHONPATH の先にそろう", "PYTHONPATH=/opt/nautobot/netops" in docker
      and all(os.path.exists(os.path.join(ROOT, *p)) for p in (("nautobot", "netops", "nb_sync.py"), ("nautobot", "netops", "bootstrap.py"), ("agent", "graph.py"), ("agent", "toolkit.py"))))
envs = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', read("nautobot", "netops", "nb_sync.py")))
check("nb_sync が読む環境変数を Terraform がコンテナに渡す", envs and all(e in nb_tf for e in envs | {"NEPTUNE_ENDPOINT"}))
check("シークレットは SSM の SecureString から（タスク定義の secrets と RDS の write-only）。Terraform の変数に値を持たない",
      all(s in nb_tf for s in ("NAUTOBOT_SECRET_KEY", "NAUTOBOT_DB_PASSWORD", "NAUTOBOT_SUPERUSER_PASSWORD"))
      and "password_wo " in tf["database.tf"].replace("=", " =").replace("  ", " ") and 'ephemeral "aws_ssm_parameter"' in tf["database.tf"]
      and not re.search(r"sensitive\s*=\s*true", tf["variables.tf"]))
names = set(re.findall(r'ensure_secret "/\$PREFIX/nautobot/([a-z-]+)"', up))
check("ops/up.sh が作る SSM のシークレット = Terraform が読む名前", names == {"secret-key", "admin-password", "db-password"}
      and all(f'"{n}"' in all_tf for n in names))
check("SG とロールは base/core の nautobot / nautobot_db を使う", '"nautobot"' in all_tf and "nautobot_db" in all_tf)
check("Web が Nautobot の有無を読むパラメータ（<接頭辞>/nautobot/url）を Terraform が置く",
      "/nautobot/url" in nb_tf and 'toolkit.Param("NAUTOBOT_URL", "nautobot/url")' in read("web", "topology_view.py"))
order = [m.start() for m in (re.search(p, down, re.M) for p in (r"^destroy_root pipeline/analytics", r"^destroy_root pipeline/nautobot",
                                                           r"^destroy_lambda_root pipeline/graph", r"^destroy_root pipeline/stream")) for m in [m] if m]
check("ops/down.sh は nautobot を graph と stream より先に消す（state を読む相手が残っているうちに）", len(order) == 4 and order == sorted(order))
check("ops/down.sh は nautobot が消えなかったとき、その secrets を残す", 'case "$n" in "/$PREFIX/nautobot/"*)' in down and "pipeline/nautobot" in down.split("5-2.")[1])
check("ops/up.sh: stream に一覧の持ち主を渡し、nautobot は graph の seed（7-3b）の後・analytics（7-4）の前",
      '-var "dialin_targets_from_nautobot=$DIALIN_FROM_NAUTOBOT"' in up
      and up.index('log "7-3b.') < up.index("# ---- 7-3c. Nautobot") < up.index("# ---- 7-4. analytics"))
check("ops/up.sh: エンドポイントに ecs（Job が dialin を作り直す）", "pipeline/nautobot) add_endpoints ecr.api ecr.dkr logs ecs ;;" in up)
check("ops/check.sh が nautobot のルートとこのテストを回す", "pipeline/nautobot" in chk and "tests/test_nautobot.py" in chk)
check("NAUTOBOT は deploy.env のキー", "NAUTOBOT" in read("ops", "deploy-env.sh").split() and "#NAUTOBOT=1" in read("deploy.env.example"))
check("デバッグ用の EC2 は Nautobot を使わない", "nautobot" not in read("ops", "lab-debug.sh").lower() and "nautobot" not in read("cloudformation", "lab-debug.yaml").lower())

# ---- Web のゲート
import types
sys.path.insert(0, os.path.join(ROOT, "web"))
sys.modules["config"] = types.ModuleType("config")   # web/config.py は .env を読むので差し替える（topology_view が使うのは log だけ）
sys.modules["config"].log = log
import graph
import topology_view as tv
called = []
tv.refresh_topology = lambda a, b: ("fig", "info")
tv._graph_call = lambda *a, **k: (called.append(a) or ("done", "fig", "info"))
graph.configured = lambda: True
tv.NAUTOBOT_URL.value = lambda: ""
check("Web: Nautobot が無ければ今までどおり編集できる", tv.can_edit() and tv.edit_note() == "" and tv.seed_graph("a", "b")[0] == "done" and len(called) == 1)
tv.NAUTOBOT_URL.value = lambda: "http://nautobot.example:8080"
r = [tv.seed_graph("a", "b"), tv.add_link("x", "e1", "y", "e1", "l2", "", ""), tv.remove_link("x|e1|y", "a", "b")]
check("Web: Nautobot があれば編集のボタンを止め、呼ばれても Neptune に書かずに案内を返す",
      not tv.can_edit() and tv.edit_note() == tv.NAUTOBOT_MSG and len(called) == 1 and all(x[0] == tv.NAUTOBOT_MSG for x in r))
graph.configured = lambda: False
check("Web: Neptune が無いときの案内は今までどおり", "未配備" in tv.edit_note())

print(f"\n{passed} 項目すべて通過")
