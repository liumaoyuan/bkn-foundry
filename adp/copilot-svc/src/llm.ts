/**
 * 大模型调用：支持两种模式——
 * 1) manual：自带 OpenAI 兼容接口（/chat/completions）；
 * 2) platform：转发到「九格多源本体构建系统 → 模型管理」里已配置的模型（需用户 token）。
 * 配置来源见 settings.ts（本地文件 > 环境变量）。
 */
import { resolveSettings, type ModelSettings } from "./settings.js";
import { chatViaPlatform } from "./platform-model.js";

export type LlmConfig = {
  baseUrl: string;
  apiKey: string;
  model: string;
};

/** 当前生效的模型设置（manual 或 platform）。 */
export function readModelSettings(): ModelSettings | null {
  return resolveSettings().settings;
}

/** 仅 manual 模式返回 OpenAI 兼容配置。 */
export function readLlmConfig(): LlmConfig | null {
  const settings = readModelSettings();
  if (!settings || settings.mode !== "manual" || !settings.baseUrl || !settings.model) {
    return null;
  }
  return { baseUrl: settings.baseUrl, apiKey: settings.apiKey ?? "", model: settings.model };
}

/** 平台模式返回平台模型 id。 */
export function readPlatformModelId(): string | null {
  const settings = readModelSettings();
  if (!settings || settings.mode !== "platform" || !settings.platformModel) {
    return null;
  }
  return settings.platformModel;
}

export function isLlmConfigured(): boolean {
  return readModelSettings() !== null;
}

const EXTRACT_SYSTEM_PROMPT = `你是本体建模助手。根据用户提供的业务文档，抽取知识网络本体。
只输出 JSON（不要解释文字），结构如下：
{
  "networkId": "英文小写下划线",
  "networkName": "中文名",
  "description": "一句话说明",
  "objectTypes": [
    {
      "id": "英文小写下划线",
      "name": "中文名",
      "description": "一句话说明",
      "properties": [
        { "name": "英文小写下划线", "displayName": "中文名", "type": "string|integer|number|boolean|datetime", "description": "说明" }
      ]
    }
  ],
  "relations": [
    { "id": "英文小写下划线", "name": "中文关系名", "source": "对象id", "target": "对象id", "description": "说明" }
  ],
  "actionTypes": [
    { "id": "act_x", "name": "中文名", "description": "说明", "actionType": "add|modify|delete", "boundObject": "对象id", "riskLevel": "low|medium|high", "requiresApproval": true, "trigger": { "field": "对象字段", "operation": "==", "value": "值" } }
  ],
  "riskTypes": [
    { "id": "risk_x", "name": "中文名", "description": "说明", "policies": ["管控策略1", "管控策略2"] }
  ],
  "conceptGroups": [
    { "id": "cg_x", "name": "中文名", "objectTypes": ["对象id"] }
  ]
}
要求：
1. 对象 3-12 个；每个对象 3-10 个属性（必须含 id）。
2. 对象粒度：只输出可以直接落地的**业务对象类型**（业务名词，2-6 个字，如「设备」「维修工单」「雷达车」）；不要输出抽象基类或元模型层（如「空间实体」「装备实体」「规则条件」「输入参数」「处理模型」），文档里的分类层级、继承关系不要变成对象。
3. 关系只连接已定义对象，且必须给出 source 与 target（对象 id）。
4. 关系命名规范：必须是**动词短语**（2-6 个汉字，如「包含部件」「发生故障」「指派人员」「部署于」），方向为主语→动词→宾语（source 是动作发出方，target 是动作承受方）；同一对对象不要输出多条同义关系；不要用「关联」「对应」这类无信息量的词除非确实描述不清。
5. 行动类型（actionTypes）2-5 个：必须是业务上真的会执行的动作（如「创建工单」「派工」「标记故障已处理」），boundObject 填已定义对象 id；actionType 只能 add/modify/delete；高风险或需审批的动作设 riskLevel=high 且 requiresApproval=true；能从文档里找到触发条件的，填 trigger.field/operation/value。
6. 风险类型（riskTypes）1-3 个：描述这类业务最容易出的风险（如「工单超期未关闭」），policies 写 2-4 条可执行的管控策略（审批、阈值、升级、审计），不要写空泛口号。`;

