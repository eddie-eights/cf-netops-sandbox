# AgentCore Runtime は linux/arm64 のイメージしか動かさない。
# ビルドは必ず --platform linux/arm64 で行う（README の手順 2）。
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# 社内プロキシの中でビルドするときの入口（README の手順 2「社内プロキシの中で打つとき」）。
# - プロキシ: HTTP_PROXY / HTTPS_PROXY / NO_PROXY は BuildKit の定義済み ARG なので、
#   ここに書かなくても --build-arg で渡せば RUN の中に届く（イメージには残らない）。
# - 社内ミラー: --build-arg PIP_INDEX_URL=https://nexus.example.com/repository/pypi/simple
# - SSL を差し替えるプロキシ: --build-arg PIP_TRUSTED_HOSTS="pypi.org files.pythonhosted.org"
#   （証明書の検証を外すので、社内ネットワークの中でだけ使う）
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOSTS=

WORKDIR /app
COPY requirements.txt .
RUN set -eu; opts=""; for h in $PIP_TRUSTED_HOSTS; do opts="$opts --trusted-host $h"; done; \
    pip install $opts -r requirements.txt

COPY app.py topology.py anomalies.py graph.py .
COPY data/ data/

# root で動かさない
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080
CMD ["python", "app.py"]
