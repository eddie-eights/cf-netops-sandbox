"""デバッグ用の EC2（cloudformation/lab-debug.yaml。lab + Telegraf を 1 台）が terraform/pipeline/lab と stream の Telegraf からずれていないかを見る。
- 独立: スタックが VPC・エンドポイント・バケット・ECR を持ち、ops/up.sh / ops/down.sh は触らない（2026-10-04 ユーザー決定。ops/lab-debug.sh だけで扱う）
- 版: CloudFormation のパラメータの既定値 = terraform/pipeline/lab の変数の既定値 = ops/lab-common.sh（ops/up.sh と ops/lab-debug.sh が source する）
- EC2 の中: どちらの user_data も env を書いて S3 の lab/ を置き直し、lab/setup.sh を exec するだけ（TELEGRAF_IMAGE の値だけが違う）
- ロール: terraform/pipeline/lab/iam.tf と同じ Sid と Action（ECR は Telegraf のリポジトリも読む）
- Telegraf: 同じ telegraf.conf.in を SINK=stdout で描く。入力は MSK 向けと同じで、出力だけが標準出力になる（telegraf/telegraf.sh render を手元で回す）
実行は uv run python tests/test_lab_debug.py（pyyaml を使う。AWS も docker も要らない）。"""
import os, re, shlex, subprocess, sys, tempfile, tomllib

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()

# CloudFormation の短縮形（!Sub / !Ref / !If ...）を {"Fn::Sub": ...} の形で読む
class CfnLoader(yaml.SafeLoader):
    pass
