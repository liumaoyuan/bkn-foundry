import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { chatViaPlatform } from "./platform-model.js";

const GATEWAY_BASE = process.env.GATEWAY_BASE ?? "http://gateway";
const SNAPSHOT_DIR = process.env.COPILOT_NETWORK_EDIT_DIR ?? join(process.cwd(), "data", "network-edits");

export type NetworkEditProperty = {
  name: string;
  displayName?: string;
  type?: string;
  description?: string;
};

export type NetworkEditObjectType = {
  id: string;
  name: string;
  data_source?: { type?: string; id?: string; name?: string } | null;
  data_properties?: { name?: string; display_name?: string; type?: string; comment?: string }[];
  [key: string]: unknown;
};

export type NetworkEditOp =
  | { op: "rename_object_type"; objectTypeId: string; newName: string }
  | {
      op: "add_property";
      objectTypeId: string;
      name: string;
      displayName?: string;
      type?: string;
      description?: string;
    }
  | {
      op: "update_property";
      objectTypeId: string;
      propertyName: string;
      displayName?: string;
      description?: string;
    }
  | {
      op: "add_object_type";
      id: string;
      name: string;
      primaryKey?: string;
      properties?: NetworkEditProperty[];
    }
  | {
      op: "add_relation_type";
      id: string;
      name: string;
      sourceObjectTypeId: string;
      targetObjectTypeId: string;
      sourceProperty: string;
      targetProperty: string;
    }
  | {
      op: "add_action_type";
      id: string;
      name: string;
      actionType: "add" | "modify" | "delete";
      objectTypeId: string;
    };

export type NetworkEditPlan = {
  reply: string;
  ops: NetworkEditOp[];
  warnings: string[];
};

export type NetworkEditResult = {
  applied: { op: NetworkEditOp; ok: boolean; error?: string; detail?: string }[];
  snapshotId?: string;
  warnings: string[];
};

type AuthOptions = { token: string };

function authHeaders(token: string, json = false): Record<string, string> {
  return {
    authorization: `Bearer ${token}`,
    ...(json ? { "content-type": "application/json" } : {}),
  };
}

async function getJson<T>(token: string, url: string): Promise<T> {
  const response = await fetch(url, { headers: authHeaders(token) });
  const text = await response.text();
  if (!response.ok) throw new Error(`GET ${url} 失败（HTTP ${response.status}）：${text.slice(0, 300)}`);
  return (text ? JSON.parse(text) : {}) as T;
}

