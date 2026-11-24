import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { extractRecords } from "./extract.js";

/**
 * 数据导入（知识网络「导入数据」功能）：
 * - 数据源：平台「数据入库」产出的数据集（tabular/document，记录为 JSON payload）
 * - 处理：字段画像 -> 映射到本体对象类型/属性 -> 类型转换/校验 -> 生成实例文档
 * - 落地：在平台创建 dataset 资源（catalog_id = data-ingestion）-> 写入文档 -> 绑定对象类型 data_source
 */

const DATA_BASE = (process.env.COPILOT_DATA_INGESTION_URL ?? "http://data-ingestion:8000").replace(/\/+$/, "");
const GATEWAY_BASE = (process.env.COPILOT_PLATFORM_GATEWAY_URL ?? "http://gateway").replace(/\/+$/, "");
const HISTORY_PATH = process.env.COPILOT_IMPORT_HISTORY_PATH ?? resolve(process.cwd(), "data", "data-imports.json");
const MAX_RECORDS = Number(process.env.COPILOT_IMPORT_MAX_RECORDS ?? 20000);
const DATASET_CATALOG_ID = process.env.COPILOT_DATASET_CATALOG_ID ?? "copilot_import_catalog";

export type DataSpace = { id: string; name: string; databaseLabel: string; isDefault: boolean };

export type DataDataset = {
  id: string;
  name: string;
  dataType: string;
  description: string;
  recordCount: number;
  targetTable: string;
  lastIngestedAt?: string;
};

export type ImportFieldProfile = {
  name: string;
  type: "string" | "number" | "boolean" | "datetime" | "array" | "object";
  distinctCount: number;
  nullRate: number;
  samples: string[];
};

export type DatasetProfile = {
  dataset: DataDataset;
  total: number;
  sampled: number;
  fields: ImportFieldProfile[];
};

export type FieldMapping = {
  sourceColumn: string;
  propertyName: string;
  type: "string" | "number" | "boolean" | "datetime";
};

export type ObjectMapping = {
  objectTypeId: string;
  objectTypeName: string;
  primaryKeyColumn?: string;
  fields: FieldMapping[];
};

export type ImportMapping = {
  objects: ObjectMapping[];
};

export type ImportWriteMode = "replace" | "upsert";

export type ImportWatermark = { field: string; value?: string };

export type ImportOptions = {
  writeMode?: ImportWriteMode;
  watermark?: ImportWatermark;
};

export type TransformIssue = { row: number; field?: string; message: string };

export type TransformResult = {
  documents: Record<string, Array<Record<string, unknown>>>;
  issues: TransformIssue[];
  stats: { rows: number; documents: number; skipped: number; issueCount: number; deduped?: number };
};

export type RelationEdgeSample = { sourceId: string; targetId: string };

export type ReportRelation = {
  relationTypeId: string;
  relationTypeName: string;
  sourceObjectTypeId: string;
  targetObjectTypeId: string;
  edgeCount: number;
  samples: RelationEdgeSample[];
  note?: string;
};

export type ImportReport = {
  ok: boolean;
  networkId: string;
  datasetId: string;
  startedAt: string;
  finishedAt: string;
  elapsedMs: number;
  objects: Array<{
    objectTypeId: string;
    objectTypeName: string;
    resourceId?: string;
    resourceName?: string;
    written: number;
    failed: number;
    bound: boolean;
    watermark?: string;
    error?: string;
  }>;
  relations?: ReportRelation[];
  issues: TransformIssue[];
  writeMode?: ImportWriteMode;
  watermark?: ImportWatermark;
  sourceLabel?: string;
  error?: string;
};

function authHeaders(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

async function getJson<T>(url: string, token: string, timeoutMs = 60000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { headers: authHeaders(token), signal: controller.signal });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`GET ${url} 失败（HTTP ${res.status}）：${text.slice(0, 300)}`);
    }
    return JSON.parse(text) as T;
  } finally {
    clearTimeout(timer);
  }
}

async function sendJson<T>(
  method: "POST" | "PUT",
  url: string,
  token: string,
  body: unknown,
  timeoutMs = 60000,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method,
      headers: { ...authHeaders(token), "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`${method} ${url} 失败（HTTP ${res.status}）：${text.slice(0, 300)}`);
    }
    return text ? (JSON.parse(text) as T) : ({} as T);
  } finally {
    clearTimeout(timer);
  }
}

export async function listDataSpaces(token: string): Promise<DataSpace[]> {
  const raw = await getJson<Array<Record<string, unknown>>>(`${DATA_BASE}/api/spaces`, token, 20000);
  return (Array.isArray(raw) ? raw : []).map((item) => ({
    id: String(item.id ?? ""),
    name: String(item.name ?? item.id ?? ""),
    databaseLabel: String(item.database_label ?? ""),
    isDefault: Boolean(item.is_default),
  }));
}

export async function listDataDatasets(token: string, spaceId: string): Promise<DataDataset[]> {
  const url = `${DATA_BASE}/api/datasets?space_id=${encodeURIComponent(spaceId)}`;
  const raw = await getJson<Array<Record<string, unknown>>>(url, token, 30000);
  return (Array.isArray(raw) ? raw : []).map((item) => ({
    id: String(item.id ?? ""),
    name: String(item.name ?? ""),
    dataType: String(item.data_type ?? ""),
    description: String(item.description ?? ""),
    recordCount: Number(item.record_count ?? 0),
    targetTable: String(item.target_table ?? ""),
    lastIngestedAt: item.last_ingested_at ? String(item.last_ingested_at) : undefined,
  }));
}

function normalizeRecord(raw: Record<string, unknown>): Record<string, unknown> {
  const payload = raw.payload;
  let flattened: Record<string, unknown> | null = null;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    flattened = payload as Record<string, unknown>;
  } else if (typeof payload === "string" && payload.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(payload);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) flattened = parsed as Record<string, unknown>;
    } catch {
      flattened = null;
    }
  }
  if (!flattened) return raw;
  return { ...flattened };
}