def _cfn(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else ("Fn::GetAtt" if suffix == "GetAtt" else "Fn::" + suffix)
    if isinstance(node, yaml.ScalarNode):
        v = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        v = loader.construct_sequence(node, deep=True)
    else:
        v = loader.construct_mapping(node, deep=True)
    return {name: v}
CfnLoader.add_multi_constructor("!", _cfn)

cfn = yaml.load(read("cloudformation", "lab-debug.yaml"), Loader=CfnLoader)
params = cfn["Parameters"]
res = cfn["Resources"]
common = read("ops", "lab-common.sh")
lab_vars = read("terraform", "pipeline", "lab", "variables.tf")
iam_tf = read("terraform", "pipeline", "lab", "iam.tf")
tftpl = read("terraform", "pipeline", "lab", "templates", "lab_user_data.sh.tftpl")
setup = read("lab", "setup.sh")
lab_sh = read("lab", "lab.sh")
tg_sh = read("telegraf", "telegraf.sh")
up = read("ops", "up.sh")
down = read("ops", "down.sh")
dbg = read("ops", "lab-debug.sh")

def tf_default(name):
    m = re.search(r'variable "%s" \{[\s\S]*?default\s*=\s*("?)([^"\n]*)\1\n' % name, lab_vars)
    return m.group(2) if m else None
def sh_const(src, name):
    m = re.search(r"^%s=(\S+)" % name, src, re.M)
    return m.group(1).strip('"') if m else None

# ---- 版と既定値
for cfn_name, tf_name, sh_name in [("ContainerlabVersion", "containerlab_version", "CONTAINERLAB_VERSION"),
                                    ("SrlinuxImageTag", "srlinux_image_tag", "SRLINUX_TAG"),
                                    ("MultitoolImageTag", "multitool_image_tag", "MULTITOOL_TAG")]:
    check(f"{cfn_name} の既定値 = terraform/pipeline/lab の {tf_name} = ops/lab-common.sh の {sh_name}",
          str(params[cfn_name]["Default"]) == tf_default(tf_name) == sh_const(common, sh_name) is not None)
check("InstanceType / VolumeSize / ImageId / AutoStartLab の既定値は terraform/pipeline/lab と同じ",
      params["InstanceType"]["Default"] == tf_default("instance_type")
      and str(params["VolumeSize"]["Default"]) == tf_default("volume_size")
      and params["ImageId"]["Default"] == tf_default("ami_ssm_parameter")
      and params["AutoStartLab"]["Default"] == tf_default("auto_start_lab"))
check("InstanceType の選べる値は terraform の検査と同じ（arm64 の t4g だけ）",
      params["InstanceType"]["AllowedValues"] == re.findall(r'"(t4g\.\w+)"', re.search(r'contains\(\[([^\]]*)\], var\.instance_type\)', lab_vars).group(1)))
check("ops/lab-common.sh の TELEGRAF_VERSION = telegraf/Dockerfile の ARG の既定値",
      sh_const(common, "TELEGRAF_VERSION") == re.search(r"^ARG TELEGRAF_VERSION=(\S+)", read("telegraf", "Dockerfile"), re.M).group(1))
check("ops/up.sh と ops/lab-debug.sh は ops/lab-common.sh を source し、lab の版を自分では持たない",
      '. "$(dirname "$0")/lab-common.sh"' in up and '. "$(dirname "$0")/lab-common.sh"' in dbg
      and not any(re.search(r"^%s=" % k, s, re.M) for k in ("SRLINUX_TAG", "MULTITOOL_TAG", "CONTAINERLAB_VERSION", "TELEGRAF_VERSION", "CONTAINERLAB_RPM") for s in (up, dbg))
      and not any(re.search(r"^(ecr_has|fetch|dir_tag)\(\)", s, re.M) for s in (up, dbg)))
check("イメージの作り方（ミラー・Telegraf のビルド・lab/ の置き方）は lab-common.sh の関数を両方が呼ぶ",
      all(f in up for f in ("mirror_lab_images ", "build_telegraf ", "upload_lab ", "TELEGRAF_TAG=$(telegraf_tag)"))
      and all(f in dbg for f in ("mirror_lab_images ", "build_telegraf ", "upload_lab ", "TELEGRAF_TAG=$(telegraf_tag)"))
      and "docker pull" not in up.split("# ---- 2. イメージ")[1].split("# ---- 3.")[0].split("NEED_TEMPORAL")[0]
      and "ghcr.io/nokia/srlinux" not in up and "ghcr.io/nokia/srlinux" not in dbg)
_pass = re.findall(r'"(\w+)=\$', dbg.split("--parameter-overrides")[1].split("--tags")[0])
check("lab-debug.sh は既定値の無いパラメータと版を全部渡す（版は lab-common.sh から）",
      {k for k, v in params.items() if "Default" not in v} <= set(_pass)
      and {"ContainerlabVersion", "SrlinuxImageTag", "MultitoolImageTag", "TelegrafImageTag", "CreateInstance", "NetworkPerimeter"} <= set(_pass)
      and set(_pass) <= set(params))

# ---- ロール（iam.tf と同じ Sid と Action）
role = res["Role"]["Properties"]
stmts = {s["Sid"]: s for s in role["Policies"][0]["PolicyDocument"]["Statement"]}
tf_sids = re.findall(r'Sid\s*=\s*"(\w+)"', iam_tf)
def tf_actions(sid):
    block = iam_tf.split(f'Sid      = "{sid}"' if f'Sid      = "{sid}"' in iam_tf else f'Sid    = "{sid}"')[1].split("},")[0]
    return sorted(re.findall(r'"((?:ecr|s3|ssm):\w+)"', re.search(r"Action\s*=\s*(.*)", block).group(1)))
def as_list(v):
    return sorted(v if isinstance(v, list) else [v])
check("ロールの Sid は iam.tf と同じ（TelegrafAddress は stream の NLB を読む forward 用なので、デバッグ用の EC2 には要らない）",
      set(stmts) == set(tf_sids) - {"TelegrafAddress"} and "ssm:GetParameter" not in str(stmts))
check("Sid ごとの Action は iam.tf と同じ", all(as_list(stmts[s]["Action"]) == tf_actions(s) for s in stmts))
repos = {k: v["Properties"] for k, v in res.items() if v["Type"] == "AWS::ECR::Repository"}
check("ECR はこのスタックのリポジトリ 3 つ（lab の 2 つと Telegraf）だけを読む",
      stmts["EcrPull"]["Resource"] == [{"Fn::GetAtt": f"{k}.Arn"} for k in ("SrlinuxRepository", "MultitoolRepository", "TelegrafRepository")])
check("S3 は このスタックのバケットの lab/ だけ（GetObject と prefix 付きの ListBucket）",
      stmts["S3Read"]["Resource"] == {"Fn::Sub": "${Bucket.Arn}/lab/*"} and stmts["S3List"]["Resource"] == {"Fn::GetAtt": "Bucket.Arn"}
      and stmts["S3List"]["Condition"] == {"StringLike": {"s3:prefix": "lab/*"}})
_perim = role["Policies"][1]["Fn::If"]
_deny = _perim[1]["PolicyDocument"]["Statement"][0]
_tf_perim = read("terraform", "base", "core", "perimeter.tf")
check("SSM のマネージドポリシーと、NetworkPerimeter のときだけ境界の Deny（perimeter.tf と同じ Action と条件。SourceVpc はこのスタックの VPC）",
      role["ManagedPolicyArns"] == [{"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"}]
      and _perim[0] == "HasPerimeter" and _perim[2] == {"Ref": "AWS::NoValue"}
      and _deny["Effect"] == "Deny" and _deny["Resource"] == "*"
      and _deny["Action"] == re.findall(r'"([\w-]+:[\w*]+)"', _tf_perim.split("perimeter_denied_actions = [")[1].split("]")[0])
      and _deny["Condition"] == {"StringNotEqualsIfExists": {"aws:SourceVpc": {"Ref": "Vpc"}, "aws:CalledViaLast": "s3tables.amazonaws.com"},
                                 "BoolIfExists": {"aws:ViaAWSService": "false"}}
      and '"aws:SourceVpc" = aws_vpc.this.id, "aws:CalledViaLast" = "s3tables.amazonaws.com"' in _tf_perim
      and 'BoolIfExists            = { "aws:ViaAWSService" = "false" }' in _tf_perim
      and "AmazonSSMManagedInstanceCore" in iam_tf)

# ---- このスタックだけで閉じる（土台 terraform/base/core を使わない）
check("パラメータに土台から受け取るもの（サブネット・SG・バケット・境界ポリシー）が無い",
      not {"SubnetId", "SecurityGroupId", "BucketName", "PerimeterPolicyArn"} & set(params)
      and not re.search(r"ImportValue|base/core の output", read("cloudformation", "lab-debug.yaml")))
check("VPC はインターネットへの経路を持たない（IGW / NAT / 0.0.0.0/0 の経路が無く、サブネットはパブリック IP を付けない）",
      not any(v["Type"] in ("AWS::EC2::InternetGateway", "AWS::EC2::NatGateway", "AWS::EC2::Route", "AWS::EC2::EgressOnlyInternetGateway") for v in res.values())
      and res["Subnet"]["Properties"]["MapPublicIpOnLaunch"] is False
      and res["Vpc"]["Properties"]["EnableDnsSupport"] is True and res["Vpc"]["Properties"]["EnableDnsHostnames"] is True)
_eps = {v["Properties"]["ServiceName"]["Fn::Sub"].rsplit(".", 1)[-1] if not v["Properties"]["ServiceName"]["Fn::Sub"].endswith(("ecr.api", "ecr.dkr"))
        else v["Properties"]["ServiceName"]["Fn::Sub"].split("${AWS::Region}.")[1]: v["Properties"]
        for v in res.values() if v["Type"] == "AWS::EC2::VPCEndpoint"}
check("エンドポイントは S3（gateway）と SSM で入る 2 本と ECR の 2 本だけ",
      set(_eps) == {"s3", "ssm", "ssmmessages", "ecr.api", "ecr.dkr"} and _eps["s3"]["VpcEndpointType"] == "Gateway"
      and "PolicyDocument" not in _eps["s3"])
check("インターフェース型はどれも private DNS を有効にし、ポリシーは endpoints.tf と同じ「このアカウントのプリンシパルだけ」",
      all(_eps[k]["VpcEndpointType"] == "Interface" and _eps[k]["PrivateDnsEnabled"] is True
          and _eps[k]["SecurityGroupIds"] == [{"Ref": "EndpointSecurityGroup"}]
          and _eps[k]["PolicyDocument"]["Statement"] == [{"Sid": "OwnAccountOnly", "Effect": "Allow", "Principal": "*", "Action": "*", "Resource": "*",
                                                          "Condition": {"StringEquals": {"aws:PrincipalAccount": {"Ref": "AWS::AccountId"}}}}]
          for k in ("ssm", "ssmmessages", "ecr.api", "ecr.dkr")))
_isg, _esg = res["InstanceSecurityGroup"]["Properties"], res["EndpointSecurityGroup"]["Properties"]
check("EC2 の SG は受信無し・送信 443 だけ。エンドポイントの SG は EC2 の SG からの 443 だけ受ける",
      "SecurityGroupIngress" not in _isg and [(r["IpProtocol"], r["FromPort"], r["ToPort"]) for r in _isg["SecurityGroupEgress"]] == [("tcp", 443, 443)]
      and [(r["FromPort"], r["SourceSecurityGroupId"]) for r in _esg["SecurityGroupIngress"]] == [(443, {"Ref": "InstanceSecurityGroup"})])
check("ECR のリポジトリはタグを上書きできず、スタックを消すとイメージごと消える（base/ecr の <接頭辞>-lab-* と別の名前）",
      {k: r["RepositoryName"]["Fn::Sub"] for k, r in repos.items()}
      == {"SrlinuxRepository": "${NamePrefix}-debug-lab-srlinux", "MultitoolRepository": "${NamePrefix}-debug-lab-multitool",
          "TelegrafRepository": "${NamePrefix}-debug-telegraf"}
      and all(r["ImageTagMutability"] == "IMMUTABLE" and r["EmptyOnDelete"] is True and r["ImageScanningConfiguration"] == {"ScanOnPush": True}
              for r in repos.values()))
_bkt = res["Bucket"]["Properties"]
check("バケットは <接頭辞>-lab-debug-<アカウント>で、公開を全部止めて暗号化し、暗号化されていない経路を拒む",
      _bkt["BucketName"] == {"Fn::Sub": "${NamePrefix}-lab-debug-${AWS::AccountId}"}
      and all(_bkt["PublicAccessBlockConfiguration"].values()) and len(_bkt["PublicAccessBlockConfiguration"]) == 4
      and res["BucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]["Sid"] == "DenyInsecureTransport")
check("lab-debug.sh のバケットとリポジトリの名前はテンプレートと同じ作り方（down は Outputs を読まずに空にできる）",
      'BUCKET="$PREFIX-lab-debug-$ACCOUNT_ID"' in dbg and 'REPO_PREFIX="$PREFIX-debug"' in dbg
      and cfn["Outputs"]["RepositoryPrefix"]["Value"]["Fn::Sub"].endswith("/${NamePrefix}-debug")
      and 'mirror_lab_images "$REG" "$REPO_PREFIX"' in dbg and 'build_telegraf "$REG/$REPO_PREFIX-telegraf:$TELEGRAF_TAG"' in dbg)
check("EC2 は CreateInstance=true のときだけ作り、そのときは TelegrafImageTag が要る（Rules）。エンドポイントができてから起こす",
      res["Instance"]["Condition"] == "HasInstance" and cfn["Conditions"]["HasInstance"] == {"Fn::Equals": [{"Ref": "CreateInstance"}, "true"]}
      and cfn["Rules"]["TelegrafTagWithInstance"]["Assertions"][0]["Assert"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "TelegrafImageTag"}, ""]}]}
      and {"SsmEndpoint", "SsmMessagesEndpoint", "EcrApiEndpoint", "EcrDkrEndpoint", "S3Endpoint", "SubnetRouteTable"} <= set(res["Instance"]["DependsOn"])
      and all(cfn["Outputs"][k]["Condition"] == "HasInstance" for k in ("InstanceId", "StartSessionCommand")))
