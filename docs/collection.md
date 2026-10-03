# 機器から集めるデータ

← [README](../README.md)

機器から何を集めたいか、いまどこまで集めているか、telemetry と性能メトリクスをどう取るか（方針と未決定事項）。2026-10-04 時点。

## 欲しいもの

| 種類 | 中身 |
|---|---|
| syslog | 機器のログ |
| SNMP trap | linkDown / linkUp などのイベント |
| telemetry | 機器の状態（BGP / IS-IS / EVPN など）と、下の性能メトリクス |
| 性能メトリクス | CPU 使用率、メモリ使用率、セッション数（アクティブ数・上限到達率）、帯域使用率（IF・集約リンク）、パケットドロップ数、収容回線数に対する使用率 |

- 性能メトリクスは Grafana と Splunk のしきい値で検知する。パケットドロップ数だけは増加傾向を見る（回数そのものでなく、ふだんとの比で見る）。
- 「本番と同じデータ」は、本番（Cisco）と同じ型・情報のこと。lab（SR Linux）の値を Cisco の YANG そのものにする意味ではない。取り方が違っても、同じ形にそろえて Kafka に流せばよい。

## いまの状態

| 種類 | 既定で取れるか | 経路 |
|---|---|---|
| syslog | 取れる | 機器 → 5140/udp → NLB → Telegraf → `logs`。既定の `SYSLOG_STANDARD=RFC3164` は本番の Cisco 向けで、lab の SR Linux（RFC5424）のログは崩れる（[deploy.md](deploy.md)） |
| SNMP trap | 取れる | 機器 → 162/udp → NLB → Telegraf → `traps`。異常として上げるのは Splunk（`SINK_SPLUNK=1`）だけ |
| telemetry | 一部 | Telegraf → 機器の gNMI（57400/tcp）で BGP / IS-IS / EVPN / MAC の状態を `gnmi` トピックへ。本番の MDT の受け口（57000/tcp → `mdt` トピック）はあるが、送ってよい機器（`MDT_SOURCE_CIDRS`）が既定で空なので何も届かない |
| 性能メトリクス | lab だけ | lab の SR Linux から gNMI で CPU・メモリ・IF のカウンタと速度・MAC テーブルの数（セッションの代替）・収容回線数の代替を購読し、Telegraf の中で共通の形（下の「共通の形（仮）」）に変えて `metrics` トピックへ（2026-10-04。実機の lab では未確認）。本番の MDT は受け口だけで、共通の形への変換はまだ無い（`mdt` トピックに生のまま）。SNMP のポーリングは既定で止めている（`SNMP_POLL=0`）。`SNMP_POLL=1` でも IF の 32 ビットカウンタとエラー数だけ |

trap と syslog では性能の時系列は取れない（届くのはイベントか、しきい値を越えたという知らせだけ）。性能メトリクスにはポーリングか telemetry が要る。

## 方針: telemetry と性能メトリクスは Cisco MDT の dial-out で受ける（2026-10-04）

機器のほうから Telegraf へ送らせる（dial-out）。Telegraf は `inputs.cisco_telemetry_mdt` で受ける。

```
機器 ─ trap 162/udp ────────┐
     ─ syslog 5140/udp ─────┼─→ NLB ─→ Telegraf の受ける側（何台でもよい）─→ MSK
     ─ MDT dial-out（TCP）──┘          inputs.cisco_telemetry_mdt
```

**受け口（2026-10-04 に作った）:** Telegraf の `inputs.cisco_telemetry_mdt`（gRPC、57000/tcp、タグ `collector=mdt`）→ Kafka の `mdt` トピック（生のまま。共通の形への変換は本番の sensor path が決まってから）。NLB に TCP 57000 のリスナーがあり、Telegraf の NLB の SG は `deploy.env` の `MDT_SOURCE_CIDRS`（機器の CIDR をカンマで。既定は空でどこからも受けない。`0.0.0.0/0` は拒む）だけを通す。Spark は `mdt` もメトリクスのトピックとして S3 Tables と Prometheus に流す。TLS と機器側の設定（`telemetry ietf subscription` / `receiver`）は未決定。

**選んだ理由: Telegraf を増やしやすい。**