export async function fetchDatasetRecords(
  token: string,
  spaceId: string,
  datasetId: string,
  page = 1,
  pageSize = 200,
): Promise<{ total: number; items: Array<Record<string, unknown>> }> {
  const url = `${DATA_BASE}/api/datasets/${encodeURIComponent(datasetId)}/records?space_id=${encodeURIComponent(
    spaceId,
  )}&page=${page}&page_size=${Math.min(pageSize, 200)}`;
  const raw = await getJson<{ total?: number; items?: Array<Record<string, unknown>> }>(url, token, 60000);
  const items = (Array.isArray(raw.items) ? raw.items : []).map((item) => normalizeRecord(item));
  return { total: Number(raw.total ?? 0), items };
}

function inferValueType(values: unknown[]): ImportFieldProfile["type"] {
  let numbers = 0;
  let dates = 0;
  let booleans = 0;
  let arrays = 0;
  let objects = 0;
  let filled = 0;
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    filled += 1;
    if (Array.isArray(value)) {
      arrays += 1;
      continue;
    }
    if (typeof value === "object") {
      objects += 1;
      continue;
    }
    const text = String(value).trim();
    if (/^-?\d+(\.\d+)?$/.test(text)) numbers += 1;
    else if (/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?/.test(text)) dates += 1;
    else if (/^(true|false|是|否|yes|no)$/i.test(text)) booleans += 1;
  }
  const base = Math.max(filled, 1);
  if (arrays / base > 0.3) return "array";
  if (objects / base > 0.3) return "object";
  if (numbers / base > 0.8) return "number";
  if (dates / base > 0.8) return "datetime";
  if (booleans / base > 0.8) return "boolean";
  return "string";
}

export function profileRecords(items: Array<Record<string, unknown>>): ImportFieldProfile[] {
  const fieldNames: string[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    for (const key of Object.keys(item)) {
      if (key === "dataset_id") continue;
      if (seen.has(key)) continue;
      seen.add(key);
      fieldNames.push(key);
    }
  }
  return fieldNames.map((name) => {
    const values = items.map((item) => item[name]);
    const filled = values.filter((value) => value !== null && value !== undefined && value !== "");
    const distinct = new Set(filled.map((value) => JSON.stringify(value)));
    return {
      name,
      type: inferValueType(values),
      distinctCount: distinct.size,
      nullRate: items.length === 0 ? 0 : 1 - filled.length / items.length,
      samples: filled.slice(0, 3).map((value) => (typeof value === "string" ? value : JSON.stringify(value))).slice(0, 3),
    };
  });
}

export async function profileDataset(
  token: string,
  spaceId: string,
  dataset: DataDataset,
  pageSize = 200,
): Promise<DatasetProfile> {
  const page = await fetchDatasetRecords(token, spaceId, dataset.id, 1, pageSize);
  return {
    dataset,
    total: page.total,
    sampled: page.items.length,
    fields: profileRecords(page.items),
  };
}

function coerceValue(value: unknown, type: FieldMapping["type"]): unknown {
  if (value === null || value === undefined || value === "") return null;
  switch (type) {
    case "number": {
      const parsed = typeof value === "number" ? value : Number(String(value).replace(/,/g, "").trim());
      return Number.isFinite(parsed) ? parsed : null;
    }
    case "boolean": {
      const text = String(value).trim().toLowerCase();
      return ["true", "1", "yes", "是", "y"].includes(text);
    }
    case "datetime": {
      const text = String(value).trim();
      if (!text) return null;
      const normalized = text.replace(/[./]/g, "-").replace(" ", "T");
      const parsed = new Date(normalized);
      return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
    }
    default:
      return String(value);
  }
}

export function transformRecords(items: Array<Record<string, unknown>>, mapping: ImportMapping): TransformResult {
  const documents: Record<string, Array<Record<string, unknown>>> = {};
  const issues: TransformIssue[] = [];
  let counts = 0;
  let skipped = 0;
  for (const objectMapping of mapping.objects) {
    documents[objectMapping.objectTypeId] = documents[objectMapping.objectTypeId] ?? [];
  }
  items.forEach((row, index) => {
    for (const objectMapping of mapping.objects) {
      const doc: Record<string, unknown> = {};
      let hasValue = false;
      for (const field of objectMapping.fields) {
        const value = coerceValue(row[field.sourceColumn], field.type);
        doc[field.propertyName] = value;
        if (value !== null && !/^ref$/i.test(field.propertyName)) hasValue = true;
      }
      if (!hasValue) {
        skipped += 1;
        continue;
      }
      const pk = objectMapping.primaryKeyColumn ? row[objectMapping.primaryKeyColumn] : undefined;
      if (pk !== undefined && pk !== null && pk !== "") {
        doc.id = String(pk);
      } else {
        doc.id = `${objectMapping.objectTypeName || objectMapping.objectTypeId}_${index + 1}`.replace(/\s+/g, "_");
      }
      if (doc.id === undefined || doc.id === "") {
        issues.push({ row: index + 1, message: "缺少主键值，已跳过该行" });
        skipped += 1;
        continue;
      }
      documents[objectMapping.objectTypeId].push(doc);
      counts += 1;
    }
  });
  let deduped = 0;
  for (const objectTypeId of Object.keys(documents)) {
    const seen = new Map<string, Record<string, unknown>>();
    for (const doc of documents[objectTypeId]) {
      const key = String(doc.id);
      if (seen.has(key)) deduped += 1;
      seen.set(key, doc);
    }
    documents[objectTypeId] = Array.from(seen.values());
  }
  counts = Object.values(documents).reduce((sum, docs) => sum + docs.length, 0);

  return {
    documents,
    issues,
    stats: { rows: items.length, documents: counts, skipped, issueCount: issues.length, deduped },
  };
}

type PlatformObjectTypeProperty = {
  name: string;
  display_name?: string;
  type?: string;
  comment?: string;
  mapped_field?: { name?: string } | null;
};

type PlatformObjectType = {
  id: string;
  name: string;
  data_source?: { type?: string; id?: string; name?: string } | null;
  data_properties?: PlatformObjectTypeProperty[];
  [key: string]: unknown;
};