_up_case = dbg.split("\n  up)\n")[1]
check("lab-debug.sh up は初回に EC2 の無い器を作り、イメージと lab/ を置いてから EC2 を作る",
      _up_case.index("deploy false") < _up_case.index("mirror_lab_images") < _up_case.index('upload_lab "$BUCKET"') < _up_case.index('deploy true "$TELEGRAF_TAG"'))
_down_case = dbg.split("\n  down)\n")[1].split(";;")[0]
check("lab-debug.sh down はバケットを空にしてからスタックを消し、消えるのを待つ",
      _down_case.index('aws s3 rm --only-show-errors --recursive "s3://$BUCKET"') < _down_case.index("delete-stack")
      < _down_case.index("wait stack-delete-complete"))

# ---- EC2（instance.tf と同じ守り）
inst = res["Instance"]["Properties"]
check("IMDSv2 必須・hop limit 1（コンテナから IMDS に届かせない）。instance.tf と同じ",
      inst["MetadataOptions"] == {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 1}
      and 'http_tokens   = "required"' in read("terraform", "pipeline", "lab", "instance.tf")
      and "http_put_response_hop_limit = 1" in read("terraform", "pipeline", "lab", "instance.tf"))
ebs = inst["BlockDeviceMappings"][0]["Ebs"]
check("ルートは gp3・暗号化・EC2 と一緒に消える。パブリック IP は付けない",
      ebs["VolumeType"] == "gp3" and ebs["Encrypted"] is True and ebs["DeleteOnTermination"] is True
      and "NetworkInterfaces" not in inst and "AssociatePublicIpAddress" not in str(inst))