const CHAT_SYSTEM_PROMPT = `你是「九格多源本体构建系统」里的本体建模助手，正在与用户对话式修改一个「知识网络本体草案」。

称呼要求（必须遵守）：回答里一律称本系统为「九格多源本体构建系统」；不要出现 OpenBKN、BKN、bkn 字样；需要提到导出文件时说「本体定义包」，不要提命令行工具名。

规则：
1. 先用中文写 2–5 条要点回复用户：做了什么、影响了几条关系、还有什么需要确认；不要空泛地说「已完成」。
2. 然后用单独一行输出标记 @@CHANGES@@，紧接着输出一个 JSON 对象 {"ops":[...]}。ops 是**要执行的原子操作列表**，系统会按顺序真实执行：
   - 新增对象：{"op":"add_object","name":"装备","description":"...","properties":[{"name":"id","displayName":"编号","type":"string"}]}
   - 删除对象：{"op":"remove_object","id":"equipment"} 或 {"op":"remove_object","name":"装备"}
   - 对象改名：{"op":"rename_object","id":"equipment","newName":"武器装备"}
   - 新增关系：{"op":"add_relation","source":"装备","target":"导弹阵地","name":"部署于"}
   - 删除关系：{"op":"remove_relation","name":"部署于"} 或 {"op":"remove_relation","id":"equipment_missile_site"}
   - 新增行动：{"op":"add_action","name":"创建工单","actionType":"add","boundObject":"维修工单","description":"故障确认后创建工单","riskLevel":"medium","requiresApproval":false}
   - 修改行动：{"op":"update_action","name":"派工","actionType":"modify","riskLevel":"high","requiresApproval":true}
   - 删除行动：{"op":"remove_action","name":"派工"}
   - 新增风险：{"op":"add_risk","name":"工单超期未关闭","description":"工单超过 24 小时未闭环","policies":["超过24小时升级班组长","超过48小时通报设备主管"]}
   - 修改风险：{"op":"update_risk","name":"工单超期未关闭","policies":["超过12小时提醒","超过24小时升级"]}
   - 删除风险：{"op":"remove_risk","name":"工单超期未关闭"}
3. 删除/改名/修改时，对象、关系、行动、风险的 id 或 name 必须与「当前草案」里完全一致（行动看 name 或 id，风险看 name 或 id）；没有修改时 ops 输出空数组 []。
4. 不要声称执行了 ops 里没有的变更；只输出上述格式，不要 Markdown 代码块。

示例：
好的，我把「装备」对象删掉，并清理它关联的 3 条关系。
@@CHANGES@@
{"ops":[{"op":"remove_object","name":"装备"}]}`;

type ChatParseResult = { reply?: unknown; ops?: unknown; ir?: unknown };

type ChatCompletionPayload = {
  choices?: { message?: { content?: string } }[];
};

function extractJsonObject(content: string): unknown | null {
  const stripped = content
    .replace(/^```(?:json)?/m, "")
    .replace(/```\s*$/m, "")
    .trim();
  const start = stripped.indexOf("{");
  const end = stripped.lastIndexOf("}");
  if (start < 0 || end <= start) {
    return null;
  }
  try {
    return JSON.parse(stripped.slice(start, end + 1)) as unknown;
  } catch {
    return null;
  }
}

