FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /service

COPY requirements.lock .
# Cache the wheels and retry aggressively so a single stalled download does not fail the whole build.
RUN --mount=type=cache,target=/root/.cache/pip \
    for i in 1 2 3; do \
      pip install --require-hashes --timeout 120 --retries 10 \
        -i ${PIP_INDEX_URL} -r requirements.lock && exit 0; \
      echo "pip install failed (attempt $i), retrying..."; \
      sleep 5; \
    done; \
    exit 1

COPY VERSION .
COPY main.py .
COPY app ./app

EXPOSE 30800
CMD ["python", "main.py"]
