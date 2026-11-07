# Multi-stage: Go is compiled within the builder (the independent compilation stage of the original Azure pipeline has been inlined), leaving only the binary for prod.
########################### Stage 0: builder ########################
ARG BUILD_IMAGE=golang:1.26.9
ARG baseImage=ubuntu:22.04

FROM ${BUILD_IMAGE} AS builder

ARG GOPROXY_URL="https://proxy.golang.org,direct"
ENV GOPROXY=${GOPROXY_URL}

COPY adp/execution-factory/operator-integration /workspace/adp/execution-factory/operator-integration
WORKDIR /workspace/adp/execution-factory/operator-integration

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    set -ex; \
    go mod download; \
    go build -ldflags '-s -w -checklinkname=0' -o /out/operator-integration ./server/main.go

########################### Stage 1: prod ########################
FROM ${baseImage} AS prod

# Ubuntu apt mirror override (host only, e.g. mirrors.aliyun.com); empty keeps the upstream sources.
ARG APT_MIRROR=

WORKDIR /app/operator-integration

COPY --from=builder /out/operator-integration ./operator-integration

# The Go binary does not include root certificates, and the base image does not include them either.
# Without ca-certificates, outbound HTTPS requests fail with "certificate signed by unknown authority" (for example, querying PyPI versions).
RUN if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        if [ -f "$f" ]; then \
          sed -i "s|http://archive.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g" "$f"; \
        fi; \
      done; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user.
RUN useradd -m -r -s /bin/bash aishu \
    && chown -R aishu:aishu /app/operator-integration

# Switch to non-root user.
USER aishu

CMD ["./operator-integration"]
