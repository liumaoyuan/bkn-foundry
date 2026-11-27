/**
 * 模型连接配置：本地文件优先，环境变量兜底。
 *
 * 两种模式：
 * - manual：自己填 OpenAI 兼容地址 / 模型名 / API Key（开发或直连）；
 * - platform：使用「九格多源本体构建系统 → 模型管理」里已配置好的模型，
 *   由 copilot-svc 带上当前用户 token 转发到平台的 mf-model-api，不需要在本服务里存 key。
 *
 * 存储位置：<cwd>/data/model-config.json
 * - 只在服务端读写，接口只返回掩码后的 key；
 * - data/ 已在 .gitignore 中忽略，不会进仓库。
 */
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

export type ModelMode = "manual" | "platform";

export type ModelSettings = {
  mode: ModelMode;
  /** platform 模式：平台模型 ID（mf-model-manager 的 model_id） */
  platformModel?: string;
  /** platform 模式：平台模型显示名，便于前端回显 */
  platformModelName?: string;
  /** manual 模式 */
  baseUrl?: string;
  model?: string;
  apiKey?: string;
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

/** 校验并规整一条配置；不完整时返回 null。 */
function normalizeSettings(raw: Partial<ModelSettings>): ModelSettings | null {
  if (raw.mode === "platform") {
    if (!isNonEmpty(raw.platformModel)) {
      return null;
    }
    return {
      mode: "platform",
      platformModel: raw.platformModel.trim(),
      platformModelName: isNonEmpty(raw.platformModelName) ? raw.platformModelName.trim() : undefined,
    };
  }
  if (!isNonEmpty(raw.baseUrl) || !isNonEmpty(raw.model) || !isNonEmpty(raw.apiKey)) {
    return null;
  }
  return {
    mode: "manual",
    baseUrl: normalizeBaseUrl(raw.baseUrl),
    model: raw.model.trim(),
    apiKey: raw.apiKey.trim(),
  };
}

/** 读取 data/model-config.json；内容不完整时视为未配置。 */
export function readStoredSettings(): ModelSettings | null {
  if (!existsSync(SETTINGS_PATH)) {
    return null;
  }
  try {
    const raw = JSON.parse(readFileSync(SETTINGS_PATH, "utf8")) as Partial<ModelSettings>;
    return normalizeSettings(raw);
  } catch {
    return null;
  }
}

/** 保存配置（自动创建 data 目录）。 */
export function writeStoredSettings(settings: ModelSettings): void {
  const normalized = normalizeSettings(settings);
  if (!normalized) {
    throw new Error("模型配置不完整");
  }
  mkdirSync(dirname(SETTINGS_PATH), { recursive: true });
  writeFileSync(SETTINGS_PATH, `${JSON.stringify(normalized, null, 2)}\n`, "utf8");
}

/** 删除本地配置，回退到环境变量。 */
export function clearStoredSettings(): void {
  if (existsSync(SETTINGS_PATH)) {
    rmSync(SETTINGS_PATH, { force: true });
  }
}

/** 环境变量配置（COPILOT_LLM_*），仅 manual 模式。 */
export function readEnvSettings(): ModelSettings | null {
  const baseUrl = process.env.COPILOT_LLM_BASE_URL?.trim();
  const apiKey = process.env.COPILOT_LLM_API_KEY?.trim();
  const model = process.env.COPILOT_LLM_MODEL?.trim();
  if (!baseUrl || !apiKey || !model) {
    return null;
  }
  return { mode: "manual", baseUrl: normalizeBaseUrl(baseUrl), model, apiKey };
}

/** 平台模型模式的环境变量兜底（COPILOT_PLATFORM_MODEL_ID）。 */
export function readEnvPlatformSettings(): ModelSettings | null {
  const platformModel = process.env.COPILOT_PLATFORM_MODEL_ID?.trim();
  if (!platformModel) {
    return null;
  }
  return { mode: "platform", platformModel };
}

/** 生效配置：本地文件 > 环境变量。 */
export function resolveSettings(): { source: SettingsSource; settings: ModelSettings | null } {
  const stored = readStoredSettings();
  if (stored) {
    return { source: "file", settings: stored };
  }
  const envPlatform = readEnvPlatformSettings();
  if (envPlatform) {
    return { source: "env", settings: envPlatform };
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