async function sendJson<T>(
  token: string,
  method: "POST" | "PUT" | "DELETE",
  url: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: authHeaders(token, body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${method} ${url} 失败（HTTP ${response.status}）：${text.slice(0, 400)}`);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

const objectTypesUrl = (networkId: string, suffix = "") =>
  `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/object-types${suffix}`;
const relationTypesUrl = (networkId: string, suffix = "") =>
  `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/relation-types${suffix}`;
const actionTypesUrl = (networkId: string, suffix = "") =>
  `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/action-types${suffix}`;

export async function listNetworkObjectTypes(
  token: string,
  networkId: string,
): Promise<NetworkEditObjectType[]> {
  const payload = await getJson<{ entries?: NetworkEditObjectType[] }>(token, objectTypesUrl(networkId));
  return Array.isArray(payload.entries) ? payload.entries : [];
}

export async function getNetworkObjectType(
  token: string,
  networkId: string,
  objectTypeId: string,
): Promise<NetworkEditObjectType> {
  const payload = await getJson<{ entries?: NetworkEditObjectType[] }>(
    token,
    `${objectTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`,
  );
  const entry = payload.entries?.[0];
  if (!entry) throw new Error(`对象类型 ${objectTypeId} 不存在`);
  return entry;
}

export async function listNetworkRelationTypes(
  token: string,
  networkId: string,
): Promise<Record<string, unknown>[]> {
  const payload = await getJson<{ entries?: Record<string, unknown>[] }>(
    token,
    `${relationTypesUrl(networkId)}?limit=200&offset=0`,
  );
  return Array.isArray(payload.entries) ? payload.entries : [];
}

const PROPERTY_TYPES = new Set(["string", "number", "boolean", "datetime", "float", "integer"]);

function propertyPayload(property: NetworkEditProperty): Record<string, unknown> {
  const type = PROPERTY_TYPES.has(String(property.type ?? "")) ? String(property.type) : "string";
  return {
    name: property.name,
    display_name: property.displayName ?? property.name,
    type,
    ...(property.description ? { comment: property.description } : {}),
  };
}

function normalizeId(value: string, fallback: string): string {
  const text = String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!text) return fallback;
  return /^[a-z]/.test(text) ? text : `n_${text}`;
}

/** 读取线上网络结构，拼成给模型看的精简 schema。 */
export async function buildNetworkSchemaBrief(token: string, networkId: string): Promise<string> {
  const [listed, relationTypes] = await Promise.all([
    listNetworkObjectTypes(token, networkId),
    listNetworkRelationTypes(token, networkId),
  ]);
  const objectTypes = await Promise.all(
    listed.map((objectType) =>
      getNetworkObjectType(token, networkId, objectType.id).catch(() => objectType),
    ),
  );
  const lines: string[] = [];
  lines.push(`知识网络 ${networkId}`);
  lines.push("对象类型：");
  for (const objectType of objectTypes) {
    const properties = (objectType.data_properties ?? [])
      .map((property) => `${property.name}:${property.type ?? "string"}`)
      .join("、");
    const bound = objectType.data_source?.type === "resource" ? "（已绑定数据资源）" : "";
    lines.push(`- ${objectType.id}（${objectType.name}）${bound}：${properties || "无属性"}`);
  }
  lines.push("关系类型：");
  for (const relation of relationTypes) {
    const mapping = Array.isArray(relation.mapping_rules) ? relation.mapping_rules : [];
    const first = (mapping[0] ?? {}) as Record<string, unknown>;
    const source = (first.source_property as { name?: string } | undefined)?.name ?? "?";
    const target = (first.target_property as { name?: string } | undefined)?.name ?? "?";
    lines.push(
      `- ${String(relation.id)}（${String(relation.name)}）：${String(
        relation.source_object_type_id,
      )}.${source} → ${String(relation.target_object_type_id)}.${target}`,
    );
  }
  return lines.join("\n");
}

const EDIT_SYSTEM_PROMPT = `你是知识网络的结构编辑助手。用户会对「已上线」的知识网络提出修改要求。
你可以使用的操作（JSON 数组 ops，每项一个 op）：
1. {"op":"rename_object_type","objectTypeId":"现有id","newName":"新中文名"}
2. {"op":"add_property","objectTypeId":"现有id","name":"属性id","displayName":"中文名","type":"string|number|datetime|boolean"}
3. {"op":"update_property","objectTypeId":"现有id","propertyName":"属性id","displayName":"新中文名","description":"说明"}
4. {"op":"add_object_type","id":"小写英文id","name":"中文名","primaryKey":"主键属性id","properties":[{"name":"属性id","displayName":"中文名","type":"string"}]}
5. {"op":"add_relation_type","id":"小写英文id","name":"中文动词短语","sourceObjectTypeId":"id","targetObjectTypeId":"id","sourceProperty":"源属性","targetProperty":"目标属性"}
6. {"op":"add_action_type","id":"小写英文id","name":"中文动宾短语","actionType":"add|modify|delete","objectTypeId":"绑定对象id"}

规则：
- 只输出 JSON：{"reply":"给用户的中文说明（1-3 句）","ops":[...],"warnings":["需要提醒的风险"]}
- 引用的对象 id/属性名必须是 schema 中已存在的，或是在同一批 ops 里新增的；
- 改名只改显示名，不动 id；
- 不提供删除对象类/属性/关系的能力；用户要求删除时，在 reply 里说明当前版本不支持，并给出替代建议；
- 不要编造 schema 中不存在的对象 id。`;

export type NetworkEditProgress = {
  stage: "schema" | "model" | "op";
  status: "running" | "done";
  detail?: string;
  error?: string;
};

export async function planNetworkEdit(
  token: string,
  model: string,
  modelId: string | undefined,
  networkId: string,
  message: string,
  onProgress?: (event: NetworkEditProgress) => void,
): Promise<NetworkEditPlan> {
  onProgress?.({ stage: "schema", status: "running", detail: "读取当前网络结构" });
  const schema = await buildNetworkSchemaBrief(token, networkId);
  onProgress?.({
    stage: "schema",
    status: "done",
    detail: `${schema.split("\n").filter((line) => line.startsWith("- ")).length} 个对象/关系`,
  });
  onProgress?.({ stage: "model", status: "running", detail: "模型生成变更计划" });
  const content = await chatViaPlatform(
    token,
    model,
    [
      { role: "system", content: EDIT_SYSTEM_PROMPT },
      { role: "system", content: `当前网络结构：\n${schema}` },
      { role: "user", content: message.slice(0, 4000) },
    ],
    { modelId, maxTokens: 4096 },
  );
  const text = String(content ?? "").trim();
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) throw new Error("模型没有返回可解析的变更计划");
  const parsed = JSON.parse(text.slice(start, end + 1)) as {
    reply?: string;
    ops?: NetworkEditOp[];
    warnings?: string[];
  };
  const ops = Array.isArray(parsed.ops) ? parsed.ops : [];
  const warnings = Array.isArray(parsed.warnings) ? parsed.warnings.map(String) : [];
  onProgress?.({
    stage: "model",
    status: "done",
    detail: `生成 ${ops.length} 条变更`,
  });
  return {
    reply: String(parsed.reply ?? "").trim() || `已生成 ${ops.length} 条变更。`,
    ops,
    warnings,
  };
}

function snapshotPath(networkId: string, snapshotId: string): string {
  return join(SNAPSHOT_DIR, `${networkId}-${snapshotId}.json`);
}

type Snapshot = {
  networkId: string;
  snapshotId: string;
  createdAt: string;
  objects: Record<string, NetworkEditObjectType>;
  createdObjectTypes: string[];
  createdRelationTypes: string[];
  createdActionTypes: string[];
};

function writeSnapshot(snapshot: Snapshot): void {
  mkdirSync(SNAPSHOT_DIR, { recursive: true });
  writeFileSync(snapshotPath(snapshot.networkId, snapshot.snapshotId), JSON.stringify(snapshot, null, 2), "utf8");
}

function readSnapshot(networkId: string, snapshotId: string): Snapshot {
  const raw = readFileSync(snapshotPath(networkId, snapshotId), "utf8");
  return JSON.parse(raw) as Snapshot;
}

async function ensureProperty(
  token: string,
  networkId: string,
  objectTypeId: string,
  propertyName: string,
  snapshot: Snapshot,
): Promise<void> {
  const objectType = await getNetworkObjectType(token, networkId, objectTypeId);
  const exists = (objectType.data_properties ?? []).some((property) => property.name === propertyName);
  if (exists) return;
  snapshot.objects[objectTypeId] = snapshot.objects[objectTypeId] ?? objectType;
  const dataProperties = [...(objectType.data_properties ?? []), propertyPayload({ name: propertyName })];
  await sendJson(token, "PUT", `${objectTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`, {
    ...objectType,
    data_properties: dataProperties,
  });
}

export async function applyNetworkEdit(
  token: string,
  networkId: string,
  ops: NetworkEditOp[],
  onProgress?: (event: { index: number; total: number; op: NetworkEditOp }) => void,
): Promise<NetworkEditResult> {
  const snapshotId = `snap_${Date.now().toString(36)}`;
  const snapshot: Snapshot = {
    networkId,
    snapshotId,
    createdAt: new Date().toISOString(),
    objects: {},
    createdObjectTypes: [],
    createdRelationTypes: [],
    createdActionTypes: [],
  };
  const applied: NetworkEditResult["applied"] = [];
  const warnings: string[] = [];

  for (const [index, op] of ops.entries()) {
    onProgress?.({ index, total: ops.length, op });
    try {
      if (op.op === "rename_object_type") {
        const objectType = await getNetworkObjectType(token, networkId, op.objectTypeId);
        snapshot.objects[op.objectTypeId] = snapshot.objects[op.objectTypeId] ?? objectType;
        await sendJson(token, "PUT", `${objectTypesUrl(networkId)}/${encodeURIComponent(op.objectTypeId)}`, {
          ...objectType,
          name: op.newName,
        });
        applied.push({ op, ok: true, detail: `已改名为「${op.newName}」` });
        continue;
      }

      if (op.op === "add_property") {
        const objectType = await getNetworkObjectType(token, networkId, op.objectTypeId);
        snapshot.objects[op.objectTypeId] = snapshot.objects[op.objectTypeId] ?? objectType;
        const property = propertyPayload({
          name: normalizeId(op.name, `field_${Date.now()}`),
          displayName: op.displayName,
          type: op.type,
          description: op.description,
        });
        const dataProperties = [...(objectType.data_properties ?? []), property];
        await sendJson(token, "PUT", `${objectTypesUrl(networkId)}/${encodeURIComponent(op.objectTypeId)}`, {
          ...objectType,
          data_properties: dataProperties,
        });
        applied.push({ op, ok: true, detail: `已新增属性「${String(property.display_name)}」` });
        continue;
      }

      if (op.op === "update_property") {
        const objectType = await getNetworkObjectType(token, networkId, op.objectTypeId);
        snapshot.objects[op.objectTypeId] = snapshot.objects[op.objectTypeId] ?? objectType;
        const dataProperties = (objectType.data_properties ?? []).map((property) =>
          property.name === op.propertyName
            ? {
                ...property,
                ...(op.displayName ? { display_name: op.displayName } : {}),
                ...(op.description ? { comment: op.description } : {}),
              }
            : property,
        );
        await sendJson(token, "PUT", `${objectTypesUrl(networkId)}/${encodeURIComponent(op.objectTypeId)}`, {
          ...objectType,
          data_properties: dataProperties,
        });
        applied.push({ op, ok: true, detail: `已更新属性「${op.propertyName}」` });
        continue;
      }

      if (op.op === "add_object_type") {
        const id = normalizeId(op.id, `object_${Date.now().toString(36)}`);
        const properties = (op.properties ?? [])
          .filter((property) => property.name)
          .map((property) => propertyPayload(property));
        const primaryKey =
          op.primaryKey && properties.some((property) => property.name === op.primaryKey)
            ? op.primaryKey
            : String(properties[0]?.name ?? "id");
        await sendJson(token, "POST", objectTypesUrl(networkId), {
          entries: [
            {
              id,
              name: op.name,
              primary_keys: primaryKey ? [primaryKey] : [],
              display_key: primaryKey,
              data_properties: properties,
            },
          ],
        });
        snapshot.createdObjectTypes.push(id);
        applied.push({ op, ok: true, detail: `已新增对象类「${op.name}」（${id}）` });
        continue;
      }

      if (op.op === "add_relation_type") {
        await ensureProperty(token, networkId, op.sourceObjectTypeId, op.sourceProperty, snapshot);
        await ensureProperty(token, networkId, op.targetObjectTypeId, op.targetProperty, snapshot);
        const id = normalizeId(op.id, `rel_${Date.now().toString(36)}`);
        await sendJson(token, "POST", relationTypesUrl(networkId), {
          entries: [
            {
              id,
              name: op.name,
              type: "direct",
              source_object_type_id: op.sourceObjectTypeId,
              target_object_type_id: op.targetObjectTypeId,
              mapping_rules: [
                {
                  source_property: { name: op.sourceProperty },
                  target_property: { name: op.targetProperty },
                },
              ],
            },
          ],
        });
        snapshot.createdRelationTypes.push(id);
        applied.push({ op, ok: true, detail: `已新增关系「${op.name}」` });
        continue;
      }

      if (op.op === "add_action_type") {
        const id = normalizeId(op.id, `action_${Date.now().toString(36)}`);
        await sendJson(token, "POST", actionTypesUrl(networkId), {
          entries: [
            {
              id,
              name: op.name,
              action_type: op.actionType,
              object_type_id: op.objectTypeId,
            },
          ],
        });
        snapshot.createdActionTypes.push(id);
        applied.push({ op, ok: true, detail: `已新增行动「${op.name}」` });
        continue;
      }

      applied.push({ op, ok: false, error: "不支持的操作类型" });
    } catch (error) {
      applied.push({ op, ok: false, error: error instanceof Error ? error.message : "执行失败" });
    }
  }

  writeSnapshot(snapshot);
  return { applied, snapshotId, warnings };
}

export async function rollbackNetworkEdit(
  token: string,
  networkId: string,
  snapshotId: string,
): Promise<{ restored: string[]; warnings: string[] }> {
  const snapshot = readSnapshot(networkId, snapshotId);
  const restored: string[] = [];
  const warnings: string[] = [];

  for (const objectTypeId of snapshot.createdRelationTypes) {
    try {
      await sendJson(token, "DELETE", `${relationTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`);
      restored.push(`删除关系 ${objectTypeId}`);
    } catch (error) {
      warnings.push(`删除关系 ${objectTypeId} 失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }
  for (const objectTypeId of snapshot.createdActionTypes) {
    try {
      await sendJson(token, "DELETE", `${actionTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`);
      restored.push(`删除行动 ${objectTypeId}`);
    } catch (error) {
      warnings.push(`删除行动 ${objectTypeId} 失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }
  for (const objectTypeId of snapshot.createdObjectTypes) {
    try {
      await sendJson(token, "DELETE", `${objectTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`);
      restored.push(`删除对象类 ${objectTypeId}`);
    } catch (error) {
      warnings.push(`删除对象类 ${objectTypeId} 失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }
  for (const [objectTypeId, before] of Object.entries(snapshot.objects)) {
    if (snapshot.createdObjectTypes.includes(objectTypeId)) continue;
    try {
      await sendJson(token, "PUT", `${objectTypesUrl(networkId)}/${encodeURIComponent(objectTypeId)}`, before);
      restored.push(`恢复对象类 ${objectTypeId}`);
    } catch (error) {
      warnings.push(`恢复对象类 ${objectTypeId} 失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  return { restored, warnings };
}

export type { AuthOptions };