async function postChatCompletion(
  config: LlmConfig,
  body: Record<string, unknown>,
  timeoutMs: number,
): Promise<{ ok: true; content: string } | { ok: false; error: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${config.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({ model: config.model, ...body }),
      signal: controller.signal,
    });

    if (!response.ok) {
      return { ok: false, error: `LLM HTTP ${response.status}` };
    }

    const payload = (await response.json()) as ChatCompletionPayload;
    return { ok: true, content: payload.choices?.[0]?.message?.content ?? "" };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "LLM 调用失败" };
  } finally {
    clearTimeout(timer);
  }
}

/** 流式调用 OpenAI 兼容接口：逐块吐字，返回完整文本。 */
async function postChatCompletionStream(
  config: LlmConfig,
  body: Record<string, unknown>,
  timeoutMs: number,
  onDelta: (text: string) => void,
): Promise<{ ok: true; content: string } | { ok: false; error: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${config.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({ model: config.model, stream: true, ...body }),
      signal: controller.signal,
    });

    if (!response.ok || !response.body) {
      return { ok: false, error: `LLM HTTP ${response.status}` };
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let lineBuffer = "";
    let content = "";
    let finished = false;

    while (!finished) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      lineBuffer += decoder.decode(chunk.value, { stream: true });
      const lines = lineBuffer.split("\n");
      lineBuffer = lines.pop() ?? "";
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line.startsWith("data:")) {
          continue;
        }
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") {
          finished = true;
          break;
        }
        try {
          const parsed = JSON.parse(payload) as {
            choices?: { delta?: { content?: string } }[];
          };
          const delta = parsed.choices?.[0]?.delta?.content;
          if (delta) {
            content += delta;
            onDelta(delta);
          }
        } catch {
          // 忽略心跳/非 JSON 行
        }
      }
    }

    return { ok: true, content };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "LLM 调用失败" };
  } finally {
    clearTimeout(timer);
  }
}

const OPS_MARKER = "@@CHANGES@@";

/**
 * 流式对话式修改草案。
 * 模型先正常说话（边生成边推给前端），说到 @@CHANGES@@ 后转到操作 JSON；
 * 文本部分实时可见，操作部分在结束时解析并交给服务端执行。
 */
export async function chatWithDraftStream(
  messages: { role: string; content: string }[],
  draft: unknown,
  onDelta: (text: string) => void,
): Promise<{ ok: true; reply: string; ops?: unknown; ir?: unknown } | { ok: false; error: string }> {
  const config = readLlmConfig();
  if (!config) {
    return { ok: false, error: "未配置 LLM" };
  }

  const history = messages
    .slice(-8)
    .map((item) => ({
      role: item.role === "assistant" ? "assistant" : "user",
      content: String(item.content ?? "").slice(0, 4_000),
    }));
  const draftText = draft ? JSON.stringify(draft).slice(0, 60_000) : "（暂无草案）";

  let pending = "";
  let replyText = "";
  let opsBuffer = "";
  let markerSeen = false;
  const emit = (text: string) => {
    if (!text) {
      return;
    }
    replyText += text;
    onDelta(text);
  };

  const result = await postChatCompletionStream(
    config,
    {
      temperature: 0.3,
      messages: [
        { role: "system", content: `${CHAT_SYSTEM_PROMPT}\n\n当前草案：\n${draftText}` },
        ...history,
      ],
    },
    180_000,
    (chunk) => {
      if (markerSeen) {
        opsBuffer += chunk;
        return;
      }
      pending += chunk;
      const index = pending.indexOf(OPS_MARKER);
      if (index >= 0) {
        emit(pending.slice(0, index).trimEnd());
        opsBuffer = pending.slice(index + OPS_MARKER.length);
        pending = "";
        markerSeen = true;
        return;
      }
      // 尾部可能是半个标记：留够长度，避免把 "@@CHAN" 吐给用户。
      const safeLength = Math.max(0, pending.length - OPS_MARKER.length);
      emit(pending.slice(0, safeLength));
      pending = pending.slice(safeLength);
    },
  );

  if (!result.ok) {
    return { ok: false, error: result.error };
  }

  if (!markerSeen) {
    emit(pending);
    // 模型没按格式输出：尝试把整段当 JSON 解析出 ops。
    const parsed = extractJsonObject(replyText) as ChatParseResult | null;
    if (parsed && Array.isArray(parsed.ops)) {
      return { ok: true, reply: "", ops: parsed.ops, ir: parsed.ir };
    }
    return { ok: true, reply: replyText.trim() };
  }

  const parsed = extractJsonObject(opsBuffer) as { ops?: unknown; ir?: unknown } | null;
  return {
    ok: true,
    reply: replyText.trim(),
    ops: parsed?.ops,
    ir: parsed?.ir,
  };
}

