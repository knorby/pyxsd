# Build from the repository root after `uv build --wheel`:
# docker build -f containers/mcp-worker/Dockerfile -t pyxsd/mcp-worker:candidate .
FROM docker.io/library/python:3.13-slim@sha256:d3449c0493345868b09708d7056dce5edda3a75233ea86008fc80c934d355a10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY dist/pyxsd-1.2.1-py3-none-any.whl /tmp/pyxsd.whl
RUN python -m pip install --no-cache-dir --no-deps /tmp/pyxsd.whl \
    && python -m pip install --no-cache-dir \
       anyio==4.10.0 defusedxml==0.7.1 pydantic==2.12.5 \
    && rm -f /tmp/pyxsd.whl \
    && groupadd --gid 65532 worker \
    && useradd --uid 65532 --gid 65532 --no-create-home worker

USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "-u"]
