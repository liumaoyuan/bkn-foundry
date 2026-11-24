/**
 * 平台模型：使用「九格多源本体构建系统 → 模型管理」里已配置的模型。
 *
 * 原理：
 * - 模型清单：GET {MANAGER}/api/mf-model-manager/v1/llm/list（需要用户登录 token）
 * - 对话：POST {API}/api/mf-model-api/v1/chat/completions（同样透传用户 token，
 *   平台会把请求路由到实际配置的模型，例如客户本地部署的 Ollama）
 *
 * copilot-svc 自身不保存任何平台密钥，只做「带 token 转发」。
 */
const MANAGER_URL = (process.env.COPILOT_PLATFORM_MANAGER_URL ?? "http://mf-model-manager:9898").replace(/\/+$/, "");
const MODEL_API_URL = (process.env.COPILOT_PLATFORM_MODEL_API_URL ?? "http://mf-model-api:9898").replace(/\/+$/, "");

const LIST_PATH = "/api/mf-model-manager/v1/llm/list";
const CHAT_PATH = "/api/mf-model-api/v1/chat/completions";

export type PlatformModel = {
  id: string;
  name: string;
  series?: string;
  maxModelLen?: number;
  isDefault?: boolean;
};

export type PlatformChatMessage = { role: "system" | "user" | "assistant"; content: string };

export type PlatformChatOptions = {
  stream?: boolean;
  onDelta?: (text: string) => void;
  timeoutMs?: number;
  modelId?: string;
  maxTokens?: number;
};

function bearer(token: string): Record<string, string> {
  return { authorization: token.startsWith("Bearer ") ? token : `Bearer ${token}`, "content-type": "application/json" };
}

function readString(source: unknown, keys: string[]): string | undefined {
  if (!source || typeof source !== "object") {
    return undefined;
  }
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return undefined;
}

function readNumber(source: unknown, keys: string[]): number | undefined {
  if (!source || typeof source !== "object") {
    return undefined;
  }
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return undefined;
}

/** 拉取平台已配置的大模型清单。 */
export async function listPlatformModels(token: string, timeoutMs = 15000): Promise<PlatformModel[]> {
  const url = `${MANAGER_URL}${LIST_PATH}?size=100&page=1`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { headers: bearer(token), signal: controller.signal });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`平台模型清单获取失败（HTTP ${response.status}）${text ? `：${text.slice(0, 200)}` : ""}`);
    }
    const payload = (await response.json()) as { data?: unknown[] } | unknown[];
    const rows = Array.isArray(payload) ? payload : (payload.data ?? []);
    return rows
      .map((row) => {
        const id = readString(row, ["model_id", "modelId", "id"]);
        const name = readString(row, ["model_name", "modelName", "name"]) ?? id;
        if (!id || !name) {
          return null;
        }
        return {
          id,
          name,
          series: readString(row, ["model_series", "modelSeries", "series"]),
          maxModelLen: readNumber(row, ["max_model_len", "maxModelLen"]),
          isDefault: row && typeof row === "object" ? Boolean((row as Record<string, unknown>).f_default) : false,
        } as PlatformModel;
      })
      .filter((item): item is PlatformModel => item !== null);
  } finally {
    clearTimeout(timer);
  }
}

function extractContent(payload: unknown): string {
  if (!payload || typeof payload !== "object") {
    return "";
  }
  const record = payload as Record<string, unknown>;
  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = choices[0] as Record<string, unknown>;
    const message = first.message as Record<string, unknown> | undefined;
    const direct = first.text ?? first.content;
    if (message && typeof message.content === "string") {
      return message.content;
    }
    if (typeof direct === "string") {
      return direct;
    }
  }
  if (typeof record.content === "string") {
    return record.content;
  }
  if (typeof record.answer === "string") {
    return record.answer;
  }
  return "";
}

/** 通过平台模型服务完成一次对话；stream=true 时逐段回调 onDelta。 */
export async function chatViaPlatform(
  token: string,
  model: string,
  messages: PlatformChatMessage[],
  options: PlatformChatOptions = {},
): Promise<string> {
  const timeoutMs = options.timeoutMs ?? 180000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${MODEL_API_URL}${CHAT_PATH}`, {
      method: "POST",
      headers: bearer(token),
      body: JSON.stringify({
        model,
        ...(options.modelId ? { model_id: options.modelId } : {}),
        max_tokens: options.maxTokens ?? 8192,
        messages,
        stream: Boolean(options.stream),
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`平台模型调用失败（HTTP ${response.status}）${text ? `：${text.slice(0, 300)}` : ""}`);
    }
    if (!options.stream) {
      const payload = (await response.json()) as unknown;
      return extractContent(payload);
    }
    if (!response.body) {
      throw new Error("平台模型调用失败：响应没有内容流");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line.startsWith("data:")) {
          continue;
        }
        const data = line.slice(5).trim();
        if (!data || data === "[DONE]") {
          continue;
        }
        try {
          const payload = JSON.parse(data) as Record<string, unknown>;
          const choices = payload.choices;
          let deltaText = "";
          if (Array.isArray(choices) && choices.length > 0) {
            const first = choices[0] as Record<string, unknown>;
            const delta = first.delta as Record<string, unknown> | undefined;
            const message = first.message as Record<string, unknown> | undefined;
            if (delta && typeof delta.content === "string") {
              deltaText = delta.content;
            } else if (message && typeof message.content === "string") {
              deltaText = message.content;
            } else if (typeof first.text === "string") {
              deltaText = first.text;
            }
          } else if (typeof payload.content === "string") {
            deltaText = payload.content;
          }
          if (deltaText) {
            answer += deltaText;
            options.onDelta?.(deltaText);
          }
        } catch {
          // 忽略非 JSON 的心跳行
        }
      }
    }
    return answer;
  } finally {
    clearTimeout(timer);
  }
}