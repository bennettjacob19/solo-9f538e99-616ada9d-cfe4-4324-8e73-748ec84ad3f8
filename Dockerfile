FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SVCDIR_DB=/data/directory.sqlite3 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY svcdir ./svcdir

RUN useradd --system --uid 10001 --create-home svcdir \
    && mkdir -p /data \
    && chown -R svcdir:svcdir /data
USER svcdir

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Multiple worker processes with threads each.  All shared state lives in
# SQLite (WAL + BEGIN IMMEDIATE), so registrations, renewals, restores and
# queries stay consistent across processes; no per-process cache is trusted.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "8", \
     "--timeout", "60", "--access-logfile", "-", "svcdir.__main__:app"]
