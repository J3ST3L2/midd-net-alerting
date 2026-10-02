FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY mist_poller/ /app/mist_poller/

# Standard library only; no pip install required.
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "-m", "mist_poller.health"]
CMD ["python", "-m", "mist_poller.main"]