- 機器から送ってくるもの（trap・syslog・MDT）は、NLB が 1 つのタスクにだけ渡すので、タスクを増やしても重複しない。
- Telegraf から取りにいくもの（SNMP のポーリング、gNMI の購読）は、タスクごとに同じ機器へ取りにいくので、増やすと MSK に同じデータが何回も入る。取りにいく側のサービス（`<prefix>-telegraf-dialin`）だけを 1 タスクに固定している（[telegraf.tf](../terraform/pipeline/stream/telegraf.tf) の `desired_count = 1` と `deployment_maximum_percent = 100`）のはこのため（下の「Telegraf を受ける側と取りにいく側に分けた」）。
- 集める側から機器への通信（161/udp や gNMI の TCP）を本番で開けてもらえるか分からないので、機器 → 集める側の向きだけで済むほうが安全。

**MDT で取れる性能メトリクス:** MDT は機器の運用データ（oper の YANG モデル）を周期（periodic）か変化時（on-change）で送るので、性能の時系列も送れる。どのモデルが使えるかは機種と版で変わる。IOS XE を仮定した例（本番では未確認）:

| 指標 | モデルの例 |
|---|---|
| CPU | `Cisco-IOS-XE-process-cpu-oper` |
| メモリ | `Cisco-IOS-XE-memory-oper` |
| 帯域・ドロップ・エラー（IF と Port-channel） | `Cisco-IOS-XE-interfaces-oper`（`statistics` の octets / discards / errors、`speed`） |
| セッション・上限 | 機能ごとに別のモデル（下の「セッションと上限」） |

**Telegraf を受ける側と取りにいく側に分けた（2026-10-04 ユーザー決定）:** stream の ECS は同じイメージで 2 つのサービスを動かし、役割はタスクの環境変数 `TELEGRAF_ROLE`（`telegraf/telegraf.sh` が `telegraf.conf.in` の `# >>> role …` の区間を残すか消す）で分ける。

| サービス | `TELEGRAF_ROLE` | 入力 | 数 | SG |
|---|---|---|---|---|
| `<prefix>-telegraf-dialout` | `dialout` | trap・syslog・MDT（NLB の後ろ） | 増やしてよい（いまは 1。入れ替えは新しいタスクが立ってから古いものを止める） | `telegraf_dialout`（NLB から受けるだけ） |
| `<prefix>-telegraf-dialin` | `dialin` | gNMI の購読、SNMP のポーリング（`SNMP_POLL=1`）、lab の値を共通の形に変える Starlark | 1 に固定（入れ替えは古いものを止めてから。そのあいだ購読が数十秒切れる） | `telegraf_dialin`（受けない。機器の 161/udp・57400/tcp へ出る） |

Kafka の出力（5 トピック）と health はどちらにもある。Starlark を取りにいく側に置くのは、変える前の `lab_*` を Kafka に載せないので gNMI の入力と同じタスクにいる必要があるから。デバッグ用の EC2 は既定の `all`（両方を 1 つの Telegraf で）。

取りにいく側は機器の一覧（`GNMI_TARGETS` / `SNMP_AGENTS`）を持つ。いまは `ops/up.sh` が lab の定義から作って stream の変数で渡す（変われば取りにいく側のタスクだけが入れ替わる）。本番で機器の一覧をどこから持つか（Nautobot などから作るか）は未決定。

## セッションと上限（一般的な解釈で調べた。2026-10-04）

