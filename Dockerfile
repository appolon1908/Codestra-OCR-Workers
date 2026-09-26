# syntax=docker/dockerfile:1.7
# Codestra OCR worker: FastAPI + Tesseract (spa/eng) + OpenCV, non-root runtime.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_THREAD_LIMIT=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng tini \
 && rm -rf /var/lib/apt/lists/* \
 && tesseract --version \
 && tesseract --list-langs | grep -qx spa

FROM base AS builder
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# `docker build --target test .` runs the full quality gate against real Tesseract.
FROM builder AS test
COPY requirements-dev.lock ./
RUN pip install -r requirements-dev.lock
COPY tests ./tests
COPY openapi ./openapi
COPY contracts ./contracts
RUN ruff check src tests \
 && ruff format --check src tests \
 && mypy \
 && codestra-ocr-export-openapi --check \
 && pytest -q -rs

FROM base AS runtime
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} ocr \
 && useradd --system --uid ${APP_UID} --gid ocr --home-dir /nonexistent --shell /usr/sbin/nologin ocr
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    OCR_WORKER_HOST=0.0.0.0 \
    OCR_WORKER_PORT=8080 \
    OCR_WORKER_ENVIRONMENT=production \
    TMPDIR=/tmp
USER ${APP_UID}:${APP_UID}
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["codestra-ocr-worker"]