const SCHEMA_SYSTEM_PROMPT = `你是本体建模助手。用户只提供数据库结构（表/列/主键/外键/注释），没有业务描述文档。
请根据表名、列名、注释、外键关系，推断业务本体。

只输出 JSON（不要解释文字、不要 Markdown 代码块），结构如下：
{
  "networkId": "英文小写下划线",
  "networkName": "中文名",
  "description": "一句话说明",
  "objectTypes": [
    {
      "id": "英文小写下划线",
      "name": "中文业务名（2-6 字）",
      "description": "一句话说明",
      "sourceTable": "对应数据库表名（必须来自输入的表名）",
      "confidence": "high|medium|low",
      "properties": [
        { "name": "英文小写下划线", "displayName": "中文名", "type": "string|integer|number|boolean|datetime", "mappedField": "对应数据库列名（必须来自输入的列名）", "description": "说明" }
      ]
    }
  ],
  "relations": [
    { "id": "英文小写下划线", "name": "中文动词短语（2-6 字）", "source": "对象id", "target": "对象id", "confidence": "high|medium|low", "mapping": [{ "sourceProperty": "源对象属性name", "targetProperty": "目标对象属性name" }] }
  ],
  "actionTypes": [
    { "id": "英文小写下划线", "name": "中文动词短语（2-6 字，如「处理工单」）", "description": "一句话说明", "actionType": "add|modify|delete", "boundObject": "对象id", "riskLevel": "low|medium|high", "requiresApproval": false }
  ],
  "riskTypes": [
    { "id": "英文小写下划线", "name": "中文风险名（如「工单超时未关闭」）", "description": "一句话说明", "policies": ["可执行的管控策略，2-4 条"] }
  ],
  "questions": ["需要用户确认的问题，最多 5 条"]
}

规则：
1. 注释优先：表注释/列注释就是最可靠的业务语义，用它命名对象与属性。
2. 没有注释时按命名规范推断：t_/tb_/tbl_/dim_/fact_ 前缀去掉；*_id 是外键、*_code/*_no 是业务编号、is_*/status 是状态、*_at/*_time/*_date 是时间。
3. 外键关系直接转成关系类型：子表 → 父表，关系名用动词短语（如「属于」「包含」「指派给」），mapping 写清楚源属性→目标属性（列名对应的属性 name）。
4. 只输出真正的业务实体表；纯关联表（只有两个外键、没有其他业务列）不要单独建对象，而是用它表达多对多关系。
5. 置信度：有注释、命名清晰用 high；仅靠命名推断用 medium；表名语义不明或结构异常用 low。
6. questions 最多 5 条，只问真正影响建模的问题（如「t_wo 是工单还是出库单？」）。
7. sourceTable / mappedField 必须是输入里真实存在的表名/列名，不要编造。
8. 行动类型（1-4 个）：从状态类列（status/state/状态）、时间戳（*_at/*_time）和常见业务动作推断，如「创建工单」「处理告警」「关闭工单」；actionType 只能是 add/modify/delete，boundObject 必须是已生成的对象 id。
9. 风险类型（1-3 个）：从等级类列（level/severity/priority/级别/严重程度）、超时/逾期语义推断，如「高等级告警未及时处理」「工单超期未关闭」；policies 写 2-4 条可执行策略，不写空泛口号。若结构里完全没有状态/等级/时间语义，可以不输出这两个数组。`;

