# lab の SR Linux の収容回線数の代替（docs/collection.md の「収容回線数の代替」）を機器ごとに数える aggregators.starlark。
# 入力は telegraf.conf.in の 2 つめの inputs.gnmi の lab_subif_type（/interface[name=*]/subinterface[index=*]/type）と
# lab_if_oper（/interface[name=*]/oper-state）。どちらも 1 分の sample で、IF やサブ IF ごとに別々の metric で届くので、
# 機器（source タグ）ごとに覚えて period ごとに 1 つ出す:
#   circuits  tags source  fields active（type bridged のサブ IF を持つ IF の数）/ up（そのうち oper-state が up）/
#                                 capacity（物理ポート ethernet-* の数。SR Linux は未使用のポートも状態に出す）/ used_pct（active / capacity）
# 消えた IF やサブ IF（gNMI の delete は Telegraf が載せない）は、EXPIRE 回の push のあいだ届かなければ数えない。
# tests/test_stream.py が Python でこのファイルを実行して確かめるので、Python と Starlark の両方で動く書き方にする。

EXPIRE = 3

def _is_str(v):
    return type(v) == "string" or type(v) == str

def _leaf_value(metric, leaf):
    for k, v in metric.fields.items():
        if k.split("/")[-1].split(":")[-1].replace("-", "_") == leaf:
            return v
    return None

def _suffix(v):
    # identityref は "srl_nokia-interfaces:bridged" のように接頭辞付きで届くことがある
    if not _is_str(v):
        return None
    return v.split(":")[-1]

def _device(source):
    devices = state.get("devices")
    if devices == None:
        devices = {}
        state["devices"] = devices
    d = devices.get(source)
    if d == None:
        d = {"ifs": {}, "subifs": {}}
        devices[source] = d
    return d

def add(metric):
    source = metric.tags.get("source")
    name = metric.tags.get("name")
    if source == None or name == None:
        return
    tick = state.get("tick", 0)
    d = _device(source)
    if metric.name == "lab_if_oper":
        oper = _suffix(_leaf_value(metric, "oper_state"))
        if oper != None:
            d["ifs"][name] = {"up": oper == "up", "tick": tick}
    elif metric.name == "lab_subif_type":
        index = metric.tags.get("index")
        kind = _suffix(_leaf_value(metric, "type"))
        if index != None and kind != None:
            d["subifs"][name + "." + index] = {"if_name": name, "bridged": kind == "bridged", "tick": tick}

def _forget(table, tick):
    for k in list(table.keys()):
        if tick - table[k]["tick"] >= EXPIRE:
            table.pop(k)

def push():
    tick = state.get("tick", 0)
    state["tick"] = tick + 1
    out = []
    devices = state.get("devices", {})
    for source in sorted(devices.keys()):
        d = devices[source]
        _forget(d["ifs"], tick)
        _forget(d["subifs"], tick)
        if len(d["ifs"]) == 0 and len(d["subifs"]) == 0:
            devices.pop(source)
            continue
        active = {}
        for s in d["subifs"].values():
            if s["bridged"]:
                active[s["if_name"]] = True
        up = 0
        for n in active.keys():
            i = d["ifs"].get(n)
            if i != None and i["up"]:
                up += 1
        capacity = 0
        for n in d["ifs"].keys():
            if n.startswith("ethernet-"):
                capacity += 1
        m = Metric("circuits")
        m.tags["source"] = source
        m.fields["active"] = len(active)
        m.fields["up"] = up
        m.fields["capacity"] = capacity
        if capacity > 0:
            m.fields["used_pct"] = len(active) * 100.0 / capacity
        out.append(m)
    return out

def reset():
    # 覚えた IF とサブ IF は period をまたいで使うので消さない（消すのは push の EXPIRE）
    pass
