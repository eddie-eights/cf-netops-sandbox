# AgentCore Runtime は linux/arm64 のイメージしか動かさない。
# ビルドは必ず --platform linux/arm64 で行う（README の手順 2）。
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# 社内ネットワークは SSL インスペクションで証明書チェーンを社内 CA に差し替える。PC には社内 CA が入っているが
# コンテナの中の pip には無いので pip install が CERTIFICATE_VERIFY_FAILED で落ちる（2026-09-15 に会社 PC で確認）。
# PyPI の 3 ホストだけ証明書の検証を外す（--trusted-host）。パッケージ自体はハッシュ付きの wheel なので中身の改竄は別に検出される。
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"

WORKDIR /app
COPY requirements.txt .
RUN pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -r requirements.txt

COPY app.py topology.py anomalies.py graph.py .
COPY data/ data/

# root で動かさない
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080
CMD ["python", "app.py"]
