import { existsSync } from "node:fs";
import { resolve } from "node:path";

import Fastify from "fastify";
import cors from "@fastify/cors";
import multipart from "@fastify/multipart";

import { copilotRoutes } from "./routes/copilot.js";

export function buildApp() {
  const app = Fastify({ logger: false });
  app.register(cors, { origin: true });
  app.register(multipart, {
    limits: { fileSize: 50 * 1024 * 1024, files: 10 },
  });
  app.register(copilotRoutes, { prefix: "/api/copilot" });
  return app;
}

const entry = process.argv[1] ?? "";
if (entry.endsWith("server.ts") || entry.endsWith("server.js")) {
  // 本地开发：从 copilot-svc/.env 读取模型配置（不存在则跳过，等价于规则模式）。
  // 只在作为入口运行时加载，避免被测试 import 时把真实 key 带进单测。
  const envFile = resolve(process.cwd(), ".env");
  if (existsSync(envFile)) {
    process.loadEnvFile(envFile);
  }

  const app = buildApp();
  const port = Number(process.env.COPILOT_PORT ?? 8787);
  const host = process.env.COPILOT_HOST ?? "127.0.0.1";
  app
    .listen({ port, host })
    .then(() => console.log(`copilot-svc on http://127.0.0.1:${port}`))
    .catch((err) => {
      console.error(err);
      process.exit(1);
    });
}