要件の「セッション」と「収容回線数」は中身が決まっていないので、一般的な意味で読み、Cisco の公開モデルで機器から取れるかを確かめた（YANG は [YangModels/yang](https://github.com/YangModels/yang) の `vendor/cisco/xe/2621` と `vendor/cisco/xr/24330`、MIB は [cisco/cisco-mibs](https://github.com/cisco/cisco-mibs)。実機では未確認）。

**セッション数と上限到達率:** 機器の役割で「セッション」の中身が変わる。

| 役割 | セッション | 今の数 | 上限 | 上限に当たった印 |
|---|---|---|---|---|
| BNG（IOS XR） | 加入者（PPPoE / IPoE） | `Cisco-IOS-XR-iedge4710-oper` の `iedge-license-manager-summary/session-count` | 同じ所の `session-limit`（設定値）・`session-license-count`（ライセンス数）・`session-threshold` | — |
| BNG（IOS XE） | 加入者（PPPoE） | `Cisco-IOS-XE-ppp-oper` のセッションの一覧（数を数える） | config | — |
| FW（IOS XE のゾーンベース FW） | 接続 | `Cisco-IOS-XE-fw-oper` の `current-active-conn`（トラフィッククラスごと） | config | `l4-session-limit`（上限を越えて捨てたパケット数） |
| NAT（IOS XE） | 変換エントリ | `Cisco-IOS-XE-nat-oper` の `entries` | config（`ip nat translation max-entries`） | `limit-entry-add-fail` |
| IPsec VPN（IOS XE） | トンネル | `Cisco-IOS-XE-crypto-oper` | `ipsec-tunnels/maximum` と `available`（機種の上限） | — |
| ハードウェアの表 | TCAM などのエントリ | `Cisco-IOS-XE-tcam-oper` の `tcam-entries-used`、`Cisco-IOS-XE-switch-dp-resources-oper` の `entries-used` | 同じ所の `tcam-entries-max` / `max-entries`（`percentage-used` もある） | — |

- 「config」と書いた上限は機器の設定値。IOS XE の MDT は運用データだけでなく設定データも購読できる（yang-push のストリームに両方入る。[Cisco の設定ガイド](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/1712/b_1712_programmability_cg/m_1712_prog_ietf_telemetry.html)）ので、dial-out のまま取れる見込み。
- **SNMP のポーリングにしても上限は増えない。** 今の数の MIB はある（`csubAggStatsCurrUpSessions`、`cufwConnGlobalNumActive`、`cfwConnectionStatValue`（ASA）、`cneAddrTranslationNumActive`、`cipSecGlobalActiveTunnels`）が、上限の MIB は無く、あるのは最大値（high-water mark）としきい値の設定だけ。上限まで取れるのは YANG（MDT）のほう。

**収容回線数に対する使用率:** 一般には「その機器に何本のお客さまの回線を載せているか」。形は 2 つ。

- ポート単位（集約スイッチや PE）: 使っている回線 ＝ お客さま向けのポート（またはサブインターフェース）で使用中のもの、上限 ＝ 機器のポート数。どちらも `Cisco-IOS-XE-interfaces-oper`（IF の一覧と状態）と `Cisco-IOS-XE-device-hardware-oper`（部品表）から取れる。「お客さま向け」の見分け方（IF の description、名前の規則など）は決めが要る。
- 加入者単位（BNG）: 上の BNG のセッションと同じ。
- どちらでも、運用で決めた設計上の上限（「この機器は N 回線まで」「80% で増設」）は機器から来ないので静的データ。

**割る上限の選び方（2026-10-04 決定）:** 設定上の上限（機器の config に書いた上限）を使う。無いときは機器の上限（ライセンス数・機種の上限・ポート数）、それも無いときだけ設計上の上限（静的データ）。

## lab（SR Linux）での取り方

**lab からは MDT は取れない。** SR Linux は Cisco MDT を話さない（送れるのは gNMI だけ）。lab では Telegraf から gNMI（dial-in、57400/tcp）で購読し、Telegraf の中で本番と同じ共通の形（下の「共通の形（仮）」）に変換する（`telegraf/lab_gnmi.star` と `telegraf/lab_circuits.star`。2026-10-04 に作った。lab の実機では未確認）。本番の受け口（`inputs.cisco_telemetry_mdt`）の経路は lab では通らない。
gNMI の購読は Telegraf から取りにいくので、lab の値は取りにいく側のタスク（1 つに固定）から来る（上の「Telegraf を受ける側と取りにいく側に分けた」）。

**lab のセッション数と収容回線数は本番の値の代替。** lab の機器には BNG・FW・NAT・IPsec が無いので、本番と同じ役割（今の数・上限・どこが食っているか）を持つ値で代える。エージェントが本番と同じ要素で判断できるかを要素ごとに比べ、揃わない要素は下に書いた（SR Linux 26.7.2 の YANG（[nokia/srlinux-yang-models](https://github.com/nokia/srlinux-yang-models) の `v26.7.2`）で確かめた。`telegraf.conf.in` の 2 つめの `inputs.gnmi`（`lab_*`）で購読しているが、lab の実機で値が出るかは未確認）。

### セッション数の代替: MAC テーブルのエントリ数

本番のセッション（NAT の変換、FW の接続、BNG の加入者）も lab の MAC も「通信や端末が増えると増え、上限で新しいものを入れられなくなる表のエントリ」。BNG の IPoE の加入者は MAC ごとなので、いちばん近いのは加入者。

| エージェントが使う要素 | 本番（Cisco） | lab の代替（SR Linux） | 揃うか |
|---|---|---|---|
| 今の数 | nat-oper の entries、fw-oper の `current-active-conn`、XR の `session-count` など | `/network-instance[name=*]/bridge-table/statistics/active-entries`（`mac-type` ごとにも出る。学習した分は `learnt`） | 揃う |
| 設定上の上限 | `ip nat translation max-entries`、`l4-session-limit`、XR の `session-limit` | `/network-instance[name=*]/bridge-table/mac-limit/maximum-entries`（既定 250）。機器の上限 `/system/bridge-table/mac-limit/maximum-entries` は、mac-vrf に既定値があるので使わない | 揃う |
| 警告のしきい値 | XR の `session-threshold`。XE は機能による | `mac-limit/warning-threshold-pct`（既定 95。超えると警告、5 下がると解除） | 揃う |
| どこが食っているか | 加入者ごと、内側の IF ごと | サブインターフェースごと: `/interface[name=*]/subinterface[index=*]/bridge-table/statistics/active-entries` と上限 `…/bridge-table/mac-limit/maximum-entries`（1〜8192、既定 250） | 揃う（このパスも購読する） |
| 上限で断った数 | NAT の `limit-entry-add-fail` など | **無い。**`failed-entries` はデータパスへの書き込みに失敗した数で、上限で断った数ではないので使わない | 揃わない。今の数が上限に達したら「上限に当たっている」とみなす。断った件数は出ない |
| 上限に当たったときの影響 | 新しい通信・加入者が入れない | 新しい MAC を覚えない（覚えていない端末宛はフラッディングになるのが一般的な L2 の動き。SR Linux では確かめていない） | 揃わない。影響の説明は機能ごとに KB に書く |
| 値の動き | 時間帯で増減する | lab の host は 2 台なので数個のまま動かない | 揃わない。host で MAC を増やす仕掛け（macvlan を足すなど）が要る（まだ無い） |

- しきい値を試すときは、lab の `mac-limit maximum-entries` を小さくすれば到達率を上げられる。

### 収容回線数の代替: お客さま向けの IF の数

| エージェントが使う要素 | 本番（Cisco） | lab の代替（SR Linux） | 揃うか |
|---|---|---|---|
| 今の数 | お客さま向けの IF の数（何をお客さま向けとみなすかは未決定） | `type bridged` のサブインターフェースを持つ IF（lab では host へ向かう `lag1`。fabric の IF は routed なので数えない）。`/interface[name=*]/subinterface[index=*]/type` と `oper-state` | 数え方は揃う。ただ「お客さま向け」の決め方が機器の設定に依存する。本番と lab で同じ決め方（トポロジで相手が host やお客さまの IF）にすると揃う |
| 上限 | 物理ポート数 | 物理ポート数（`/interface[name=ethernet-*]` の数。SR Linux は未使用のポートも状態に出す）。設定上の上限は無いので機器の上限を使う | 揃う |
| 値の動き | 開通・解約で変わる（ほぼ静的） | 設定を変えたときだけ変わる | 揃う |

### ほかに調べて使わなかった候補

| 候補 | 使わない理由 |
|---|---|
| IPv6 の ND の上限（サブ IF ごとの `ipv6/neighbor-discovery/limit/max-entries`。`log-only false` なら上限で断る） | 断る動きは本番に近いが、今の数の葉が無く一覧を数えることになる。lab は IPv4 が主で値が動かない |
| ARP | 上限の設定が無い（ND の limit は IPv6 だけ） |
| DHCP snooping | 統計はパケット数だけで、バインディングの数も上限も無い |
| BGP のピア数 | 上限が無い |
| データパスの資源（`/platform/.../datapath/.../resource` の used / free） | コンテナの SR Linux で値が出るか未確認 |

## 共通の形（仮。2026-10-04）

本番の機種が決まるまでの仮の形。lab の gNMI の値はこの形にして `metrics` トピックへ出す（`telegraf/lab_gnmi.star` と `telegraf/lab_circuits.star`。変えられなかった値は `lab_*` の名前のまま残り、Kafka には載らずデバッグ用の EC2 の標準出力でだけ見える。そこで `lab_*` が見えたら変換の取りこぼし）。本番の MDT もこの形に寄せる予定（未実装）。

| measurement | タグ | field |
|---|---|---|
| `device_cpu` | `source`、`component`（制御カードの slot） | `used_pct`（CPU 全体。コアごとは出さない） |
| `device_memory` | `source`、`component` | `total_bytes`、`free_bytes`、`used_pct` |
| `if_stats` | `source`、`if_name` | `in_octets` / `out_octets`、`in_discards` / `out_discards`、`in_errors` / `out_errors`（カウンタ。率は Grafana / Splunk で出す）、`speed_bps`（覚えていれば） |
| `sessions` | `source`、`kind`（lab は `mac`）、`scope`（`network_instance` / `subinterface`）、`owner` | `active`、`limit`、`warning_pct`、`used_pct`（上限が分かっているときだけ） |
| `circuits` | `source` | `active`（お客さま向けの IF の数）、`up`（そのうち up）、`capacity`（物理ポートの数）、`used_pct` |

- 上限（`limit`）と速度は、届いた値を Telegraf の中で覚えて次の値に付ける（購読の間隔が同じ 1 分なので、最初の 1 回は付かないことがある）。
- `circuits` は IF とサブ IF の値を 1 分ごとにまとめて機器ごとに 1 つ出す。3 回続けて届かなかった IF は数えない（Telegraf は gNMI の delete を載せない）。

## 未決定事項

| 項目 | 決まると何が決まるか | 状態 |
|---|---|---|
| 本番の機種と OS の版 | MDT が使えるか（旧来の IOS 15.x などには MDT が無く、SNMP のポーリングしか無い）。使える YANG モデル | ユーザーが確認中 |
| 集める側から機器へ通信を開けられるか | MDT が使えない機種に、ポーリングの道を残すか | 不明 |
| 購読するパス（sensor path）と間隔 | 共通の形の項目、Telegraf の受け口の負荷、`mdt` トピックから共通の形への変換 | 本番の版が分かってから |
| MDT の TLS と機器側の設定 | 受け口は平文の gRPC。証明書を誰が持つか、機器の `receiver` の書き方 | 本番の版が分かってから |
| 「セッション」が何のセッションか | 一般的な解釈で候補を上に並べた（BNG の加入者、FW の接続、NAT、IPsec、ハードウェアの表）。本番の機器の役割が分かれば絞れる。lab は MAC の数で代える（上の「lab での取り方」） | 本番は候補まで。lab は決定 |
| セッションの上限と収容回線数の上限の出どころ | 機器の上限（ライセンス・設定値・機種の上限・ポート数）は多くが MDT で取れる。設計上の上限だけ静的データ（`agent/data/devices.yaml` か Neptune）。割るのは設定上の上限を先に使う（上の「割る上限の選び方」） | 決定 |
| 共通の形 | Grafana と Splunk のルールを書く相手。本番の機種が 1 種類なら Cisco の形を正にし、混ざるなら独自の共通の形にする | 仮の形（上の「共通の形（仮）」）で lab を変換している。本番の機種が分かってから決める |
| lab からどう送るか | gNMI で取って Telegraf の中で共通の形に変換する（上の「lab での取り方」）。変換は作った（lab の実機では未確認） | 決定 |
| lab に Cisco の機器を足すか | 足せば MDT の受け口（`cisco_telemetry_mdt`）と Cisco の YANG の名前を lab で試せ、IOS XE ならセッションも代替ではなく本物（NAT / FW）が取れる見込み。本番が XR なら XRd（コンテナ。KVM 不要、1 台 2 GiB）、XE なら Cat8000v（VM。KVM が要るので lab の EC2 を Graviton の t4g から x86 の C8i / M8i などのネステッド仮想化か .metal に変える）。どちらも x86 だけ（lab は arm64 で通すと 2026-09-26 に決めているので、その決定を変えることになる）で、入手に Cisco の契約が要る見込み（未確認）。IOL は NETCONF が無く MDT を出せない見込みで、CML の同梱イメージは CML の中でしか使えないライセンス。SR Linux のファブリックは残し、本番と同じ OS の Cisco を 1〜2 台足すのが候補（2026-10-04 に調べた）。XRd の control-plane 版は転送が最小限で leaf の代わりにならず、Nexus（N9Kv）でファブリックを組むと 1 台 6〜10 GB で lab の EC2 が約 $0.17/h（t4g.xlarge）から $0.64〜1.28/h（r7i.2xlarge〜4xlarge）になる。Cat8000v を 2 台足すだけなら m7i.2xlarge で約 $0.52/h（東京のオンデマンド） | **当面は SR Linux のまま**（2026-10-04 決定。費用と arm64 の決定を優先）。本番の機種が分かったら見直す |
| Grafana と Splunk の分担 | 同じ指標を両方で見るとルールを 2 か所でそろえることになる（異常の id が同じなので通知は 1 つにまとまる） | 未定 |