check("Project と owner のタグ（Terraform の default_tags と同じ）を EC2 とロールに付ける",
      all({t["Key"] for t in res[r]["Properties"]["Tags"]} >= {"Name", "Project", "owner"} for r in ("Instance", "Role")))

# ---- UserData（tftpl と同じ形）
ud = inst["UserData"]["Fn::Base64"]["Fn::Sub"]
TF2CFN = {"name_prefix": "NamePrefix", "region": "AWS::Region", "account_id": "AWS::AccountId", "bucket": "Bucket",
          "containerlab_version": "ContainerlabVersion", "srlinux_image_tag": "SrlinuxImageTag",
          "multitool_image_tag": "MultitoolImageTag", "auto_start_lab": "AutoStartLab"}
def body(s):  # コメントと、2 つで違ってよい行（TELEGRAF_IMAGE と、案内のコマンド名）を外し、リポジトリの名前（-debug- が付く）をそろえる
    s = re.sub(r"\$\{(\w+)\}", lambda m: "${" + TF2CFN.get(m.group(1), m.group(1)) + "}", s).replace("${NamePrefix}-debug-lab-", "${NamePrefix}-lab-")
    return [l for l in s.strip().splitlines()
            if not (l.startswith("# ") or l.startswith("TELEGRAF_IMAGE=") or "is not in s3://" in l)]
check("UserData は terraform の user_data（tftpl）と、変数の置き換えとリポジトリの名前と TELEGRAF_IMAGE の行のほかは同じ",
      body(tftpl) == body(ud) and "/${NamePrefix}-debug-lab-srlinux:${SrlinuxImageTag}\n" in ud and "/${NamePrefix}-debug-lab-multitool:${MultitoolImageTag}\n" in ud)
check("UserData の ${…} はパラメータか疑似パラメータかこのスタックのバケットだけ（シェルの変数は $LAB と書く）",
      all(v in params or v.startswith("AWS::") or v == "Bucket" for v in re.findall(r"\$\{([\w:]+)\}", ud)))
check("TELEGRAF_IMAGE は lab の EC2 では空、デバッグ用の EC2 ではこのスタックのリポジトリ（<接頭辞>-debug-telegraf。stream と同じ作り方のイメージ）",
      "\nTELEGRAF_IMAGE=\n" in tftpl and "\nTELEGRAF_IMAGE=${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/${NamePrefix}-debug-telegraf:${TelegrafImageTag}\n" in ud)
env_keys = re.findall(r"^(\w+)=", tftpl.split("<<'__ENV__'")[1].split("__ENV__")[0], re.M)
check("env のキーは 2 つの user_data と setup.sh の頭の一覧で同じ",
      env_keys == re.findall(r"^(\w+)=", ud.split("<<'__ENV__'")[1].split("__ENV__")[0], re.M)
      and env_keys == [k.strip() for k in re.search(r"^# env のキー: (.*)$", setup, re.M).group(1).split("/")])
check("どちらも起動のたびに流し（cloud-config の always）、lab/ を置き直して setup.sh を exec する",
      all("- [scripts-user, always]" in s and "exec bash $LAB/src/setup.sh" in s and "aws s3 sync --delete" in s for s in (tftpl, ud)))
check("lab/ の置き場は upload_lab の宛先と同じ（s3://<バケット>/lab/）",
      'aws s3 sync --only-show-errors lab/ "s3://$1/lab/"' in common and "s3://${bucket}/lab/ $LAB/src/" in tftpl and "s3://${Bucket}/lab/ $LAB/src/" in ud)
check("setup.sh は TELEGRAF_IMAGE があるときだけ Telegraf のユニットを作り、無ければ消す（lab の EC2 には残さない）",
      re.search(r'if \[ -n "\$\{TELEGRAF_IMAGE:-\}" \]; then\n\s*cat > "\$TG_UNIT"[\s\S]*?ExecStart=\$SRC/lab\.sh telegraf run[\s\S]*?else\n\s*systemctl disable --now[\s\S]*?rm -f "\$TG_UNIT"', setup) is not None)
check("setup.sh は lab のユニットを tftpl の前の版と同じ中身で作る（lab.sh up / down、20 分待つ）",
      "ExecStart=$SRC/lab.sh up" in setup and "ExecStop=$SRC/lab.sh down" in setup and "TimeoutStartSec=1200" in setup)