async function listObjectTypes(token: string, networkId: string): Promise<PlatformObjectType[]> {
  const raw = await getJson<{ entries?: PlatformObjectType[] }>(
    `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/object-types`,
    token,
  );
  return Array.isArray(raw.entries) ? raw.entries : [];
}

async function getObjectType(token: string, networkId: string, objectTypeId: string): Promise<PlatformObjectType> {
  const raw = await getJson<{ entries?: PlatformObjectType[] }>(
    `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(
      networkId,
    )}/object-types/${encodeURIComponent(objectTypeId)}`,
    token,
  );
  const entry = Array.isArray(raw.entries) ? raw.entries[0] : undefined;
  if (!entry) throw new Error(`对象类型 ${objectTypeId} 读取失败`);
  return entry;
}

type VegaResource = { id?: string; name?: string };

async function readCatalogBuiltIn(token: string, catalogId: string): Promise<boolean> {
  const res = await fetch(`${GATEWAY_BASE}/api/vega-backend/v1/catalogs/${encodeURIComponent(catalogId)}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!res.ok) return true;
  const data = (await res.json()) as {
    built_in?: boolean;
    entry?: { built_in?: boolean };
    entries?: Array<{ built_in?: boolean }>;
  };
  const entry = data.entry ?? data.entries?.[0];
  return Boolean(data.built_in ?? entry?.built_in ?? false);
}

async function ensureImportCatalog(token: string): Promise<string> {
  const catalogId = DATASET_CATALOG_ID;
  const res = await fetch(`${GATEWAY_BASE}/api/vega-backend/v1/catalogs/${encodeURIComponent(catalogId)}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (res.ok) return catalogId;
  if (res.status !== 404) {
    const text = await res.text();
    throw new Error(`读取导入目录失败（HTTP ${res.status}）：${text.slice(0, 200)}`);
  }
  await sendJson(
    "POST",
    `${GATEWAY_BASE}/api/vega-backend/v1/catalogs`,
    token,
    {
      id: catalogId,
      name: catalogId,
      tags: [],
      description: "智能构建「导入数据」使用的资源目录（非内置，供知识网络托管代理读取）",
      enabled: true,
      connector_type: "",
      connector_config: {},
    },
    60000,
  );
  return catalogId;
}

function vegaFieldType(type?: string): string {
  switch ((type ?? "").toLowerCase()) {
    case "integer":
    case "unsigned integer":
      return "integer";
    case "float":
    case "number":
    case "double":
      return "float";
    case "boolean":
      return "boolean";
    case "datetime":
    case "timestamp":
      return "datetime";
    case "json":
      return "json";
    default:
      return "string";
  }
}

function extraSchemaFields(fields: FieldMapping[], objectType: PlatformObjectType) {
  const mapped = new Set(["id", ...fields.map((field) => field.propertyName)]);
  const usedColumns = new Set(mapped);
  const usedDisplayNames = new Set(["ID"]);
  const result: Array<Record<string, unknown>> = [];
  for (const property of objectType.data_properties ?? []) {
    const column = property.name;
    if (!column || usedColumns.has(column)) continue;
    usedColumns.add(column);
    let displayName = property.display_name ?? column;
    if (usedDisplayNames.has(displayName)) displayName = column;
    if (usedDisplayNames.has(displayName)) displayName = column + "_" + result.length;
    usedDisplayNames.add(displayName);
    result.push({
      name: column,
      display_name: displayName,
      type: vegaFieldType(property.type),
      description: property.comment ?? "",
      original_name: column,
      features: [],
      attributes: {},
    });
  }
  return result;
}

async function createDatasetResource(
  token: string,
  name: string,
  description: string,
  fields: FieldMapping[],
  objectType: PlatformObjectType,
): Promise<VegaResource> {
  const catalogId = await ensureImportCatalog(token);
  const catalogBuiltIn = await readCatalogBuiltIn(token, catalogId);
  const body = {
    catalog_id: catalogId,
    built_in: catalogBuiltIn,
    category: "dataset",
    name,
    description,
    enabled: true,
    status: "active",
    schema_definition: (() => {
      const seen = new Set<string>();
      const list: Array<Record<string, unknown>> = [];
      const push = (name: string, displayName: string, type: string, originalName: string) => {
        const key = name.trim();
        if (!key || seen.has(key)) return;
        seen.add(key);
        list.push({
          name: key,
          display_name: displayName || key,
          type,
          description: "",
          original_name: originalName,
          features: [],
          attributes: {},
        });
      };
      push("id", "ID", "string", "id");
      for (const field of fields) {
        push(
          field.propertyName,
          field.propertyName,
          field.type === "number" ? "float" : field.type === "datetime" ? "datetime" : field.type === "boolean" ? "boolean" : "string",
          field.sourceColumn,
        );
      }
      for (const extra of extraSchemaFields(fields, objectType)) {
        push(
          String((extra as Record<string, unknown>).name ?? ""),
          String((extra as Record<string, unknown>).display_name ?? ""),
          String((extra as Record<string, unknown>).type ?? "string"),
          String((extra as Record<string, unknown>).original_name ?? ""),
        );
      }
      return list;
    })(),
  };
  return sendJson<VegaResource>("POST", `${GATEWAY_BASE}/api/vega-backend/v1/resources`, token, body, 60000);
}

async function deleteDocumentsByKeys(token: string, resourceId: string, ids: string[]): Promise<void> {
  const unique = Array.from(new Set(ids.map((id) => id.trim()).filter(Boolean)));
  if (unique.length === 0) return;
  for (let index = 0; index < unique.length; index += 100) {
    const chunk = unique.slice(index, index + 100);
    const url = `${GATEWAY_BASE}/api/vega-backend/v1/resources/${encodeURIComponent(resourceId)}/data`;
    const res = await fetch(url, {
      method: "POST",
      headers: {
        ...authHeaders(token),
        "content-type": "application/json",
        "x-http-method-override": "DELETE",
      },
      body: JSON.stringify({ filter_condition: { field: "id", operation: "in", value: chunk } }),
    });
    if (!res.ok && res.status !== 404 && res.status !== 204) {
      const text = await res.text();
      throw new Error(`按主键清理旧文档失败（HTTP ${res.status}）：${text.slice(0, 300)}`);
    }
  }
}

async function writeDocument(token: string, resourceId: string, doc: Record<string, unknown>): Promise<void> {
  const url = `${GATEWAY_BASE}/api/vega-backend/v1/resources/${encodeURIComponent(resourceId)}/data`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      ...authHeaders(token),
      "content-type": "application/json",
      "x-http-method-override": "POST",
    },
    body: JSON.stringify(doc),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`写入文档失败（HTTP ${res.status}）：${text.slice(0, 300)}`);
  }
}

