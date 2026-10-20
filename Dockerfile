# syntax=docker/dockerfile:1
# One image for web, prober and engine; compose picks the command.
# Base images are pinned by digest; bump them deliberately (tag kept for readability).

ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements/runtime.txt /tmp/runtime.txt
# --require-hashes + --no-deps: nothing gets in that is not pinned and hashed.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/runtime.txt

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 sentinel \
    && useradd --system --uid 10001 --gid sentinel --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin sentinel \
    && install -d -o 10001 -g 10001 -m 0700 /data  # prober buffer (a volume in compose)
COPY --from=deps /opt/venv /opt/venv
COPY src/ /app/src/
COPY catalog/ /app/catalog/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "sentinel.web"]
