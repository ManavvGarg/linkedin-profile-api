# curl_cffi ships prebuilt wheels for glibc, so the slim (Debian) image is the
# right base — Alpine/musl would force a source build of the vendored curl.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app

# Run unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

# Hosts inject $PORT; default to 8000 for plain `docker run`.
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;\
urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/v1/health').read()" \
    || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