async function bindObjectType(
  token: string,
  networkId: string,
  objectType: PlatformObjectType,
  resource: VegaResource,
  fields: FieldMapping[],
): Promise<void> {
  const fieldTypes = new Map(fields.map((field) => [field.propertyName, field.type]));
  const properties = Array.isArray(objectType.data_properties) ? objectType.data_properties : [];
  const nextProperties = properties.map((property) => ({
    ...property,
    mapped_field: {
      name: property.name,
      type: property.type ?? fieldTypes.get(property.name) ?? "string",
      display_name: property.display_name ?? property.name,
    },
  }));
  const payload = {
    ...objectType,
    data_properties: nextProperties,
    data_source: { type: "resource", id: resource.id, name: resource.name },
  };
  await sendJson(
    "PUT",
    `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/object-types/${encodeURIComponent(
      objectType.id,
    )}`,
    token,
    payload,
    60000,
  );
}

export async function suggestMapping(
  token: string,
  spaceId: string,
  dataset: DataDataset,
  networkId: string,
) {
  const profile = await profileDataset(token, spaceId, dataset);
  return suggestMappingByProfile(token, networkId, profile);
}

export async function suggestMappingByProfile(
  token: string,
  networkId: string,
  profile: DatasetProfile,
) {
  const objectTypes = await listObjectTypes(token, networkId);
  const detailed: Array<{
    id: string;
    name: string;
    primaryKey?: string;
    properties: Array<{ name: string; displayName: string; type: string }>;
  }> = [];
  for (const objectType of objectTypes) {
    const full = await getObjectType(token, networkId, objectType.id);
    const raw = full as Record<string, unknown>;
    const properties = Array.isArray(raw.data_properties)
      ? (raw.data_properties as Array<Record<string, unknown>>)
      : [];
    const primaryKeys = Array.isArray(raw.primary_keys) ? (raw.primary_keys as unknown[]) : [];
    detailed.push({
      id: objectType.id,
      name: objectType.name,
      primaryKey: primaryKeys.length > 0 ? String(primaryKeys[0] ?? "") : undefined,
      properties: properties.map((property) => ({
        name: String(property.name ?? ""),
        displayName: String(property.display_name ?? property.name ?? ""),
        type: String(property.type ?? "string"),
      })),
    });
  }
  const warnings: string[] = [];
  const normalized = (value: string) => value.toLowerCase().replace(/[\s_\-()（）:：]/g, "");
  const columns = profile.fields;

  type Candidate = { objectType: (typeof detailed)[number]; fields: FieldMapping[]; score: number };
  const candidates: Candidate[] = [];
  for (const candidate of detailed) {
    const fields: FieldMapping[] = [];
    for (const property of candidate.properties) {
      const match = columns.find(
        (field) =>
          normalized(field.name) === normalized(property.name) ||
          normalized(field.name) === normalized(property.displayName),
      );
      if (!match) continue;
      fields.push({
        sourceColumn: match.name,
        propertyName: property.name,
        type: match.type === "number" ? "number" : match.type === "datetime" ? "datetime" : match.type === "boolean" ? "boolean" : "string",
      });
    }
    const matchesPrimaryKey =
      Boolean(candidate.primaryKey) && fields.some((field) => field.propertyName === candidate.primaryKey);
    if (fields.length >= 3 || (fields.length >= 2 && matchesPrimaryKey)) {
      candidates.push({ objectType: candidate, fields, score: fields.length });
    }
  }
  candidates.sort((left, right) => right.score - left.score);
  const picked = candidates.slice(0, 3);

  if (picked.length === 0) {
    const fallback = detailed[0];
    if (!fallback) {
      return { profile, objectTypes: detailed, mapping: { objects: [] }, warnings: ["目标知识网络没有对象类型"] };
    }
    const fields: FieldMapping[] = [];
    for (const field of columns.slice(0, Math.min(columns.length, fallback.properties.length))) {
      const property = fallback.properties[fields.length];
      if (!property) break;
      fields.push({
        sourceColumn: field.name,
        propertyName: property.name,
        type: field.type === "number" ? "number" : field.type === "datetime" ? "datetime" : "string",
      });
    }
    const primaryKeyColumn =
      profile.fields.find((field) => /(^id$|_id$|_code$|编号|编码|代码)/i.test(field.name) && field.distinctCount === profile.sampled)?.name ??
      profile.fields.find(
        (field) =>
          field.distinctCount === profile.sampled && field.type !== "datetime" && field.type !== "number",
      )?.name;
    warnings.push("字段名与本体属性差异较大，已按顺序兜底映射，请人工确认");
    return {
      profile,
      objectTypes: detailed,
      mapping: {
        objects: [
          {
            objectTypeId: fallback.id,
            objectTypeName: fallback.name,
            primaryKeyColumn,
            fields,
          },
        ],
      },
      warnings,
    };
  }

  const objects: ObjectMapping[] = picked.map(({ objectType, fields }) => {
    const declaredKey = objectType.primaryKey
      ? fields.find((field) => field.propertyName === objectType.primaryKey)
      : undefined;
    const businessKey = fields.find((field) => /(_id$|_code$|_no$|^id$|^code$|^no$)/i.test(field.propertyName));
    const headerKey = fields.find((field) => /(^id$|_id$|^code$|_code$|_no$|编号|编码|代码|序号)/i.test(field.sourceColumn));
    const uniqueColumn = columns.find(
      (field) =>
        field.distinctCount === profile.sampled &&
        field.distinctCount > 0 &&
        field.type !== "datetime" &&
        field.type !== "number",
    )?.name;
    return {
      objectTypeId: objectType.id,
      objectTypeName: objectType.name,
      primaryKeyColumn: declaredKey?.sourceColumn ?? businessKey?.sourceColumn ?? headerKey?.sourceColumn ?? uniqueColumn,
      fields,
    };
  });
  if (picked.length > 1) {
    warnings.push("数据中匹配到 " + String(picked.length) + " 个对象类型，已分别映射，请确认是否都需要导入");
  }
  return { profile, objectTypes: detailed, mapping: { objects }, warnings };
}

