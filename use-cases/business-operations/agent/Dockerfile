# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# The whole build context is this directory: RuntimeStack zips it and builds the
# image remotely for arm64, so nothing outside it can reach the image. The packs are
# not here: they arrive from the published registry at cold start.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm

RUN pip install --no-cache-dir uv==0.12.7

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    DOCKER_CONTAINER=1 \
    OTEL_PYTHON_LOG_CORRELATION=true \
    PYTHONUNBUFFERED=1

COPY requirements.txt requirements.txt
# nosemgrep: missing-pip-no-cache-dir — uv uses --no-cache (equivalent)
RUN uv pip install --no-cache -r requirements.txt && \
    uv pip install --no-cache aws-opentelemetry-distro==0.20.0

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

COPY basic_agent.py .
COPY utils/ utils/

# nosemgrep: prefer-json-notation — HEALTHCHECK CMD needs shell form for || exit 1
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

CMD ["opentelemetry-instrument", "python", "-m", "basic_agent"]