/** 只有数据库结构时（无业务描述）抽取 IR。 */
export async function extractIrFromSchema(
  brief: string,
): Promise<{ raw: unknown } | { error: string }> {
  const config = readLlmConfig();
  if (!config) {
    return { error: "未配置 LLM" };
  }

  const result = await postChatCompletion(
    config,
    {
      temperature: 0.2,
      messages: [
        { role: "system", content: SCHEMA_SYSTEM_PROMPT },
        { role: "user", content: brief.slice(0, 120_000) },
      ],
    },
    180_000,
  );

  if (!result.ok) {
    return { error: result.error };
  }

  const parsed = extractJsonObject(result.content);
  if (!parsed) {
    return { error: "LLM 未返回 JSON" };
  }
  return { raw: parsed };
}

/** 调用 OpenAI 兼容接口抽取 IR，失败返回 error。 */
export async function extractIrWithLlm(
  text: string,
): Promise<{ raw: unknown } | { error: string }> {
  const config = readLlmConfig();
  if (!config) {
    return { error: "未配置 LLM" };
  }

  const result = await postChatCompletion(
    config,
    {
      temperature: 0.2,
      messages: [
        { role: "system", content: EXTRACT_SYSTEM_PROMPT },
        { role: "user", content: text.slice(0, 60_000) },
      ],
    },
    120_000,
  );

  if (!result.ok) {
    return { error: result.error };
  }

  const parsed = extractJsonObject(result.content);
  if (!parsed) {
    return { error: "LLM 未返回 JSON" };
  }
  return { raw: parsed };
}

/** 对话式修改草案：返回回答文本 + 要执行的操作（或完整 IR 兜底）。 */
export async function chatWithDraft(
  messages: { role: string; content: string }[],
  draft: unknown,
): Promise<{ ok: true; reply: string; ops?: unknown; ir?: unknown } | { ok: false; error: string }> {
  const config = readLlmConfig();
  if (!config) {
    return { ok: false, error: "未配置 LLM" };
  }

  const history = messages
    .slice(-8)
    .map((item) => ({
      role: item.role === "assistant" ? "assistant" : "user",
      content: String(item.content ?? "").slice(0, 4_000),
    }));

  const draftText = draft ? JSON.stringify(draft).slice(0, 60_000) : "（暂无草案）";
  const result = await postChatCompletion(
    config,
    {
      temperature: 0.3,
      messages: [
        { role: "system", content: `${CHAT_SYSTEM_PROMPT}\n\n当前草案：\n${draftText}` },
        ...history,
      ],
    },
    120_000,
  );

  if (!result.ok) {
    return { ok: false, error: result.error };
  }

  const parsed = extractJsonObject(result.content) as ChatParseResult | null;
  if (!parsed) {
    return { ok: true, reply: result.content.trim().slice(0, 2_000) };
  }

  const fallbackReply =
    Array.isArray(parsed.ops) && parsed.ops.length > 0
      ? "已按要求更新草案。"
      : "没有识别到需要修改的内容。";
  const reply = typeof parsed.reply === "string" && parsed.reply.trim() ? parsed.reply.trim() : fallbackReply;
  return { ok: true, reply, ops: parsed.ops, ir: parsed.ir };
}

/** 测试连接：发一条最小请求，返回耗时。 */
export async function testLlmConnection(
  override?: Partial<LlmConfig>,
): Promise<{ ok: true; latencyMs: number } | { ok: false; error: string }> {
  const stored = readLlmConfig();
  const config: LlmConfig | null =
    override && override.baseUrl && override.model && override.apiKey
      ? { baseUrl: override.baseUrl.replace(/\/+$/, ""), model: override.model, apiKey: override.apiKey }
      : stored;
  if (!config) {
    return { ok: false, error: "未配置模型连接" };
  }

  const startedAt = Date.now();
  const result = await postChatCompletion(
    config,
    {
      temperature: 0,
      max_tokens: 8,
      messages: [{ role: "user", content: "只回复 ok" }],
    },
    20_000,
  );

  if (!result.ok) {
    return { ok: false, error: result.error };
  }
  return { ok: true, latencyMs: Date.now() - startedAt };
}

