FROM python:3.12-slim

ENV PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

EXPOSE 8000

# Entrypoint chowns the (root-owned on Railway) /data volume to appuser, then
# drops privileges via setpriv (util-linux, present in debian slim) so uvicorn
# never runs as root against the persistent volume.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
