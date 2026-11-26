import { chatViaPlatform } from "./platform-model.js";

type StageObject = {
  id: string;
  name: string;
  description?: string;
  properties: { name: string; displayName?: string; type?: string; description?: string }[];
  primaryKey?: string;
};

type StageRelation = {
  id: string;
  name: string;
  source: string;
  target: string;
  description?: string;
  mapping?: { sourceProperty: string; targetProperty: string }[];
};

export type StagedExtractResult =
  | {
      ok: true;
      raw: Record<string, unknown>;
      stages: { stage: string; ms: number; attempt: number }[];
      warnings: string[];
    }
  | { ok: false; error: string; stages: { stage: string; ms: number; attempt: number }[] };

const STAGE_TIMEOUT = 120_000;

function extractJson(content: string): unknown | null {
  const text = String(content ?? "").trim();
  if (!text) return null;
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced ? fenced[1] : text;
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(candidate.slice(start, end + 1)) as unknown;
  } catch {
    return null;
  }
}

function sliceText(text: string, limit: number): string {
  const value = String(text ?? "").trim();
  return value.length <= limit ? value : `${value.slice(0, limit)}\n…（原文过长，已截断）`;
}

async function runStage<T>(
  token: string,
  model: string,
  modelId: string | undefined,
  stage: string,
  system: string,
  user: string,
  stages: { stage: string; ms: number; attempt: number }[],
  onProgress?: (event: StagedProgress) => void,
): Promise<{ ok: true; value: T } | { ok: false; error: string }> {
  let lastError = "";
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    if (attempt > 1) {
      onProgress?.({
        stage: stage as StagedProgress["stage"],
        status: "running",
        detail: `第 ${attempt} 次尝试（${lastError}）`,
      });
    }
    const startedAt = Date.now();
    try {
      const messages: { role: "user" | "system" | "assistant"; content: string }[] =
        attempt === 1
          ? [
              { role: "system", content: system },
              { role: "user", content: user },
            ]
          : [
              { role: "system", content: system },
              { role: "user", content: user },
              {
                role: "user",
                content: `上一次输出无法解析为合法 JSON（${lastError}）。请只输出一个 JSON 对象，不要解释，不要 Markdown 代码块。`,
              },
            ];
      const content = await chatViaPlatform(token, model, messages, {
        modelId,
        maxTokens: 8192,
      });
      stages.push({ stage, ms: Date.now() - startedAt, attempt });
      const parsed = extractJson(content);
      if (!parsed || typeof parsed !== "object") {
        lastError = "不是 JSON 对象";
        continue;
      }
      return { ok: true, value: parsed as T };
    } catch (error) {
      stages.push({ stage, ms: Date.now() - startedAt, attempt });
      lastError = error instanceof Error ? error.message : "调用失败";
    }
  }
  return { ok: false, error: `${stage} 失败：${lastError || "未知错误"}` };
}

function pickArray(value: Record<string, unknown>, keys: string[]): Record<string, unknown>[] {
  for (const key of keys) {
    const item = value[key];
    if (Array.isArray(item)) return item as Record<string, unknown>[];
  }
  return [];
}

const ACTION_TYPE_ALIAS: Record<string, string> = {
  add: "add",
  create: "add",
  insert: "add",
  new: "add",
  modify: "modify",
  update: "modify",
  edit: "modify",
  change: "modify",
  delete: "delete",
  remove: "delete",
};
const OBJECT_PROMPT = `你是本体建模专家。请从给定材料中抽取「对象类型与属性」，严格输出 JSON：
{"networkName":"知识网络名称(≤20字)","description":"一句话说明","objectTypes":[{"id":"小写英文标识","name":"中文名","description":"说明","primaryKey":"主键属性名","properties":[{"name":"属性标识(小写英文)","displayName":"中文名","type":"string|number|boolean|datetime","description":"说明"}]}]}
要求：
1. 抽取材料中真实出现的业务对象，6-14 个，按材料结构粒度逐个列出（如司令部、舰队、战队、基地指挥部、军港、舰艇、军官、学历、经历、装备等），不要把上下级或不同表合并成一个泛化对象，也不要编造。
2. 每个对象 3-12 个属性，必须有主键（如 xxx_id），字段名用材料里的原始含义；材料里单独成表/单独成段的实体要独立成对象类。
3. 对象/属性中文名要贴近材料用词；不要包含关系、行动、风险。
4. 只输出 JSON。`;

