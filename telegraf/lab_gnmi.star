# lab の SR Linux から gNMI で取った値（telegraf.conf.in の 2 つめの inputs.gnmi。measurement は lab_*）を、本番と同じ共通の形に変える
# processors.starlark。共通の形（仮。本番の機種が決まるまで。docs/collection.md の「共通の形（仮）」）:
#   device_cpu     tags source / component                     fields used_pct
#   device_memory  tags source / component                     fields total_bytes / free_bytes / used_pct
#   if_stats       tags source / if_name                       fields in_octets / out_octets / in_discards / out_discards / in_errors / out_errors / speed_bps
#   sessions       tags source / kind / scope / owner          fields active / limit / warning_pct / used_pct
# 収容回線数（circuits）は IF ごとの値を機器ごとに数える必要があるので、ここではなく aggregators.starlark（lab_circuits.star）。
# 速度（lab_port_speed / lab_lag_speed）と MAC の上限（lab_*_mac_limit）は別の購読で届くので state に覚えて、次の if_stats / sessions に付ける
# （覚えた値の measurement は落とす）。変換できなかった metric は lab_* のまま返す（Kafka には載らず、デバッグ用の EC2 の標準出力でだけ見える）。
# Telegraf 1.40 の inputs.gnmi: field の名前は購読のパスからの相対パス（葉を購読すれば葉の名前）で、"-" は "_"。名前空間の接頭辞（srl_nokia-…:）が
# 付くことがあるので、最後の "/" と ":" の後ろで比べる。パスの鍵（name / index / slot）はタグ、source タグは購読先のアドレス。
# uint64 の葉は json_ietf では文字列で届くので数にする。
# tests/test_stream.py が Python でこのファイルを実行して確かめるので、Python と Starlark の両方で動く書き方にする（type の比較、try が無い）。

def _is_str(v):
    return type(v) == "string" or type(v) == str

def _is_int(v):
    return type(v) == "int" or type(v) == int

def _is_float(v):
    return type(v) == "float" or type(v) == float

def _leaf(key):
    return key.split("/")[-1].split(":")[-1].replace("-", "_")

def _num(v):
    # 数（int / float）か数の文字列なら数を、それ以外は None を返す
    if _is_int(v) or _is_float(v):
        return v
    if not _is_str(v):
        return None
    s = v.strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if s.isdigit():
        n = int(s) if len(s) <= 18 else float(s)
        return -n if neg else n
    parts = s.split(".")
    if len(parts) == 2 and (parts[0].isdigit() or parts[0] == "") and parts[1].isdigit():
        f = float(s)
        return -f if neg else f
    return None

def _count(v):
    # 数（バイト数・パケット数・エントリ数）。json_ietf の数は float で届くので int にそろえる
    n = _num(v)
    return int(n) if n != None else None

def _fields(metric):
    # 葉の名前 → 値
    out = {}
    for k, v in metric.fields.items():
        out[_leaf(k)] = v
    return out

def _tag(metric, key):
    v = metric.tags.get(key)
    if v != None:
        return v
    for k, tv in metric.tags.items():
        if k.endswith("/" + key):
            return tv
    return None

def _speed_bps(s):
    # SR Linux の port-speed（10M / 100M / 1G / 2.5G / 10G / 25G / 100G / 400G / 800G …）を bps に
    if not _is_str(s) or len(s) < 2:
        return None
    units = {"M": 1000000, "G": 1000000000, "T": 1000000000000}
    u = units.get(s[-1].upper())
    n = _num(s[:-1])
    if u == None or n == None:
        return None
    return int(n * u)

def _new(name, metric, tags):
    m = Metric(name)
    m.time = metric.time
    m.tags["source"] = metric.tags.get("source", "")
    for k, v in tags.items():
        m.tags[k] = v
    return m

def _cache(kind):
    c = state.get(kind)
    if c == None:
        c = {}
        state[kind] = c
    return c

def _pct(used, total):
    if used == None or total == None or total <= 0:
        return None
    return used * 100.0 / total

def _cpu(metric):
    # /platform/control[slot=*]/cpu[index=*]/total/instant。index は "all"（全コアの平均）とコアの番号。コアごとは落とす
    if _tag(metric, "index") != "all":
        return None
    v = _num(_fields(metric).get("instant"))
    if v == None:
        return metric
    m = _new("device_cpu", metric, {"component": _tag(metric, "slot") or ""})
    m.fields["used_pct"] = float(v)
    return m

