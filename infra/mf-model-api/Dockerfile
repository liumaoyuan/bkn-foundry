# Base image, rebuilt from public sources and pushed to GHCR; override with a build argument if needed.
ARG BASE_IMAGE=ghcr.io/openbkn-ai/model-factory-base:v2
FROM ${BASE_IMAGE}
# PyPI index override; keeps the default upstream behavior when not passed.
ARG PIP_INDEX_URL=https://pypi.org/simple
# Set the application working directory to /app.
WORKDIR /app
# The shared base already contains the OTel SDK and FastAPI instrumentation.
# Keep only the matching OTLP HTTP exporter service-local until the next base release.
COPY requirements-observability.txt /tmp/requirements-observability.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    for i in 1 2 3; do \
      pip install --timeout 120 --retries 10 \
        -i ${PIP_INDEX_URL} -r /tmp/requirements-observability.txt && exit 0; \
      echo "pip install failed (attempt $i), retrying..."; \
      sleep 5; \
    done; \
    exit 1
# Copy the current source tree into /app.
ADD . /app

# Port.
EXPOSE 9898
# CMD
#CMD ["python3", "/app/main.py"]
ENTRYPOINT [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9898", "--workers", "1"]
