FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PARCEL_SYNC_DIR=/data \
    PARCEL_SYNC_AUTH_BIND=0.0.0.0 \
    PARCEL_SYNC_INTERVAL=1800

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parcel_sync.py docker/entrypoint.sh ./
RUN chmod 755 entrypoint.sh \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data && chmod 700 /data

USER app
# credentials.json, token.json and state live here. Mount a volume; never bake them in.
VOLUME ["/data"]
# Only used by `auth` for the one-time Google sign-in.
EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["daemon"]
