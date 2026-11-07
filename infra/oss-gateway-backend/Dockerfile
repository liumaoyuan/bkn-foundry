# Global build arguments must be declared before FROM.
ARG GO_BUILD_IMAGE=golang:1.26.9
ARG BASE_IMAGE=ubuntu:24.04

# Build stage.
FROM ${GO_BUILD_IMAGE} AS builder
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /app

COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o oss-gateway main.go

# Runtime stage; redeclare the build argument after FROM.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Ubuntu apt mirror override (host only, e.g. mirrors.aliyun.com); empty keeps the upstream sources.
ARG APT_MIRROR=

# Install runtime dependencies with apt-get on Ubuntu.
RUN if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        if [ -f "$f" ]; then \
          sed -i "s|http://archive.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://$APT_MIRROR/ubuntu|g" "$f"; \
        fi; \
      done; \
    fi; \
    apt-get update && apt-get install -y ca-certificates tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /root/

COPY --from=builder /app/oss-gateway .
COPY --from=builder /app/locale ./locale

EXPOSE 8080

CMD ["./oss-gateway"]
