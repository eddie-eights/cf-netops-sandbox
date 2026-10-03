# Nautobot のコンテナ（terraform/pipeline/nautobot の ECS。Fargate の arm64）。ops/up.sh の手順 2 がビルドして ECR の <接頭辞>-nautobot に置く。
# 上流の公式イメージに、Job（jobs/ → JOBS_ROOT の /opt/nautobot/jobs）と、Job が使うモジュール（netops/ → /opt/nautobot/netops）を足すだけ。
# 設定は上流の nautobot_config.py が環境変数（NAUTOBOT_DB_* / NAUTOBOT_REDIS_* / NAUTOBOT_SECRET_KEY …）から読むので、設定ファイルは足さない。
# ビルドの context は nautobot/ そのままではない。ops/up.sh が一時ディレクトリに nautobot/ の中身と、agent/graph.py・agent/toolkit.py
# （Neptune へ Gremlin で書く関数。Web や Lambda と同じもの）、lab_seed.json（lab/lab_topology.py の出力。最初の seed）を集めて渡す。
# 同じイメージを web（bootstrap.py → uwsgi）と celery worker（Job を回す）の 2 つのコンテナで使う。
ARG NAUTOBOT_VERSION=3.2.6
FROM networktocode/nautobot:${NAUTOBOT_VERSION}-py3.12

# 上流は pip を root で入れてから USER nautobot に落としている。boto3（SSM / ECS / neptunedata）だけ足す。
# --trusted-host は agent/Dockerfile と同じ理由（社内ネットワークの SSL インスペクション）
USER root
RUN pip install --no-cache-dir --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org "boto3>=1.40,<2"

COPY --chown=nautobot:nautobot jobs/ /opt/nautobot/jobs/
COPY --chown=nautobot:nautobot netops/ graph.py toolkit.py lab_seed.json /opt/nautobot/netops/

# 閉域なので外へは出られない。インストールの統計（外への送信）は切る
ENV PYTHONPATH=/opt/nautobot/netops \
    NAUTOBOT_INSTALLATION_METRICS_ENABLED=false

USER nautobot