type ImportSource = { datasetId?: string; sourceLabel: string };

function watermarkText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function compareWatermark(left: unknown, right: string): number {
  const leftText = watermarkText(left);
  if (!leftText) return -1;
  const leftNumber = Number(leftText);
  const rightNumber = Number(right);
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) return leftNumber - rightNumber;
  const leftTime = Date.parse(leftText);
  const rightTime = Date.parse(right);
  if (Number.isFinite(leftTime) && Number.isFinite(rightTime)) return leftTime - rightTime;
  return leftText < right ? -1 : leftText > right ? 1 : 0;
}

function maxWatermark(values: string[]): string | undefined {
  let max: string | undefined;
  for (const value of values) {
    if (!value) continue;
    if (!max || compareWatermark(value, max) > 0) max = value;
  }
  return max;
}

function computeObjectWatermark(
  docs: Array<Record<string, unknown>>,
  objectMapping: ObjectMapping,
  field: string,
): string | undefined {
  const mapped = objectMapping.fields.find(
    (item) => item.sourceColumn === field || item.propertyName === field,
  );
  const propertyName = mapped?.propertyName ?? field;
  return maxWatermark(docs.map((doc) => watermarkText(doc[propertyName])).filter(Boolean));
}

export function watermarkCandidates(profile: DatasetProfile): string[] {
  return profile.fields
    .filter((item) => item.type === "datetime" || item.type === "number")
    .map((item) => item.name);
}

export function findLastImport(
  networkId: string,
  sourceLabel: string,
): { finishedAt: string; writeMode?: ImportWriteMode; watermark?: ImportWatermark } | undefined {
  const matched = readHistoryFile().find(
    (item) => item.networkId === networkId && item.sourceLabel === sourceLabel,
  );
  if (!matched) return undefined;
  return {
    finishedAt: matched.finishedAt,
    writeMode: matched.writeMode,
    watermark: matched.watermark,
  };
}

async function runImport(
  token: string,
  networkId: string,
  mapping: ImportMapping,
  items: Array<Record<string, unknown>>,
  source: ImportSource,
  options: ImportOptions = {},
): Promise<ImportReport> {
  const startedAt = new Date();
  const requestedUpsert = options.writeMode === "upsert";
  const watermarkField = options.watermark?.field;
  const lastImport = watermarkField ? findLastImport(networkId, source.sourceLabel) : undefined;
  const watermarkValue = options.watermark?.value || lastImport?.watermark?.value || "";
  const writeMode: ImportWriteMode = requestedUpsert ? "upsert" : "replace";
  const watermark = watermarkField ? { field: watermarkField, value: watermarkValue } : undefined;
  const filtered =
    writeMode === "upsert" && watermark?.value
      ? items.filter((item) => compareWatermark(item[watermark.field], watermark.value as string) > 0)
      : items;
  const report: ImportReport = {
    ok: false,
    networkId,
    datasetId: source.datasetId ?? "",
    startedAt: startedAt.toISOString(),
    finishedAt: startedAt.toISOString(),
    elapsedMs: 0,
    objects: [],
    issues: [],
    writeMode,
    watermark: writeMode === "upsert" ? watermark : undefined,
    sourceLabel: source.sourceLabel,
  };
  try {
    const transformed = transformRecords(filtered, mapping);
    report.issues = transformed.issues.slice(0, 200);
    if (requestedUpsert && !watermarkValue) {
      report.issues.push({ row: 0, message: "未提供水位：本次按主键 upsert（删除同主键旧文档后写入）" });
    }
    const objectTypes = await listObjectTypes(token, networkId);
    const previousResources = new Map<string, string>();
    for (const item of listImportHistory()) {
      if (item.networkId !== networkId) continue;
      for (const obj of item.objects ?? []) {
        if (obj.resourceId) previousResources.set(`${item.networkId}:${obj.objectTypeId}`, obj.resourceId);
      }
    }
    for (const objectMapping of mapping.objects) {
      const entry: ImportReport["objects"][number] = {
        objectTypeId: objectMapping.objectTypeId,
        objectTypeName: objectMapping.objectTypeName,
        written: 0,
        failed: 0,
        bound: false,
      };
      report.objects.push(entry);
      const docs = transformed.documents[objectMapping.objectTypeId] ?? [];
      if (docs.length === 0) {
        entry.error = "没有可写入的记录（映射结果为空）";
        continue;
      }
      try {
        const key = `${networkId}:${objectMapping.objectTypeId}`;
        const previousId = previousResources.get(key);
        const reuseResource = writeMode === "upsert" && Boolean(previousId);
        if (previousId && !reuseResource) {
          await fetch(`${GATEWAY_BASE}/api/vega-backend/v1/resources/${encodeURIComponent(previousId as string)}`, {
            method: "DELETE",
            headers: { authorization: `Bearer ${token}` },
          }).catch(() => undefined);
        }
        let resource: { id?: string; name?: string };
        if (reuseResource) {
          resource = { id: previousId, name: previousId };
        } else {
          const resourceName = `${networkId}_${objectMapping.objectTypeId}_import_${Date.now()}`.toLowerCase();
          resource = await createDatasetResource(
            token,
            resourceName,
            `由「导入数据」从${source.sourceLabel}生成`,
            objectMapping.fields,
            await getObjectType(token, networkId, objectMapping.objectTypeId),
          );
        }
        entry.resourceId = resource.id;
        entry.resourceName = resource.name ?? String(resource.id ?? "");
        if (!resource.id) throw new Error("创建数据资源失败：未返回资源 ID");
        if (writeMode === "upsert") {
          const ids = docs.map((doc) => String(doc.id ?? "")).filter((id) => id.length > 0);
          await deleteDocumentsByKeys(token, resource.id, ids);
        }
        const concurrency = 4;
        for (let index = 0; index < docs.length; index += concurrency) {
          const chunk = docs.slice(index, index + concurrency);
          const results = await Promise.allSettled(chunk.map((doc) => writeDocument(token, resource.id as string, doc)));
          results.forEach((result) => {
            if (result.status === "fulfilled") entry.written += 1;
            else {
              entry.failed += 1;
              if (!entry.error) {
                const reason = result.reason as unknown;
                entry.error = reason instanceof Error ? reason.message : String(reason);
              }
            }
          });
        }
        if (writeMode === "upsert" && watermark) {
          entry.watermark = computeObjectWatermark(docs, objectMapping, watermark.field);
        }
        const target = objectTypes.find((item) => item.id === objectMapping.objectTypeId);
        if (target) {
          const full = await getObjectType(token, networkId, target.id);
          await bindObjectType(token, networkId, full, resource, objectMapping.fields);
          entry.bound = true;
        }
      } catch (error) {
        entry.error = error instanceof Error ? error.message : String(error);
      }
    }
    const finishedAt = new Date();
    report.ok = report.objects.every((item) => !item.error);
    report.finishedAt = finishedAt.toISOString();
    report.elapsedMs = finishedAt.getTime() - startedAt.getTime();
    if (writeMode === "upsert" && watermark) {
      const values = report.objects
        .map((item) => item.watermark)
        .filter((item): item is string => Boolean(item));
      report.watermark = { field: watermark.field, value: maxWatermark(values) || watermark.value };
    }
    if (report.ok) {
      const importedObjectTypeIds = new Set(
        report.objects.filter((item) => item.bound).map((item) => item.objectTypeId),
      );
      if (importedObjectTypeIds.size > 0) {
        report.relations = await collectReportRelations(token, networkId, importedObjectTypeIds).catch(() => []);
      }
    }
    appendHistory(report);
    return report;
  } catch (error) {
    const finishedAt = new Date();
    report.finishedAt = finishedAt.toISOString();
    report.elapsedMs = finishedAt.getTime() - startedAt.getTime();
    report.error = error instanceof Error ? error.message : String(error);
    appendHistory(report);
    return report;
  }
}