const RELATION_PROMPT = `你是本体建模专家。给定对象清单与材料，抽取「对象之间的关系」，严格输出 JSON：
{"relations":[{"id":"小写英文标识","name":"中文动词短语(2-8字)","source":"源对象id","target":"目标对象id","description":"说明","mapping":[{"sourceProperty":"源对象属性名","targetProperty":"目标对象属性名"}]}]}
要求：
1. source/target 必须严格等于给定对象清单的 id（不要写中文名），字段名就叫 source/target；不要新增对象。
2. 至少给出 6 条关系，覆盖组织隶属、舰艇归属、驻泊、任职、履历关联等；能确定连接字段时给 mapping（外键→主键），不确定就省略。
3. 名称用动词短语，如「隶属」「驻泊于」「任职于」「下辖」。
4. 只输出 JSON。`;

const ACTION_PROMPT = `你是本体建模专家。给定对象与关系清单，抽取「行动类型与风险类型」，严格输出 JSON：
{"actionTypes":[{"id":"小写英文标识","name":"中文动宾短语","description":"动作说明","actionType":"add|modify|delete","boundObject":"绑定对象id","riskLevel":"low|medium|high","trigger":"触发条件"}],
"riskTypes":[{"id":"小写英文标识","name":"风险名称","description":"风险说明","policies":["管控策略1","管控策略2"]}]}
要求：
1. boundObject 必须严格等于给定对象清单里 id 字段的值（不要写中文名）；每个对象最多 2 个行动，总数 3-8 个。
2. 风险 1-4 个，必须给出可执行策略。
3. 只输出 JSON。`;

/**
 * 分步抽取：对象属性 → 关系 → 行动风险。
 *
 * 相比一次性长提示，分步能降低小模型（如 27B）漏抽取与格式错误的概率；
 * 每一阶段失败会带着错误信息重试一次，最终合并为一份 IR 原始结构。
 */
export type StagedProgress = {
  stage: "objects" | "relations" | "actions";
  status: "running" | "done";
  detail?: string;
  ms?: number;
  error?: string;
};

