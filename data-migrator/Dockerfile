# Dockerfile for the core-data-migrator image.
#
# Build context is the monorepo root (see release-data-migrator.yml,
# context: '..'). The consolidated SQL lives at the repo root under
# migrations/<service>/<db_type>/<version>/ — which is already the exact
# layout the runtime expects under /app/repos, so it is copied in directly
# (no collect step / copy_repos.py / git fetch / PAT / network).

ARG BASE_IMAGE=ubuntu:24.04

FROM ${BASE_IMAGE}

# PyPI index override; keeps the default upstream behavior when not passed.
ARG PIP_INDEX_URL=https://pypi.org/simple
# Ubuntu apt mirror override (host only, e.g. mirrors.aliyun.com); empty keeps the upstream sources.
ARG APT_MIRROR=

WORKDIR /app

COPY data-migrator/server ./server

# git is required for pip to install the git-sourced RDS driver in requirements.txt.
RUN --mount=type=cache,target=/root/.cache/pip \
    set -ex; \
    if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        if [ -f "$f" ]; then \
          sed -i "s|http://archive.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g" "$f"; \
        fi; \
      done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends git python3-pip ca-certificates; \
    i=0; \
    until pip3 install --timeout 120 --retries 10 -i ${PIP_INDEX_URL} \
        -r server/requirements.txt --break-system-packages; do \
      i=$((i+1)); \
      if [ "$i" -ge 3 ]; then echo "pip install failed after $i attempts"; exit 1; fi; \
      echo "pip install failed (attempt $i), retrying..."; sleep 5; \
    done; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

RUN set -ex; \
    echo "/usr/local/lib/python3.12/dist-packages/ksycopg2" > /etc/ld.so.conf.d/ksycopg2.conf; \
    ldconfig

# Bake config + migrations. migrations/<service>/<db_type>/<version>/ maps 1:1
# onto the runtime's repos/<service>/<db_type>/<version>/ layout.
COPY data-migrator/config.monorepo.yaml /app/config.yaml
COPY migrations /app/repos

CMD ["python3", "server/data-migrator.py"]