export async function importDataset(
  token: string,
  spaceId: string,
  dataset: DataDataset,
  networkId: string,
  mapping: ImportMapping,
  options: ImportOptions = {},
): Promise<ImportReport> {
  const collected: Array<Record<string, unknown>> = [];
  let page = 1;
  while (collected.length < MAX_RECORDS) {
    const result = await fetchDatasetRecords(token, spaceId, dataset.id, page, 200);
    if (result.items.length === 0) break;
    collected.push(...result.items);
    if (collected.length >= result.total) break;
    page += 1;
  }
  return runImport(token, networkId, mapping, collected, {
    datasetId: dataset.id,
    sourceLabel: `数据集「${dataset.name}」`,
  }, options);
}

export async function importFileRecords(
  token: string,
  fileName: string,
  buffer: Buffer,
  networkId: string,
  mapping: ImportMapping,
  options: ImportOptions = {},
): Promise<ImportReport> {
  const tables = await extractRecords(fileName, buffer, MAX_RECORDS);
  const first = tables
    .filter((table) => table.records.length > 0)
    .sort((left, right) => right.headers.length - left.headers.length || right.records.length - left.records.length)[0];
  if (!first) {
    const now = new Date();
    const report: ImportReport = {
      ok: false,
      networkId,
      datasetId: "",
      startedAt: now.toISOString(),
      finishedAt: now.toISOString(),
      elapsedMs: 0,
      objects: [],
      issues: [],
      error: "文件里没有解析到数据行（仅支持 Excel / CSV，且需要有表头）",
    };
    appendHistory(report);
    return report;
  }
  return runImport(token, networkId, mapping, first.records, {
    sourceLabel: `文件「${fileName}」`,
  }, options);
}


type PlatformRelationType = {
  id?: string;
  name?: string;
  source_object_type_id?: string;
  target_object_type_id?: string;
  type?: string;
  comment?: string;
  tags?: string[];
  mapping_rules?: Array<{
    source_property?: { name?: string };
    target_property?: { name?: string };
  }>;
};

export type RelationMappingInfo = {
  id: string;
  name: string;
  type?: string;
  sourceObjectTypeId: string;
  targetObjectTypeId: string;
  sourceProperty: string;
  targetProperty: string;
};

function toRelationMappingInfo(item: PlatformRelationType): RelationMappingInfo {
  const rule = item.mapping_rules?.[0];
  return {
    id: String(item.id ?? ""),
    name: String(item.name ?? item.id ?? ""),
    type: item.type,
    sourceObjectTypeId: String(item.source_object_type_id ?? ""),
    targetObjectTypeId: String(item.target_object_type_id ?? ""),
    sourceProperty: String(rule?.source_property?.name ?? ""),
    targetProperty: String(rule?.target_property?.name ?? ""),
  };
}

export async function listRelationMappings(
  token: string,
  networkId: string,
): Promise<RelationMappingInfo[]> {
  const relationTypes = await listRelationTypes(token, networkId);
  return relationTypes
    .filter((item) => item.id)
    .map((item) => toRelationMappingInfo(item));
}