def _memory(metric):
    # /platform/control[slot=*]/memory（physical / free / reserved / utilization）
    f = _fields(metric)
    total = _count(f.get("physical"))
    free = _count(f.get("free"))
    used_pct = _num(f.get("utilization"))
    if used_pct == None and total != None and free != None:
        used_pct = _pct(total - free, total)
    if total == None and free == None and used_pct == None:
        return metric
    m = _new("device_memory", metric, {"component": _tag(metric, "slot") or ""})
    if total != None:
        m.fields["total_bytes"] = total
    if free != None:
        m.fields["free_bytes"] = free
    if used_pct != None:
        m.fields["used_pct"] = float(used_pct)
    return m

IF_FIELDS = {
    "in_octets": "in_octets",
    "out_octets": "out_octets",
    "in_discarded_packets": "in_discards",
    "out_discarded_packets": "out_discards",
    "in_error_packets": "in_errors",
    "out_error_packets": "out_errors",
}

def _if_counters(metric):
    # /interface[name=*]/statistics
    name = _tag(metric, "name")
    if name == None:
        return metric
    f = _fields(metric)
    m = _new("if_stats", metric, {"if_name": name})
    got = False
    for src, dst in IF_FIELDS.items():
        v = _count(f.get(src))
        if v != None:
            m.fields[dst] = v
            got = True
    # Telegraf の Metric の fields は len を持たない（Telegraf で実測）ので、数えずに印を立てる
    if not got:
        return metric
    speed = _cache("speed").get(m.tags["source"] + "|" + name)
    if speed != None:
        m.fields["speed_bps"] = speed
    return m

def _remember_speed(metric):
    # /interface[name=*]/ethernet/port-speed（enum）と /interface[name=*]/lag/lag-speed（Mbps）
    name = _tag(metric, "name")
    f = _fields(metric)
    if metric.name == "lab_port_speed":
        bps = _speed_bps(f.get("port_speed"))
    else:
        mbps = _num(f.get("lag_speed"))
        bps = int(mbps * 1000000) if mbps != None else None
    if name == None or bps == None:
        return metric
    _cache("speed")[metric.tags.get("source", "") + "|" + name] = bps
    return None

def _owner(metric, scope):
    name = _tag(metric, "name")
    if name == None:
        return None
    if scope == "subinterface":
        index = _tag(metric, "index")
        if index == None:
            return None
        return name + "." + index
    return name

def _scope(metric):
    return "subinterface" if metric.name.startswith("lab_subif_") else "network_instance"

def _remember_limit(metric):
    # …/bridge-table/mac-limit（maximum-entries / warning-threshold-pct）
    scope = _scope(metric)
    owner = _owner(metric, scope)
    f = _fields(metric)
    limit = _count(f.get("maximum_entries"))
    warn = _count(f.get("warning_threshold_pct"))
    if owner == None or (limit == None and warn == None):
        return metric
    _cache("mac_limit")[metric.tags.get("source", "") + "|" + scope + "|" + owner] = {"limit": limit, "warning_pct": warn}
    return None

def _mac_active(metric):
    # …/bridge-table/statistics/active-entries
    scope = _scope(metric)
    owner = _owner(metric, scope)
    active = _count(_fields(metric).get("active_entries"))
    if owner == None or active == None:
        return metric
    m = _new("sessions", metric, {"kind": "mac", "scope": scope, "owner": owner})
    m.fields["active"] = active
    lim = _cache("mac_limit").get(m.tags["source"] + "|" + scope + "|" + owner)
    if lim != None:
        if lim["limit"] != None:
            m.fields["limit"] = lim["limit"]
            pct = _pct(active, lim["limit"])
            if pct != None:
                m.fields["used_pct"] = pct
        if lim["warning_pct"] != None:
            m.fields["warning_pct"] = lim["warning_pct"]
    return m

def apply(metric):
    n = metric.name
    if n == "lab_cpu":
        return _cpu(metric)
    if n == "lab_memory":
        return _memory(metric)
    if n == "lab_if_counters":
        return _if_counters(metric)
    if n == "lab_port_speed" or n == "lab_lag_speed":
        return _remember_speed(metric)
    if n == "lab_ni_mac_limit" or n == "lab_subif_mac_limit":
        return _remember_limit(metric)
    if n == "lab_ni_mac_active" or n == "lab_subif_mac_active":
        return _mac_active(metric)
    return metric
