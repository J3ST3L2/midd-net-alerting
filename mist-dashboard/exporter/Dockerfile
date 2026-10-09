FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY mist_exporter/ /app/mist_exporter/

# Standard library only; no pip install required.
# /state holds the lookup cache (keyed hashes only). A named volume mounted here inherits this ownership.
RUN mkdir -p /state && chown 10001:10001 /state
USER 10001:10001
EXPOSE 9877
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "-m", "mist_exporter.main", "--healthcheck"]
CMD ["python", "-m", "mist_exporter.main"]
