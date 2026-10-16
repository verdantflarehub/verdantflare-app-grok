FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY grok_channel ./grok_channel
COPY dashboard ./dashboard-source
RUN python -m grok_channel.dashboard --source /app/dashboard-source --output /app/dashboard && \
    rm -rf /app/dashboard-source && pip install --no-cache-dir . && pip check && \
    useradd --uid 10001 --system --home /nonexistent grok-channel && \
    mkdir -p /data/grok && chown 10001:10001 /data/grok
ENV GROK_DASHBOARD_DIR=/app/dashboard \
    GROK_STATE_DB=/data/grok/state.sqlite3 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000 8001
CMD ["python", "-m", "grok_channel.server"]
