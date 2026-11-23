/**
 * 模型连接配置：本地文件优先，环境变量兜底。
 *
 * 存储位置：<cwd>/data/model-config.json
 * - 只在服务端读写，接口只返回掩码后的 key；
 * - data/ 已在 .gitignore 中忽略，不会进仓库。
 */
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

export type ModelSettings = {
  baseUrl: string;
  model: string;
  apiKey: string;
};

export type SettingsSource = "file" | "env" | "none";

// 允许测试/部署通过环境变量改写存储位置
const SETTINGS_PATH = process.env.COPILOT_SETTINGS_PATH
  ? resolve(process.env.COPILOT_SETTINGS_PATH)
  : resolve(process.cwd(), "data", "model-config.json");

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(/\/+$/, "");
}

function isNonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/** 读取 data/model-config.json；内容不完整时视为未配置。 */
export function readStoredSettings(): ModelSettings | null {
  if (!existsSync(SETTINGS_PATH)) {
    return null;
  }
  try {
    const raw = JSON.parse(readFileSync(SETTINGS_PATH, "utf8")) as Partial<ModelSettings>;
    if (!isNonEmpty(raw.baseUrl) || !isNonEmpty(raw.model) || !isNonEmpty(raw.apiKey)) {
      return null;
    }
    return {
      baseUrl: normalizeBaseUrl(raw.baseUrl),
      model: raw.model.trim(),
      apiKey: raw.apiKey.trim(),
    };
  } catch {
    return null;
  }
}

/** 保存配置（自动创建 data 目录）。 */
export function writeStoredSettings(settings: ModelSettings): void {
  mkdirSync(dirname(SETTINGS_PATH), { recursive: true });
  const payload: ModelSettings = {
    baseUrl: normalizeBaseUrl(settings.baseUrl),
    model: settings.model.trim(),
    apiKey: settings.apiKey.trim(),
  };
  writeFileSync(SETTINGS_PATH, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

/** 删除本地配置，回退到环境变量。 */
export function clearStoredSettings(): void {
  if (existsSync(SETTINGS_PATH)) {
    rmSync(SETTINGS_PATH, { force: true });
  }
}

/** 环境变量配置（COPILOT_LLM_*）。 */
export function readEnvSettings(): ModelSettings | null {
  const baseUrl = process.env.COPILOT_LLM_BASE_URL?.trim();
  const apiKey = process.env.COPILOT_LLM_API_KEY?.trim();
  const model = process.env.COPILOT_LLM_MODEL?.trim();
  if (!baseUrl || !apiKey || !model) {
    return null;
  }
  return { baseUrl: normalizeBaseUrl(baseUrl), model, apiKey };
}

/** 生效配置：本地文件 > 环境变量。 */
export function resolveSettings(): { source: SettingsSource; settings: ModelSettings | null } {
  const stored = readStoredSettings();
  if (stored) {
    return { source: "file", settings: stored };
  }
  const env = readEnvSettings();
  if (env) {
    return { source: "env", settings: env };
  }
  return { source: "none", settings: null };
}

/** 掩码：保留前 4 位与后 4 位，便于用户确认用的是哪把 key。 */
export function maskApiKey(apiKey: string): string {
  if (apiKey.length <= 8) {
    return "***";
  }
  return `${apiKey.slice(0, 4)}***${apiKey.slice(-4)}`;
}

export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