export async function updateRelationMapping(
  token: string,
  networkId: string,
  relationTypeId: string,
  sourceProperty: string,
  targetProperty: string,
): Promise<RelationMappingInfo> {
  const detailUrl = `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(
    networkId,
  )}/relation-types/${encodeURIComponent(relationTypeId)}`;
  const detail = await getJson<{ entries?: PlatformRelationType[] } | PlatformRelationType[]>(detailUrl, token);
  const entry = Array.isArray(detail) ? detail[0] : detail.entries?.[0];
  if (!entry) throw new Error("关系类型不存在：" + relationTypeId);
  const payload: Record<string, unknown> = {
    id: entry.id,
    name: entry.name,
    type: entry.type ?? "direct",
    source_object_type_id: entry.source_object_type_id,
    target_object_type_id: entry.target_object_type_id,
    comment: entry.comment,
    tags: entry.tags ?? [],
    mapping_rules: [
      {
        source_property: { name: sourceProperty },
        target_property: { name: targetProperty },
      },
    ],
  };
  const res = await fetch(detailUrl, {
    method: "PUT",
    headers: { ...authHeaders(token), "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`更新关系映射失败（HTTP ${res.status}）：${text.slice(0, 200)}`);
  }
  return toRelationMappingInfo({
    ...entry,
    mapping_rules: [
      {
        source_property: { name: sourceProperty },
        target_property: { name: targetProperty },
      },
    ],
  });
}

async function listRelationTypes(token: string, networkId: string): Promise<PlatformRelationType[]> {
  const url = `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(
    networkId,
  )}/relation-types?limit=200&offset=0`;
  const data = await getJson<{ entries?: PlatformRelationType[] } | PlatformRelationType[]>(url, token);
  if (Array.isArray(data)) return data;
  return data.entries ?? [];
}

type SubgraphPath = {
  relations?: Array<{ relation_type_id?: string; source_object_id?: string; target_object_id?: string }>;
};

async function queryRelationEdges(
  token: string,
  networkId: string,
  relation: PlatformRelationType,
  limit = 100,
): Promise<{ edgeCount: number; samples: RelationEdgeSample[] }> {
  const url = `${GATEWAY_BASE}/api/ontology-query/v1/knowledge-networks/${encodeURIComponent(
    networkId,
  )}/subgraph`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        ...authHeaders(token),
        "content-type": "application/json",
        "x-http-method-override": "GET",
      },
      body: JSON.stringify({
        source_object_type_id: relation.source_object_type_id,
        direction: "forward",
        path_length: 1,
        limit,
      }),
      signal: controller.signal,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
    const data = JSON.parse(text) as { relation_paths?: SubgraphPath[] };
    const samples: RelationEdgeSample[] = [];
    let edgeCount = 0;
    for (const path of data.relation_paths ?? []) {
      const edge = (path.relations ?? []).find((item) => item.relation_type_id === relation.id);
      if (!edge) continue;
      edgeCount += 1;
      if (samples.length < 3) {
        samples.push({
          sourceId: String(edge.source_object_id ?? ""),
          targetId: String(edge.target_object_id ?? ""),
        });
      }
    }
    return { edgeCount, samples };
  } finally {
    clearTimeout(timer);
  }
}