export async function extractIrStagedWithPlatform(
  token: string,
  model: string,
  modelId: string | undefined,
  text: string,
  onProgress?: (event: StagedProgress) => void,
): Promise<StagedExtractResult> {
  if (!token) return { ok: false, error: "缺少平台访问令牌", stages: [] };
  if (!String(text ?? "").trim()) return { ok: false, error: "没有可分析的内容", stages: [] };

  const stages: { stage: string; ms: number; attempt: number }[] = [];
  const warnings: string[] = [];
  const source = sliceText(text, 24_000);

  onProgress?.({ stage: "objects", status: "running", detail: "按材料提取对象类与属性" });
  const objectsStage = await runStage<{
    networkName?: string;
    description?: string;
    objectTypes?: StageObject[];
  }>(token, model, modelId, "objects", OBJECT_PROMPT, source, stages, onProgress);
  if (!objectsStage.ok) {
    onProgress?.({ stage: "objects", status: "done", error: objectsStage.error });
    return { ok: false, error: objectsStage.error, stages };
  }

  const rawObjects = pickArray(
    objectsStage.value as Record<string, unknown>,
    ["objectTypes", "objects", "entityTypes", "entities"],
  );
  const objectTypes = rawObjects.filter(
    (item) => item && item["id"] && item["name"],
  ) as unknown as StageObject[];
  if (objectTypes.length === 0) {
    return { ok: false, error: "第一阶段没有抽到任何对象类型", stages };
  }
  if (objectTypes.length < 3) {
    warnings.push(`对象类型只抽到 ${objectTypes.length} 个，可能偏少`);
  }
  onProgress?.({
    stage: "objects",
    status: "done",
    detail: `${objectTypes.length} 个对象类 / ${objectTypes.reduce(
      (sum, object) => sum + (object.properties ?? []).length,
      0,
    )} 个属性`,
    ms: stages.filter((item) => item.stage === "objects").reduce((sum, item) => sum + item.ms, 0),
  });

  const relationInput = [
    "对象清单：",
    ...objectTypes.map(
      (object) =>
        `- ${object.id}（${object.name}）：${(object.properties ?? [])
          .map((property) => property.name)
          .join("、")}`,
    ),
    "",
    "材料：",
    sliceText(text, 16_000),
  ].join("\n");

  onProgress?.({ stage: "relations", status: "running", detail: "按对象清单提取关系类型" });
  const relationStage = await runStage<{ relations?: StageRelation[] }>(
    token,
    model,
    modelId,
    "relations",
    RELATION_PROMPT,
    relationInput,
    stages,
    onProgress,
  );
  if (!relationStage.ok) return { ok: false, error: relationStage.error, stages };
  const rawRelations = pickArray(
    relationStage.value as Record<string, unknown>,
    ["relations", "relationTypes", "edges", "links"],
  ) as unknown as StageRelation[];
  // 小模型常把 source/target 写成对象中文名，这里统一映射回 id
  const normalizeAlias = (value: string): string =>
    String(value ?? "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "")
      .replace(/海军|对象|类型|实体|表|记录|详情|信息/g, "");
  const idByName = new Map<string, string>();
  const registerAlias = (alias: string, id: string) => {
    const text = String(alias ?? "").trim();
    if (!text) return;
    if (!idByName.has(text)) idByName.set(text, id);
    const lower = text.toLowerCase();
    if (!idByName.has(lower)) idByName.set(lower, id);
    const normalized = normalizeAlias(text);
    if (normalized && !idByName.has(normalized)) idByName.set(normalized, id);
  };
  for (const object of objectTypes) {
    registerAlias(object.id, object.id);
    registerAlias(object.name ?? "", object.id);
  }
  const resolveEndpoint = (value: unknown): string => {
    const text = String(value ?? "").trim();
    if (!text) return "";
    const direct = idByName.get(text) ?? idByName.get(text.toLowerCase()) ?? idByName.get(normalizeAlias(text));
    if (direct) return direct;
    const entries = [...idByName.entries()];
    const byContains = entries.find(([name]) => name && (text.includes(name) || name.includes(text)));
    return byContains ? byContains[1] : "";
  };
  const readEndpoint = (relation: StageRelation, keys: string[]): string => {
    const record = relation as unknown as Record<string, unknown>;
    for (const key of keys) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value;
      if (value && typeof value === "object") {
        const nested = value as Record<string, unknown>;
        const idValue = nested["id"] ?? nested["name"];
        if (typeof idValue === "string" && idValue.trim()) return idValue;
      }
    }
    return "";
  };
  const relations = rawRelations
    .map((relation) => ({
      ...relation,
      source: resolveEndpoint(readEndpoint(relation, ["source", "sourceId", "from", "fromId", "source_object_type_id"])),
      target: resolveEndpoint(readEndpoint(relation, ["target", "targetId", "to", "toId", "target_object_type_id"])),
    }))
    .filter((relation) => relation.source && relation.target);
  const unmappedRelationCount = rawRelations.length - relations.length;
  if (unmappedRelationCount > 0) {
    warnings.push(`${unmappedRelationCount} 条关系未找到匹配的对象类，已丢弃`);
  }
  onProgress?.({
    stage: "relations",
    status: "done",
    detail: `${relations.length} 条关系${unmappedRelationCount > 0 ? `，${unmappedRelationCount} 条未匹配已丢弃` : ""}`,
    ms: stages
      .filter((item) => item.stage.startsWith("relations"))
      .reduce((sum, item) => sum + item.ms, 0),
  });
  if (relations.length === 0) {
    const retry = await runStage<{ relations?: StageRelation[] }>(
      token,
      model,
      modelId,
      "relations-retry",
      RELATION_PROMPT,
      `${relationInput}\n\n至少给出 3 条关系；source/target 必须直接复制对象清单里的 id 字符串。`,
      stages,
      onProgress,
    );
    if (retry.ok) {
      const retryRaw = pickArray(
        retry.value as Record<string, unknown>,
        ["relations", "relationTypes", "edges", "links"],
      ) as unknown as StageRelation[];
      for (const item of retryRaw) {
        const source = resolveEndpoint(String(item.source ?? ""));
        const target = resolveEndpoint(String(item.target ?? ""));
        if (source && target) relations.push({ ...item, source, target });
      }
      if (relations.length > 0) warnings.push("关系抽取重试后补齐");
    }
  }

  const actionInput = [
    "对象清单：",
    ...objectTypes.map((object) => `- ${object.id}（${object.name}）`),
    "",
    "关系清单：",
    ...relations.map((relation) => `- ${relation.name}：${relation.source} → ${relation.target}`),
  ].join("\n");

  onProgress?.({ stage: "actions", status: "running", detail: "结合对象与关系推导行动、风险" });
  const actionStage = await runStage<{
    actionTypes?: Record<string, unknown>[];
    riskTypes?: Record<string, unknown>[];
  }>(token, model, modelId, "actions", ACTION_PROMPT, actionInput, stages, onProgress);
  if (!actionStage.ok) {
    warnings.push(`行动/风险阶段失败：${actionStage.error}，已保留对象与关系`);
  }
  onProgress?.({
    stage: "actions",
    status: "done",
    ms: stages.filter((item) => item.stage === "actions").reduce((sum, item) => sum + item.ms, 0),
    detail: actionStage.ok ? "行动与风险已生成" : `失败：${actionStage.error}`,
  });

  const raw: Record<string, unknown> = {
    networkName: String(objectsStage.value.networkName ?? "").trim() || "未命名知识网络",
    description: String(objectsStage.value.description ?? "").trim(),
    objectTypes: objectTypes.map((object) => ({
      id: object.id,
      name: object.name,
      description: object.description ?? "",
      primaryKey: object.primaryKey,
      properties: (object.properties ?? []).map((property) => ({
        name: property.name,
        displayName: property.displayName ?? property.name,
        type: property.type ?? "string",
        description: property.description ?? "",
      })),
    })),
    relations: relations.map((relation) => ({
      id: relation.id,
      name: relation.name,
      source: relation.source,
      target: relation.target,
      description: relation.description ?? "",
      mapping: relation.mapping,
    })),
    actionTypes: (actionStage.ok
      ? pickArray(actionStage.value as Record<string, unknown>, ["actionTypes", "actions"])
      : []
    )
      .map((action) => {
        const rawType = String((action as Record<string, unknown>)["actionType"] ?? "")
          .trim()
          .toLowerCase();
        return {
          ...action,
          actionType: ACTION_TYPE_ALIAS[rawType] ?? "add",
          boundObject: resolveEndpoint(
            String(
              (action as Record<string, unknown>)["boundObject"] ??
                (action as Record<string, unknown>)["object"] ??
                "",
            ),
          ),
        };
      })
      .filter((action) => Boolean((action as Record<string, unknown>)["boundObject"])),
    riskTypes: actionStage.ok
      ? pickArray(actionStage.value as Record<string, unknown>, ["riskTypes", "risks"])
      : [],
    questions: [],
  };

  return { ok: true, raw, stages, warnings };
}

export const STAGED_EXTRACT_TIMEOUT = STAGE_TIMEOUT;