# ---- Telegraf: 同じ telegraf.conf.in を SINK で描き分ける
def render(sink, **extra):
    with tempfile.TemporaryDirectory() as d:
        env = {"PATH": os.environ["PATH"], "TELEGRAF_TEMPLATE": os.path.join(ROOT, "telegraf", "telegraf.conf.in"),
               "TELEGRAF_CONF": os.path.join(d, "telegraf.conf"), "AWS_REGION": "ap-northeast-1",
               "SNMP_AGENTS": '"udp://203.0.113.11:161", "udp://203.0.113.12:161"', "GNMI_TARGETS": '"203.0.113.11:57400"',
               "GNMI_USERNAME": "u", "GNMI_PASSWORD": "p", "SNMP_COMMUNITY": "c", **({"SINK": sink} if sink else {}), **extra}
        r = subprocess.run(["bash", os.path.join(ROOT, "telegraf", "telegraf.sh"), "render"], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return None, r.stderr, os.listdir(d)
        with open(env["TELEGRAF_CONF"], "rb") as f:
            return tomllib.load(f), r.stdout, os.listdir(d)
kafka, _, kafka_files = render(None, KAFKA_BROKERS="b-1.example:9098,b-2.example:9098")
stdout_conf, out, stdout_files = render("stdout")
check("SINK の既定は kafka（MSK）。outputs.kafka が 5 つ（metrics / traps / gnmi / logs / mdt）で outputs.file は無い", kafka is not None
      and len(kafka["outputs"]["kafka"]) == 5 and "file" not in kafka["outputs"] and "aws_config" in kafka_files)
check("SINK=stdout は KAFKA_BROKERS が無くても描け、outputs.kafka が無く outputs.file（stdout / json）だけ。aws_config も書かない",
      stdout_conf is not None and "kafka" not in stdout_conf["outputs"]
      and stdout_conf["outputs"]["file"] == [{"files": ["stdout"], "data_format": "json", "json_timestamp_units": "1s"}]
      and "aws_config" not in stdout_files and "sink: stdout" in out)
check("入力（SNMP / trap / syslog / gNMI ...）と agent と health は kafka と stdout で同じ",
      stdout_conf["inputs"] == kafka["inputs"] and stdout_conf["agent"] == kafka["agent"]
      and stdout_conf["outputs"]["health"] == kafka["outputs"]["health"])
check("stdout の json は MSK に載るもの（outputs.kafka）と同じ形（秒の timestamp）",
      all(o.get("data_format") == "json" and o.get("json_timestamp_units") == "1s" for o in kafka["outputs"]["kafka"]))
check("知らない SINK と、kafka で KAFKA_BROKERS が無いのは描かずに止まる",
      render("s3")[0] is None and render("kafka")[0] is None)
_rfc5424, out5424, _ = render("stdout", SYSLOG_STANDARD="RFC5424")
check("syslog の形式の既定は RFC3164（本番の Cisco IOS）。SYSLOG_STANDARD=RFC5424 で lab の SR Linux 向けになり、ほかの値は描かずに止まる",
      kafka["inputs"]["syslog"][0]["syslog_standard"] == "RFC3164" and stdout_conf["inputs"]["syslog"][0]["syslog_standard"] == "RFC3164"
      and _rfc5424 is not None and _rfc5424["inputs"]["syslog"][0]["syslog_standard"] == "RFC5424" and "RFC5424" in out5424
      and render("stdout", SYSLOG_STANDARD="rfc3164")[0] is None and render("stdout", SYSLOG_STANDARD="")[0] is not None)
tpl = read("telegraf", "telegraf.conf.in")
check("telegraf.conf.in の出力の区間は telegraf.sh の SINKS と同じ名前で、開きと閉じが対になる",
      sorted(re.findall(r"^# >>> sink (\w+)", tpl, re.M)) == sorted(re.findall(r"^# <<< sink (\w+)", tpl, re.M))
      == sorted(re.search(r'^SINKS="([^"]*)"', tg_sh, re.M).group(1).split()))
# SNMP のポーリングは既定で止める（trap だけ。2026-10-04 ユーザー決定）。SNMP_POLL=1 で inputs.snmp を残す
_poll, out_poll, _ = render("stdout", SNMP_POLL="1")
_snmp_blk = tpl.split("# >>> snmp_poll", 1)[1].split("# <<< snmp_poll", 1)[0] if "# >>> snmp_poll" in tpl else ""
check("telegraf.conf.in の inputs.snmp（ポーリング）は「>>> snmp_poll」〜「<<< snmp_poll」の 1 区間に丸ごと入り、trap / gNMI / syslog は外にある",
      tpl.count("# >>> snmp_poll") == 1 and tpl.count("# <<< snmp_poll") == 1 and "[[inputs.snmp]]" in _snmp_blk and "agents = [__SNMP_AGENTS__]" in _snmp_blk
      and tpl.count("[[inputs.snmp]]") == 1 and tpl.count("agents = [__SNMP_AGENTS__]") == 1
      and not any(w in _snmp_blk for w in ("[[inputs.snmp_trap]]", "[[inputs.gnmi]]", "[[inputs.syslog]]", "[[outputs.")))
check("SNMP_POLL の既定は 0: kafka でも stdout でも inputs.snmp が無く、trap / gNMI / syslog は残る（ログは snmp poll: off）",
      sh_const(tg_sh, "SNMP_POLL") == "${SNMP_POLL:-0}"
      and all("snmp" not in c["inputs"] and len(c["inputs"]["snmp_trap"]) == 1 and len(c["inputs"]["gnmi"]) == 2 and len(c["inputs"]["syslog"]) == 1
              for c in (kafka, stdout_conf))
      and "snmp poll: off" in out)
check("gNMI は 2 つ（状態と lab の性能メトリクス）で、どちらも GNMI_TARGETS を宛先にする。MDT の受け口（57000/tcp）と Starlark の変換は kafka でも stdout でも入る",
      all([g["addresses"] for g in c["inputs"]["gnmi"]] == [["203.0.113.11:57400"]] * 2
          and c["inputs"]["cisco_telemetry_mdt"] == [{"transport": "grpc", "service_address": ":57000", "tags": {"collector": "mdt"}}]
          and [p["script"] for p in c["processors"]["starlark"]] == ["/etc/telegraf/lab_gnmi.star"]
          and [p["script"] for p in c["aggregators"]["starlark"]] == ["/etc/telegraf/lab_circuits.star"]
          for c in (kafka, stdout_conf))
      and "mdt: 57000/tcp" in out
      and [o["topic"] for o in kafka["outputs"]["kafka"]] == ["metrics", "gnmi", "traps", "logs", "mdt"]
      and kafka["outputs"]["kafka"][-1]["tagpass"] == {"collector": ["mdt"]})
check("SNMP_POLL=1 は inputs.snmp を残し、agents を SNMP_AGENTS で埋める（ifName をタグにした interface の表と system）",
      _poll is not None and _poll["inputs"]["snmp"][0]["agents"] == ["udp://203.0.113.11:161", "udp://203.0.113.12:161"]
      and _poll["inputs"]["snmp"][0]["name"] == "system" and _poll["inputs"]["snmp"][0]["table"][0]["name"] == "interface"
      and len(_poll["inputs"]["snmp_trap"]) == 1 and "snmp poll: \"udp://203.0.113.11:161\"" in out_poll)
check("SNMP_AGENTS を見るのは SNMP_POLL=1 のときだけ（無いか形が違えば止まる）。SNMP_POLL は 0 か 1 だけ",
      render("stdout", SNMP_AGENTS="")[0] is not None and render("stdout", SNMP_AGENTS="bad")[0] is not None
      and render("stdout", SNMP_POLL="1", SNMP_AGENTS="")[0] is None and render("stdout", SNMP_POLL="1", SNMP_AGENTS="bad")[0] is None
      and render("stdout", SNMP_POLL="0")[0] is not None and render("stdout", SNMP_POLL="yes")[0] is None and render("stdout", SNMP_POLL="true")[0] is None)
def _tg_test(cmd="test", **extra):  # 描いた設定に入力が無ければ、telegraf を呼ぶ前に分かる言葉で止まる（手元に telegraf は無くてよい）
    with tempfile.TemporaryDirectory() as d:
        env = {"PATH": os.environ["PATH"], "TELEGRAF_TEMPLATE": os.path.join(ROOT, "telegraf", "telegraf.conf.in"),
               "TELEGRAF_CONF": os.path.join(d, "telegraf.conf"), "AWS_REGION": "ap-northeast-1", "SINK": "stdout",
               "GNMI_TARGETS": '"203.0.113.11:57400"', "GNMI_USERNAME": "u", "GNMI_PASSWORD": "p", **extra}
        return subprocess.run(["bash", os.path.join(ROOT, "telegraf", "telegraf.sh"), cmd], capture_output=True, text=True, env=env)
_t = _tg_test()
check("tg test はポーリングを止めている（SNMP_POLL=0）と、SNMP_POLL=1 で起こし直すよう言って止まる",
      _t.returncode == 1 and "SNMP_POLL=1" in _t.stderr and "telegraf: command not found" not in _t.stderr)

# 役割（TELEGRAF_ROLE）: stream の ECS は受ける側（dialout）と取りにいく側（dialin）の 2 タスク。既定 all（デバッグ用の EC2）は両方（2026-10-04 ユーザー決定）
_roles = re.findall(r"^# >>> role (\w+)", tpl, re.M)
_dialin_blk = tpl.split("# >>> role dialin", 1)[1].split("# <<< role dialin", 1)[0] if "# >>> role dialin" in tpl else ""
check("telegraf.conf.in の役割の区間は telegraf.sh の ROLES と同じ名前で対になり、snmp_poll の区間は dialin の中にある",
      sorted(_roles) == sorted(re.findall(r"^# <<< role (\w+)", tpl, re.M)) == sorted(re.search(r'^ROLES="([^"]*)"', tg_sh, re.M).group(1).split())
      and "# >>> snmp_poll" in _dialin_blk and "# <<< snmp_poll" in _dialin_blk
      and sh_const(tg_sh, "TELEGRAF_ROLE") == "${TELEGRAF_ROLE:-all}")
_out_conf, _out_log, _ = render(None, KAFKA_BROKERS="b-1.example:9098,b-2.example:9098", TELEGRAF_ROLE="dialout", GNMI_TARGETS="", SNMP_AGENTS="")
_in_conf, _in_log, _ = render(None, KAFKA_BROKERS="b-1.example:9098,b-2.example:9098", TELEGRAF_ROLE="dialin", SNMP_POLL="1")
check("TELEGRAF_ROLE=dialout は trap / syslog / MDT と syslog の rename だけ（gNMI・SNMP のポーリング・Starlark は無く、GNMI_TARGETS / SNMP_AGENTS は要らない）",
      _out_conf is not None and sorted(_out_conf["inputs"]) == ["cisco_telemetry_mdt", "snmp_trap", "syslog"]
      and list(_out_conf["processors"]) == ["rename"] and "aggregators" not in _out_conf
      and "role: dialout" in _out_log and "mdt: 57000/tcp" in _out_log and "gnmi:" not in _out_log)
check("TELEGRAF_ROLE=dialin は gNMI 2 つ・SNMP のポーリング（SNMP_POLL=1 のとき）・Starlark だけ（trap / syslog / MDT の受け口は無い）",
      _in_conf is not None and sorted(_in_conf["inputs"]) == ["gnmi", "snmp"] and len(_in_conf["inputs"]["gnmi"]) == 2
      and list(_in_conf["processors"]) == ["starlark"] and list(_in_conf["aggregators"]) == ["starlark"]
      and "role: dialin" in _in_log and "trap:" not in _in_log and "mdt:" not in _in_log
      and render(None, KAFKA_BROKERS="b-1.example:9098,b-2.example:9098", TELEGRAF_ROLE="dialin", GNMI_TARGETS="")[0] is None)
check("どの役割でも出力（Kafka の 5 つと health）は同じ。知らない TELEGRAF_ROLE は描かずに止まる。既定（all）は両方の入力を持つ",
      all(c["outputs"] == kafka["outputs"] and c["agent"] == kafka["agent"] for c in (_out_conf, _in_conf))
      and render("stdout", TELEGRAF_ROLE="dial-in")[0] is None and render("stdout", TELEGRAF_ROLE="")[0] is not None
      and "role: all" in out and {"snmp_trap", "syslog", "cisco_telemetry_mdt", "gnmi"} <= set(stdout_conf["inputs"]))
_tg_out = [_tg_test(c, TELEGRAF_ROLE="dialout", GNMI_TARGETS="") for c in ("test", "gnmi")]
check("受ける側（dialout）のタスクで tg test / tg gnmi を打つと、取りにいく側（telegraf-dialin）で打つよう言って止まる",
      all(r.returncode == 1 and "telegraf-dialin" in r.stderr and "telegraf: command not found" not in r.stderr for r in _tg_out))

# 機器の認証情報: 設定には ${...} のまま残し、Telegraf が起きるときに環境変数から読む（stream の ECS は SSM の SecureString を secrets で受ける。2026-10-04）
check("telegraf.conf.in は機器の認証情報を持たず、${GNMI_USERNAME} / ${GNMI_PASSWORD} / ${SNMP_COMMUNITY} で受ける（render した設定にも値は入らない）",
      "NokiaSrl1" not in tpl and 'community = "public"' not in tpl
      and tpl.count('username = "${GNMI_USERNAME}"') == 2 and tpl.count('password = "${GNMI_PASSWORD}"') == 2 and tpl.count('community = "${SNMP_COMMUNITY}"') == 1
      and all(g["username"] == "${GNMI_USERNAME}" and g["password"] == "${GNMI_PASSWORD}" for g in stdout_conf["inputs"]["gnmi"])
      and _poll["inputs"]["snmp"][0]["community"] == "${SNMP_COMMUNITY}")
check("取りにいく入力があるとき（all / dialin）は GNMI_USERNAME / GNMI_PASSWORD が無ければ止まり、SNMP_COMMUNITY は SNMP_POLL=1 のときだけ要る。dialout はどれも要らない",
      render("stdout", GNMI_USERNAME="")[0] is None and render("stdout", GNMI_PASSWORD="")[0] is None
      and render("stdout", SNMP_COMMUNITY="")[0] is not None and render("stdout", SNMP_POLL="1", SNMP_COMMUNITY="")[0] is None
      and render(None, KAFKA_BROKERS="b-1.example:9098", TELEGRAF_ROLE="dialin", GNMI_PASSWORD="")[0] is None
      and render(None, KAFKA_BROKERS="b-1.example:9098", TELEGRAF_ROLE="dialout", GNMI_USERNAME="", GNMI_PASSWORD="", SNMP_COMMUNITY="")[0] is not None)
check("lab の認証情報（containerlab の既定）は lab.sh と lab-common.sh で同じで、lab.sh telegraf run が Telegraf に渡す",
      all(sh_const(lab_sh, k) == sh_const(common, "LAB_" + k) != "" for k in ("GNMI_USERNAME", "GNMI_PASSWORD", "SNMP_COMMUNITY")))

# ---- lab.sh: この EC2 の Telegraf
check("trap のポートは lab.sh と telegraf.sh で同じ（機器は 162 に送り、デバッグ用の EC2 は REDIRECT で Telegraf の待つポートへ）",
      sh_const(lab_sh, "TRAP_PORT") == sh_const(tg_sh, "TRAP_PORT") == "1162"
      and re.search(r'iptables -t nat -I PREROUTING 1 -s "\$MGMT" -d "\$MGMT_GW" -p udp --dport 162 "\$\{c\[@\]\}" -j REDIRECT --to-ports "\$TRAP_PORT"', lab_sh) is not None)
check("syslog のポートも lab.sh と telegraf.sh で同じ", sh_const(lab_sh, "LOG_PORT") == sh_const(tg_sh, "LOG_PORT") == "5140")
check("lab の SR Linux の syslog の形式は lab.sh の LOG_STANDARD = lab-common.sh の LAB_SYSLOG_STANDARD = RFC5424（Telegraf の既定 RFC3164 を上書きする）",
      sh_const(lab_sh, "LOG_STANDARD") == sh_const(common, "LAB_SYSLOG_STANDARD") == "RFC5424"
      and sh_const(tg_sh, "SYSLOG_STANDARD") == "${SYSLOG_STANDARD:-RFC3164}")
check("lab.sh telegraf run は同じイメージを host ネットワークで SINK=stdout で起こし、ポーリング先は up.sh と同じ lab_topology.py から作る",
      re.search(r"docker run -d --name \"\$TG\" --restart unless-stopped --network host [^\n]*\\\n\s*-e SINK=stdout -e SYSLOG_STANDARD=\"\$LOG_STANDARD\" -e SNMP_POLL=\"\$\{SNMP_POLL:-0\}\" -e AWS_REGION -e SNMP_AGENTS=\"\$agents\" -e GNMI_TARGETS=\"\$gnmi\" \\\n\s*-e GNMI_USERNAME=\"\$GNMI_USERNAME\" -e GNMI_PASSWORD=\"\$GNMI_PASSWORD\" -e SNMP_COMMUNITY=\"\$SNMP_COMMUNITY\" \"\$TELEGRAF_IMAGE\" run", lab_sh) is not None
      and "python3 lab_topology.py . --snmp-agents" in lab_sh and "python3 lab_topology.py . --gnmi-targets" in lab_sh
      and "lab/lab_topology.py lab --snmp-agents" in up)
check("lab.sh の forward は TELEGRAF_IMAGE があれば SSM の NLB を見ずに抜ける（デバッグ用の EC2 は stream を使わない）",
      re.search(r'forward\)\n\s*if \[ -n "\$\{TELEGRAF_IMAGE:-\}" \]; then[\s\S]*?exit 0\n\s*fi', lab_sh) is not None)
check("lab.sh pull は TELEGRAF_IMAGE があるときだけ Telegraf も引く",
      'for i in "$SRLINUX_IMAGE" "$MULTITOOL_IMAGE" ${TELEGRAF_IMAGE:+"$TELEGRAF_IMAGE"}; do docker pull -q "$i"; done' in lab_sh)

# ---- ops: up.sh / down.sh とは別（2026-10-04 ユーザー決定）
def _code(src):  # コメント行と echo の案内を外した、実際に動く行
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#") and "echo " not in l)
check("ops/up.sh / ops/down.sh はデバッグ用の EC2 を作らない・消さない・見ない",
      not any(w in _code(src) for src in (up, down) for w in ("lab-debug.sh", "lab-debug\"", "cfn_stack_status", "cloudformation", "LAB_DEBUG_STACK", "flag_value LAB_DEBUG")))
check("LAB_DEBUG は使わない。deploy.env に残っていれば up.sh が注意を出すだけ（deploy.env.example には無い）",
      "LAB_DEBUG" not in read("deploy.env.example")
      and re.search(r'case "\$\{LAB_DEBUG:-\}" in \'\'\|0\|false\|no\) ;; \*\) echo "注意: LAB_DEBUG は使わない。[^"]*ops/lab-debug\.sh up / down', up) is not None)
_epblk = up[up.index('ENDPOINTS=""'):up.index('echo "インターフェース型エンドポイント')]
def _endpoints(roots, **env):
    r = subprocess.run(["bash", "-c", f'ROOTS="{roots}"\n' + _epblk + 'echo "OUT: $ENDPOINTS"'],
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env})
    return r.stdout.strip().splitlines()[-1]
