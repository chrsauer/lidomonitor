FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_FILE=/data/state.json \
    VC_MAP_FILE=/config/validators.csv

RUN useradd -r -u 10001 lidomon && mkdir /data && chown lidomon /data
WORKDIR /app
COPY monitor.py .

USER lidomon
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s \
  CMD ["python", "monitor.py", "healthcheck"]

ENTRYPOINT ["python", "monitor.py"]
