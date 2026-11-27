FROM node:22-alpine AS deps
WORKDIR /app
ARG NPM_REGISTRY=https://registry.npmmirror.com/
# 容器内没有可用 IPv6 路由，Node 若先选 AAAA 记录会 ENETUNREACH；强制 IPv4 优先。
ENV NODE_OPTIONS="--dns-result-order=ipv4first"
RUN npm config set registry ${NPM_REGISTRY} \
 && npm config set fetch-retries 5 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm install -g pnpm@11.5.1
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile=false --registry=${NPM_REGISTRY} \
      --fetch-retries=5 --fetch-timeout=120000

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NODE_OPTIONS="--dns-result-order=ipv4first"
COPY --from=deps /app/node_modules ./node_modules
COPY package.json tsconfig.json ./
COPY src ./src
EXPOSE 8787
CMD ["node", "node_modules/tsx/dist/cli.mjs", "src/server.ts"]