check("土台だけなら up.sh は ECR のエンドポイントを作らない（LAB_DEBUG=1 が残っていても。デバッグ用の EC2 は自分の VPC のを使う）",
      _endpoints("base/ecr base/core", LAB_DEBUG="1") == "OUT: ssm ssmmessages" == _endpoints("base/ecr base/core"))
def _stack_status(aws_out, aws_rc):
    """cfn_stack_status を、決まった出力と終了コードを返す偽の aws で打つ。(stdout, stderr, 終了コード)"""
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "aws")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(aws_out)} >&2\nexit {aws_rc}\n" if aws_rc else f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(aws_out)}\n")
        os.chmod(fake, 0o755)
        r = subprocess.run(["bash", "-c", 'REGION=ap-northeast-1; . ops/lab-common.sh; cfn_stack_status x-lab-debug'],
                           capture_output=True, text=True, cwd=ROOT, env={"PATH": d + os.pathsep + os.environ["PATH"]})
    return r.stdout.strip(), r.stderr.strip(), r.returncode
_ok, _gone, _err = (_stack_status("CREATE_COMPLETE", 0),
                    _stack_status("An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id x-lab-debug does not exist", 254),
                    _stack_status("An error occurred (ExpiredToken) when calling the DescribeStacks operation: token expired", 254))
