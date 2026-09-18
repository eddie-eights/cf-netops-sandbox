# WORKFLOW のワーカー（Temporal の worker + 異常を拾う starter）。ECS Fargate の ARM64 で動かすので
# ビルドは必ず --platform linux/arm64（ops/up.sh の手順 2 が打つ）。temporal 本体は同じタスクの別コンテナ（temporalio/temporal を ECR にミラー）。
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# 社内ネットワークの SSL インスペクション対策（agent/Dockerfile と同じ理由）
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"

WORKDIR /app
COPY requirements.txt .
RUN pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -r requirements.txt

COPY worker.py .

# root で動かさない
RUN useradd --create-home --uid 10001 worker && chown -R worker:worker /app
USER worker

CMD ["python", "worker.py"]
