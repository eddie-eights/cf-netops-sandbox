"""Nautobot の Job（JOBS_ROOT = /opt/nautobot/jobs）。Nautobot を機器の一覧とトポロジの正にして、Telegraf の dialin と Neptune に流す。
中身は /opt/nautobot/netops/nb_sync.py。Job の行と JobHook は起動時の bootstrap.py が作って有効にする。

  SyncTopology   画面から手で打つ同期。IP をインタフェースに付け替えただけのような、JobHook が出ない変更のあとに使う
  SyncOnChange   JobHook が呼ぶ。Device / Interface / Cable / IPAddress / Service / Location / Role の作成・変更・削除のたびに同じ同期をする
"""
from nautobot.apps.jobs import BooleanVar, Job, JobHookReceiver, register_jobs

import nb_sync

name = "NetOps"


class SyncTopology(Job):
    redeploy = BooleanVar(default=False, label="Telegraf を作り直す",
                          description="機器の一覧が変わっていなくても Telegraf の dialin のサービスを作り直す（前回の作り直しが失敗したとき）")

    class Meta:
        name = "Telegraf と Neptune に同期"
        description = "Service（gnmi / snmp）を持つ機器を Telegraf の dialin の一覧に、機器・インタフェース・ケーブルを Neptune の物理層に合わせる"
        has_sensitive_variables = False

    def run(self, redeploy=False):
        return nb_sync.sync(self.logger, force_redeploy=redeploy)


class SyncOnChange(JobHookReceiver):
    class Meta:
        name = "変更のたびに Telegraf と Neptune に同期"
        description = "JobHook（netops-sync）が呼ぶ。中身は「Telegraf と Neptune に同期」と同じ"
        has_sensitive_variables = False

    def receive_job_hook(self, change, action, changed_object):
        self.logger.info("%s（%s）を受けて同期する", change.changed_object_type, action)
        return nb_sync.sync(self.logger)


register_jobs(SyncTopology, SyncOnChange)
