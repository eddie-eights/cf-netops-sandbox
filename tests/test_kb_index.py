"""agent/kb_index.py（KB のベクトル索引を VPC の中で作る Lambda）の模擬テスト。boto3 / botocore と _call を差し替え、AWS に触れない。
実行は python3 tests/test_kb_index.py（依存は無い）。"""
import importlib.util, os, sys, types

PATH = os.path.join(os.path.dirname(__file__), "..", "agent", "kb_index.py")

# boto3 / botocore は Lambda の実行環境にあるが、手元に無くても読めるように差し替える
boto3 = types.ModuleType("boto3")
botocore = types.ModuleType("botocore")
auth = types.ModuleType("botocore.auth"); auth.SigV4Auth = object
awsreq = types.ModuleType("botocore.awsrequest"); awsreq.AWSRequest = object
sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.auth": auth, "botocore.awsrequest": awsreq})
spec = importlib.util.spec_from_file_location("kb_index", PATH)
kb = importlib.util.module_from_spec(spec); spec.loader.exec_module(kb)

passed = 0
def check(name, cond):
    global passed
    if not cond:
        print(f"NG: {name}"); sys.exit(1)
    passed += 1; print(f"OK: {name}")

class Ctx:
    def __init__(self, ms): self.ms = ms
    def get_remaining_time_in_millis(self): return self.ms

def run(responses, ms=300_000):
    """_call の返りを順に responses から出し、呼ばれた (method, url, body) と sleep の回数を返す"""
    calls, sleeps = [], []
    def fake(method, url, body=None):
        calls.append((method, url, body))
        return responses.pop(0)
    kb._call = fake
    ev = {"endpoint": "https://abc.ap-northeast-1.aoss.amazonaws.com/", "index": "kb-index", "body": {"mappings": {}}}
    try:
        r = kb.handler(ev, Ctx(ms), sleep=sleeps.append)
    except RuntimeError as e:
        r = e
    return r, calls, sleeps

r, calls, _ = run([(200, "")])
check("索引がもうあれば PUT しない（created False）", r == {"index": "kb-index", "created": False} and [c[0] for c in calls] == ["HEAD"]
      and calls[0][1] == "https://abc.ap-northeast-1.aoss.amazonaws.com/kb-index")
r, calls, _ = run([(404, ""), (200, "{}")])
check("無ければ body を PUT して created True", r["created"] is True and calls[1] == ("PUT", calls[0][1], {"mappings": {}}))
r, calls, sleeps = run([(403, "denied"), (0, "timed out"), (404, ""), (200, "{}")])
check("403（ポリシーの反映待ち）と 0（届かない）は 10 秒おきに打ち直す", r["created"] is True and sleeps == [10, 10] and len(calls) == 4)
r, calls, _ = run([(404, ""), (400, '{"error":{"type":"resource_already_exists_exception"}}')])
check("PUT が resource_already_exists_exception なら作られたものとして返す", r == {"index": "kb-index", "created": False})
r, _, sleeps = run([(404, ""), (400, '{"error":{"type":"mapper_parsing_exception"}}')])
check("それ以外の 400 は打ち直さずに落とす", isinstance(r, RuntimeError) and "400" in str(r) and sleeps == [])
r, _, sleeps = run([(500, "boom")])
check("HEAD の 5xx は落とす", isinstance(r, RuntimeError) and sleeps == [])
r, _, sleeps = run([(403, "denied")], ms=44_999)
check("残り時間が次の 1 回分（45 秒）を切ったら 403 でも落とす", isinstance(r, RuntimeError) and "403" in str(r) and sleeps == [])
print(f"通過 {passed} / 失敗 0")