check("cfn_stack_status は「無い」（空・0）と「読めない」（認証切れなど。1）を分ける（読めないのを無いと扱うと、lab-debug.sh down が消さずに終わり、up が器を作り直そうとする）",
      _ok == ("CREATE_COMPLETE", "", 0) and _gone == ("", "", 0) and _err[0] == "" and _err[2] == 1 and "ExpiredToken" in _err[1]
      and "s=$(stack_status)" in dbg and '"$(stack_status)"' not in dbg)
check("スタック名は <接頭辞>-lab-debug で、ロールとインスタンスプロファイルは lab の EC2（<接頭辞>-lab）と別の名前",
      'STACK="$PREFIX-lab-debug"' in dbg and role["RoleName"] == {"Fn::Sub": "${NamePrefix}-lab-debug"}
      and res["InstanceProfile"]["Properties"]["InstanceProfileName"] == {"Fn::Sub": "${NamePrefix}-lab-debug"})
for f in (("ops", "lab-common.sh"), ("ops", "lab-debug.sh"), ("ops", "up.sh"), ("ops", "down.sh"), ("lab", "setup.sh"), ("lab", "lab.sh"), ("telegraf", "telegraf.sh")):
    r = subprocess.run(["bash", "-n", os.path.join(ROOT, *f)], capture_output=True, text=True)
    check(f"{'/'.join(f)} は bash として読める", r.returncode == 0)

print(f"通過 {passed} / 失敗 0")