async function queryRelationEdgesWithRetry(
  token: string,
  networkId: string,
  relation: PlatformRelationType,
): Promise<{ edgeCount: number; samples: RelationEdgeSample[] }> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      return await queryRelationEdges(token, networkId, relation);
    } catch (error) {
      lastError = error;
      const message = (error as Error).message ?? "";
      if (!/SyncPending|503|权限正在同步/.test(message)) break;
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

async function listBoundObjectTypeIds(token: string, networkId: string): Promise<Set<string>> {
  const bound = new Set<string>();
  try {
    const data = await getJson<{ entries?: Array<Record<string, unknown>> }>(
      `${GATEWAY_BASE}/api/bkn-backend/v1/knowledge-networks/${encodeURIComponent(networkId)}/object-types?limit=200&offset=0`,
      token,
    );
    for (const item of Array.isArray(data.entries) ? data.entries : []) {
      const dataSource = item.data_source as Record<string, unknown> | null | undefined;
      if (dataSource && String(dataSource.id ?? "").trim()) {
        bound.add(String(item.id ?? ""));
      }
    }
  } catch {
    // 读取失败时退回仅统计本次导入的对象类型
  }
  return bound;
}

async function collectReportRelations(
  token: string,
  networkId: string,
  importedObjectTypeIds: Set<string>,
): Promise<ReportRelation[]> {
  const relationTypes = await listRelationTypes(token, networkId);
  const boundObjectTypeIds = await listBoundObjectTypeIds(token, networkId);
  for (const objectTypeId of boundObjectTypeIds) importedObjectTypeIds.add(objectTypeId);
  const relevant = relationTypes.filter(
    (item) =>
      item.id &&
      item.source_object_type_id &&
      item.target_object_type_id &&
      importedObjectTypeIds.has(item.source_object_type_id) &&
      importedObjectTypeIds.has(item.target_object_type_id),
  );
  const results: ReportRelation[] = [];
  for (const relation of relevant) {
    const base: ReportRelation = {
      relationTypeId: String(relation.id),
      relationTypeName: String(relation.name ?? relation.id),
      sourceObjectTypeId: String(relation.source_object_type_id),
      targetObjectTypeId: String(relation.target_object_type_id),
      edgeCount: 0,
      samples: [],
    };
    try {
      const { edgeCount, samples } = await queryRelationEdgesWithRetry(token, networkId, relation);
      base.edgeCount = edgeCount;
      base.samples = samples;
      if (edgeCount === 0) {
        base.note = "两端数据的主键/连接字段未匹配，暂无可关联实例";
      }
    } catch (error) {
      base.note = `关系边统计失败：${(error as Error).message}`;
    }
    results.push(base);
  }
  return results;
}

function readHistoryFile(): ImportReport[] {
  try {
    if (!existsSync(HISTORY_PATH)) return [];
    const raw = JSON.parse(readFileSync(HISTORY_PATH, "utf8")) as unknown;
    return Array.isArray(raw) ? (raw as ImportReport[]) : [];
  } catch {
    return [];
  }
}

function appendHistory(report: ImportReport): void {
  try {
    mkdirSync(dirname(HISTORY_PATH), { recursive: true });
    const items = readHistoryFile();
    items.unshift(report);
    writeFileSync(HISTORY_PATH, JSON.stringify(items.slice(0, 200), null, 2), "utf8");
  } catch {
    // 历史文件写失败不影响导入结果
  }
}

export function listImportHistory(): ImportReport[] {
  return readHistoryFile().slice(0, 50);
}

// ---------------------------------------------------------------------------
// 数据库直连数据源：平台「数据连接」（vega catalog）→ 表资源 → 原始 SQL 抽样/导入
// ---------------------------------------------------------------------------

export type DataConnection = {
  id: string;
  name: string;
  connectorType: string;
  database?: string;
};

export type ConnectionTable = {
  id: string;
  name: string;
  category: string;
  sourceIdentifier: string;
  fields: string[];
};

const VEGA_BASE = `${GATEWAY_BASE}/api/vega-backend/v1`;

export async function listDataConnections(token: string): Promise<DataConnection[]> {
  const data = await getJson<{ entries?: Array<Record<string, unknown>> }>(
    `${VEGA_BASE}/catalogs?limit=100&offset=0`,
    token,
  );
  const entries = Array.isArray(data.entries) ? data.entries : [];
  return entries
    .filter((item) => typeof item.connector_type === "string" && item.connector_type.trim() !== "")
    .map((item) => {
      const config = (item.connector_config ?? {}) as Record<string, unknown>;
      return {
        id: String(item.id ?? ""),
        name: String(item.name ?? item.id ?? ""),
        connectorType: String(item.connector_type ?? ""),
        database: typeof config.database === "string" ? config.database : undefined,
      };
    })
    .filter((item) => item.id);
}

export async function listConnectionTables(token: string, catalogId: string): Promise<ConnectionTable[]> {
  const data = await getJson<{ entries?: Array<Record<string, unknown>> }>(
    `${VEGA_BASE}/resources?catalog_id=${encodeURIComponent(catalogId)}&limit=200&offset=0`,
    token,
  );
  const entries = Array.isArray(data.entries) ? data.entries : [];
  return entries
    .filter((item) => {
      const category = String(item.category ?? "");
      return category === "table" || category === "logical_view" || category === "view";
    })
    .map((item) => ({
      id: String(item.id ?? ""),
      name: String(item.name ?? item.id ?? ""),
      category: String(item.category ?? "table"),
      sourceIdentifier: String(item.source_identifier ?? ""),
      fields: Array.isArray(item.schema_definition)
        ? (item.schema_definition as Array<Record<string, unknown>>)
            .map((field) => String(field.name ?? ""))
            .filter(Boolean)
        : [],
    }))
    .filter((item) => item.id);
}

type RawQueryResult = {
  columns: Array<{ name?: string; type?: string }>;
  entries: Array<Record<string, unknown>>;
  totalCount?: number;
};

async function runRawQuery(
  token: string,
  sql: string,
  limit: number,
  offset = 0,
): Promise<RawQueryResult> {
  const payload = await sendJson<{
    columns?: Array<{ name?: string; type?: string }>;
    entries?: Array<Record<string, unknown>>;
    total_count?: number;
  }>("POST", `${VEGA_BASE}/resources/query`, token, {
    query: sql,
    query_format: "sql",
    paging: { mode: "single", offset, limit },
    need_total: true,
  });
  return {
    columns: Array.isArray(payload.columns) ? payload.columns : [],
    entries: Array.isArray(payload.entries) ? payload.entries : [],
    totalCount: typeof payload.total_count === "number" ? payload.total_count : undefined,
  };
}

function tablePlaceholder(resourceId: string): string {
  return `{{${resourceId}}}`;
}

export async function countTableRows(token: string, resourceId: string): Promise<number | undefined> {
  const result = await runRawQuery(token, `SELECT COUNT(*) AS cnt FROM ${tablePlaceholder(resourceId)}`, 1);
  const first = result.entries[0];
  if (!first) return undefined;
  const value = first.cnt ?? first.CNT ?? Object.values(first)[0];
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export async function fetchTableRows(
  token: string,
  resourceId: string,
  maxRows = MAX_RECORDS,
  pageSize = 1000,
): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let offset = 0;
  while (rows.length < maxRows) {
    const limit = Math.min(pageSize, maxRows - rows.length);
    const page = await runRawQuery(
      token,
      `SELECT * FROM ${tablePlaceholder(resourceId)} LIMIT ${limit} OFFSET ${offset}`,
      limit,
      offset,
    );
    rows.push(...page.entries);
    if (page.entries.length < limit) break;
    offset += page.entries.length;
  }
  return rows;
}

export async function previewTable(
  token: string,
  networkId: string,
  catalogId: string,
  resourceId: string,
  tableName: string,
): Promise<{
  tableName: string;
  rows: number;
  sampleRows: Array<Record<string, unknown>>;
  profile: DatasetProfile;
  mapping: ImportMapping;
  objectTypes: unknown;
  warnings: string[];
  stats: TransformResult["stats"];
  documents: Record<string, Array<Record<string, unknown>>>;
  issues: TransformIssue[];
}> {
  const total = (await countTableRows(token, resourceId)) ?? 0;
  const sampled = await runRawQuery(
    token,
    `SELECT * FROM ${tablePlaceholder(resourceId)} LIMIT 200`,
    200,
  );
  const rows = sampled.entries;
  const profile: DatasetProfile = {
    dataset: {
      id: resourceId,
      name: tableName,
      dataType: "tabular",
      description: `数据库表 ${tableName}`,
      recordCount: total,
      targetTable: tableName,
    },
    total,
    sampled: rows.length,
    fields: profileRecords(rows),
  };
  const suggestion = await suggestMappingByProfile(token, networkId, profile);
  const transformed = transformRecords(rows.slice(0, 50), suggestion.mapping);
  return {
    tableName,
    rows: total,
    sampleRows: rows.slice(0, 20),
    profile,
    mapping: suggestion.mapping,
    objectTypes: suggestion.objectTypes,
    warnings: suggestion.warnings,
    stats: transformed.stats,
    documents: transformed.documents,
    issues: transformed.issues,
  };
}

export async function importTableRecords(
  token: string,
  networkId: string,
  catalogId: string,
  resourceId: string,
  tableName: string,
  mapping: ImportMapping,
  options: ImportOptions = {},
): Promise<ImportReport> {
  const rows = await fetchTableRows(token, resourceId);
  return runImport(token, networkId, mapping, rows, {
    datasetId: `${catalogId}:${tableName}`,
    sourceLabel: `数据库表 ${tableName}`,
  }, options);
}
