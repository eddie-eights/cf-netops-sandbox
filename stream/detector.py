"""detector Lambda。MSK の metrics / traps トピックを読み、異常を DynamoDB に書く。

stream.yaml の ZipFile と同じ内容（tests/test_stream.py が一致を確かめる。4096 文字まで）。
記録: ifOperStatus が down のインタフェース（ポーリング）と linkDown トラップ（即時）。
解消: up に戻ったポーリング、linkUp トラップ。キーは <機器>#<種別>#<インタフェース>。
機器名は sysName タグ > DEVICE_MAP（IP=機器名,...）の順で引く。
"""
import base64, json, os, time
import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE"])
DEVMAP = dict(p.split("=", 1) for p in os.environ.get("DEVICE_MAP", "").split(",") if "=" in p)
LINK_DOWN, LINK_UP = ".1.3.6.1.6.3.1.1.5.3", ".1.3.6.1.6.3.1.1.5.4"


def device(m):
    t = m.get("tags", {})
    return t.get("sysName") or DEVMAP.get(t.get("agent_host") or t.get("source", ""), t.get("agent_host") or t.get("source", "?"))


def events(m):
    """1 メトリクスから (機器, 種別, インタフェース, 開く/閉じる, 元) を出す"""
    name, f, t = m.get("name"), m.get("fields", {}), m.get("tags", {})
    if name == "interface" and "ifOperStatus" in f:
        ifn = t.get("ifDescr") or t.get("ifIndex") or "?"
        if ifn.startswith("lo"):
            return []
        return [(device(m), "link_down", ifn, int(f["ifOperStatus"]) == 2, "poll")]
    if name == "snmp_trap":
        oid = t.get("oid", "")
        if oid in (LINK_DOWN, LINK_UP):
            # MIB が無いと varbind の名前は数値 OID（末尾に ifIndex が付く）。ifDescr を優先し、無ければ ifIndex
            ifn = "?"
            for pre in ("ifDescr", ".1.3.6.1.2.1.2.2.1.2", "ifIndex", ".1.3.6.1.2.1.2.2.1.1"):
                v = [v for k, v in f.items() if k == pre or k.startswith(pre + ".")]
                if v:
                    ifn = str(v[0])
                    break
            return [(device(m), "link_down", ifn, oid == LINK_DOWN, "trap")]
        return [(device(m), "trap", oid, True, "trap")]
    return []


def handler(event, _ctx):
    now = int(time.time())
    n = 0
    for recs in event.get("records", {}).values():
        for r in recs:
            try:
                m = json.loads(base64.b64decode(r["value"]))
            except (ValueError, KeyError):
                continue
            for dev, kind, ifn, opened, src in events(m if isinstance(m, dict) else {}):
                key = f"{dev}#{kind}#{ifn}"
                if opened:
                    TABLE.update_item(
                        Key={"anomaly_id": key},
                        UpdateExpression="SET device_id=:d, kind=:k, target=:i, #s=:o, last_seen=:n, #src=:p, first_seen=if_not_exists(first_seen,:n), detail=:t",
                        ExpressionAttributeNames={"#s": "status", "#src": "source"},
                        ExpressionAttributeValues={":d": dev, ":k": kind, ":i": ifn, ":o": "open", ":n": now, ":p": src,
                                                   ":t": f"{ifn} is down ({src})" if kind == "link_down" else f"trap {ifn}"},
                    )
                else:
                    try:
                        TABLE.update_item(
                            Key={"anomaly_id": key}, ConditionExpression="#s = :o",
                            UpdateExpression="SET #s=:r, resolved_at=:n, last_seen=:n",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":o": "open", ":r": "resolved", ":n": now},
                        )
                    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
                        continue  # 開いていない異常の up は何もしない（正常時のポーリングは毎回ここ）
                n += 1
    return {"processed": n}