/** 平台模式：抽取 IR（非流式）。 */
export async function extractIrWithPlatform(
  token: string,
  model: string,
  modelId: string | undefined,
  text: string,
): Promise<{ raw: unknown } | { error: string }> {
  try {
    const content = await chatViaPlatform(token, model, [
      { role: "system", content: EXTRACT_SYSTEM_PROMPT },
      { role: "user", content: text.slice(0, 60_000) },
    ]);
    const parsed = extractJsonObject(content);
    if (!parsed) {
      return { error: "LLM 未返回 JSON" };
    }
    return { raw: parsed };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "平台模型调用失败" };
  }
}

/** 平台模式：流式对话修改草案（与 manual 模式同样的 @@CHANGES@@ 协议）。 */
export async function chatWithDraftPlatformStream(
  token: string,
  model: string,
  modelId: string | undefined,
  messages: { role: string; content: string }[],
  draft: unknown,
  onDelta: (text: string) => void,
): Promise<{ ok: true; reply: string; ops?: unknown; ir?: unknown } | { ok: false; error: string }> {
  const history = messages
    .slice(-8)
    .map((item) => ({
      role: item.role === "assistant" ? "assistant" : "user",
      content: String(item.content ?? "").slice(0, 4_000),
    }));
  const draftText = draft ? JSON.stringify(draft).slice(0, 60_000) : "（暂无草案）";

  let pending = "";
  let replyText = "";
  let opsBuffer = "";
  let markerSeen = false;
  const emit = (text: string) => {
    if (!text) {
      return;
    }
    replyText += text;
    onDelta(text);
  };

  try {
    await chatViaPlatform(
      token,
      model,
      [
        { role: "system" as const, content: `${CHAT_SYSTEM_PROMPT}\n\n当前草案：\n${draftText}` },
        ...history.map((item) => ({ role: item.role as "user" | "assistant", content: item.content })),
      ],
      {
        stream: true,
        onDelta: (chunk) => {
          if (markerSeen) {
            opsBuffer += chunk;
            return;
          }
          pending += chunk;
          const index = pending.indexOf(OPS_MARKER);
          if (index >= 0) {
            emit(pending.slice(0, index).trimEnd());
            opsBuffer = pending.slice(index + OPS_MARKER.length);
            pending = "";
            markerSeen = true;
            return;
          }
          const safeLength = Math.max(0, pending.length - OPS_MARKER.length);
          emit(pending.slice(0, safeLength));
          pending = pending.slice(safeLength);
        },
      },
    );
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "平台模型调用失败" };
  }

  if (!markerSeen) {
    emit(pending);
    const parsed = extractJsonObject(replyText) as ChatParseResult | null;
    if (parsed && Array.isArray(parsed.ops)) {
      return { ok: true, reply: "", ops: parsed.ops, ir: parsed.ir };
    }
    return { ok: true, reply: replyText.trim() };
  }

  const parsed = extractJsonObject(opsBuffer) as { ops?: unknown; ir?: unknown } | null;
  return { ok: true, reply: replyText.trim(), ops: parsed?.ops, ir: parsed?.ir };
}

/** 平台模式：测试连通性（发一条最小请求）。 */
export async function testPlatformConnection(
  token: string,
  model: string,
  modelId: string | undefined,
): Promise<{ ok: true; latencyMs: number } | { ok: false; error: string }> {
  const startedAt = Date.now();
  try {
    await chatViaPlatform(token, model, [{ role: "user", content: "ping" }], {
      timeoutMs: 30_000,
    });
    return { ok: true, latencyMs: Date.now() - startedAt };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "平台模型调用失败" };
  }
}