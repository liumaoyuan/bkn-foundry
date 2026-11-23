export type IrPropertyType = "string" | "integer" | "number" | "boolean" | "datetime";

/**
 * 证据引用：某个对象/属性/关系在输入材料中的来源。
 * 由服务端在生成草案后做确定性检索得到（不依赖模型自报，避免幻觉）。
 */
export type IrEvidence = {
  /** 来源文件名；粘贴文本记为「粘贴文本」。 */
  source: string;
  /** 更精确的位置：工作表名（表格输入）或行号。 */
  location?: string;
  /** 命中的原文片段（截断保存）。 */
  quote: string;
};

export type IrProperty = {
  name: string;
  displayName?: string;
  type: IrPropertyType;
  description?: string;
  /** 推断出的来源列名（来自上传的数据表表头），用于导入平台后绑定字段。 */
  mappedField?: string;
  evidence?: IrEvidence[];
};

export type IrObjectType = {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  properties: IrProperty[];
  primaryKey?: string;
  displayKey?: string;
  /** 推断出的来源表名（来自上传的数据表/工作表名）。 */
  sourceTable?: string;
  evidence?: IrEvidence[];
};

export type IrRelation = {
  id: string;
  name: string;
  source: string;
  target: string;
  description?: string;
  /**
   * 数据表键映射（由数据表外键推断）。
   * 例如：设备包含部件 → 部件表.equipment_id = 设备表.equipment_id。
   */
  mapping?: Array<{ sourceProperty: string; targetProperty: string }>;
  evidence?: IrEvidence[];
};

export type IrActionTypeValue = "add" | "modify" | "delete";

export type IrActionType = {
  id: string;
  name: string;
  description?: string;
  /** 平台 action_type：add / modify / delete。 */
  actionType: IrActionTypeValue;
  /** 绑定的对象类型 id。 */
  boundObject: string;
  riskLevel?: "low" | "medium" | "high";
  requiresApproval?: boolean;
  enabled?: boolean;
  /** 触发条件：对象字段 + 运算符 + 值。 */
  trigger?: { field: string; operation: string; value?: string };
  evidence?: IrEvidence[];
};

export type IrRiskType = {
  id: string;
  name: string;
  description?: string;
  /** 管控策略要点（写入 .bkn 的 Control Policy）。 */
  policies: string[];
  evidence?: IrEvidence[];
};

export type IrConceptGroup = {
  id: string;
  name: string;
  description?: string;
  objectTypes: string[];
};

export type OntologyIr = {
  networkId: string;
  networkName: string;
  description?: string;
  objectTypes: IrObjectType[];
  relations: IrRelation[];
  actionTypes?: IrActionType[];
  riskTypes?: IrRiskType[];
  conceptGroups?: IrConceptGroup[];
};

export type NormalizeResult =
  | { ok: true; ir: OntologyIr; warnings: string[] }
  | { ok: false; errors: string[] };

const PROPERTY_TYPES: IrPropertyType[] = ["string", "integer", "number", "boolean", "datetime"];

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};

const asString = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value.trim() : fallback;

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const slugify = (raw: string, fallback: string): string => {
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return IDENTIFIER.test(slug) ? slug : fallback;
};

const uniqueId = (candidate: string, used: Set<string>): string => {
  let id = candidate;
  let index = 2;
  while (used.has(id)) {
    id = `${candidate}_${index}`;
    index += 1;
  }
  used.add(id);
  return id;
};

const toPropertyType = (value: unknown): IrPropertyType => {
  const raw = asString(value).toLowerCase();
  return (PROPERTY_TYPES as string[]).includes(raw) ? (raw as IrPropertyType) : "string";
};

/** 读取证据列表：宽松解析，缺 source/quote 的条目丢弃。 */
const readEvidence = (value: unknown): IrEvidence[] | undefined => {
  const items = asArray(value)
    .map((entry) => {
      const item = asRecord(entry);
      const source = asString(item.source);
      const quote = asString(item.quote);
      if (!source || !quote) {
        return null;
      }
      const evidence: IrEvidence = {
        source,
        quote: quote.length > 160 ? `${quote.slice(0, 160)}…` : quote,
        ...(asString(item.location) ? { location: asString(item.location) } : {}),
      };
      return evidence;
    })
    .filter((item): item is IrEvidence => item !== null);
  return items.length > 0 ? items : undefined;
};

/**
 * 把 LLM 或启发式抽取的松散 JSON 规范化为可编译的 IR。
 * 只做结构纠正，不做业务推断。
 */
export function normalizeIr(input: unknown): NormalizeResult {
  const errors: string[] = [];
  const warnings: string[] = [];
  const root = asRecord(input);

  const networkName = asString(root.networkName) || asString(root.name) || "未命名知识网络";
  const networkId = uniqueId(
    slugify(asString(root.networkId) || asString(root.id) || networkName, "kn_generated"),
    new Set(),
  );

  const usedObjectIds = new Set<string>();
  const objectTypes: IrObjectType[] = [];
  asArray(root.objectTypes).forEach((rawObject, index) => {
    const raw = asRecord(rawObject);
    const name = asString(raw.name) || asString(raw.displayName) || `对象${index + 1}`;
    const id = uniqueId(slugify(asString(raw.id) || name, `obj_${index + 1}`), usedObjectIds);

    const propertyIds = new Set<string>();
    const properties: IrProperty[] = [];
    asArray(raw.properties).forEach((rawProperty, propertyIndex) => {
      const item = asRecord(rawProperty);
      const propertyName = slugify(
        asString(item.name) || `field_${propertyIndex + 1}`,
        `field_${propertyIndex + 1}`,
      );
      if (propertyIds.has(propertyName)) {
        return;
      }
      propertyIds.add(propertyName);
      properties.push({
        name: propertyName,
        displayName: asString(item.displayName) || propertyName,
        type: toPropertyType(item.type),
        description: asString(item.description) || undefined,
        mappedField: asString(item.mappedField) || undefined,
        evidence: readEvidence(item.evidence),
      });
    });

    if (!propertyIds.has("id")) {
      properties.unshift({
        name: "id",
        displayName: "ID",
        type: "string",
        description: "主键ID",
      });
      propertyIds.add("id");
    }

    const primaryKey = slugify(asString(raw.primaryKey) || "id", "id");
    const displayKey = slugify(asString(raw.displayKey) || primaryKey, primaryKey);

    objectTypes.push({
      id,
      name,
      description: asString(raw.description) || undefined,
      tags: asArray(raw.tags).map((tag) => asString(tag)).filter(Boolean),
      properties,
      primaryKey: propertyIds.has(primaryKey) ? primaryKey : "id",
      displayKey: propertyIds.has(displayKey) ? displayKey : "id",
      sourceTable: asString(raw.sourceTable) || undefined,
      evidence: readEvidence(raw.evidence),
    });
  });

  if (objectTypes.length === 0) {
    errors.push("没有解析出任何对象类型");
  }

  const objectIds = new Set(objectTypes.map((object) => object.id));
  const usedRelationIds = new Set<string>();
  const relations: IrRelation[] = [];
  asArray(root.relations ?? root.relationTypes).forEach((rawRelation, index) => {
    const raw = asRecord(rawRelation);
    const source = slugify(asString(raw.source), "");
    const target = slugify(asString(raw.target), "");
    if (!objectIds.has(source) || !objectIds.has(target)) {
      warnings.push(`跳过关系 ${index + 1}：source/target 不在对象类型中`);
      return;
    }
    const name = asString(raw.name) || `${source}->${target}`;
    // 数据表外键推断出的键映射（sourceProperty -> targetProperty），无则不带。
    const mapping = asArray(raw.mapping)
      .map((item) => {
        const pair = asRecord(item);
        const sourceProperty = slugify(asString(pair.sourceProperty), "");
        const targetProperty = slugify(asString(pair.targetProperty), "");
        return sourceProperty && targetProperty ? { sourceProperty, targetProperty } : null;
      })
      .filter((item): item is { sourceProperty: string; targetProperty: string } => item !== null);
    relations.push({
      id: uniqueId(slugify(asString(raw.id) || name, `rel_${index + 1}`), usedRelationIds),
      name,
      source,
      target,
      description: asString(raw.description) || undefined,
      ...(mapping.length > 0 ? { mapping } : {}),
      evidence: readEvidence(raw.evidence),
    });
  });

  const usedGroupIds = new Set<string>();
  const conceptGroups: IrConceptGroup[] = [];
  asArray(root.conceptGroups).forEach((rawGroup, index) => {
    const raw = asRecord(rawGroup);
    const members = asArray(raw.objectTypes)
      .map((item) => slugify(asString(item), ""))
      .filter((item) => objectIds.has(item));
    if (members.length === 0) {
      return;
    }
    const name = asString(raw.name) || `概念分组${index + 1}`;
    conceptGroups.push({
      id: uniqueId(slugify(asString(raw.id) || name, `cg_${index + 1}`), usedGroupIds),
      name,
      description: asString(raw.description) || undefined,
      objectTypes: members,
    });
  });

  const usedActionIds = new Set<string>();
  const actionTypes: IrActionType[] = [];
  asArray(root.actionTypes ?? root.action_types).forEach((rawAction, index) => {
    const raw = asRecord(rawAction);
    const boundObject = slugify(asString(raw.boundObject ?? raw.objectTypeId ?? raw.object_type_id), "");
    if (!objectIds.has(boundObject)) {
      warnings.push(`跳过行动类型 ${index + 1}：绑定对象不在对象类型中`);
      return;
    }
    const actionValue = asString(raw.actionType ?? raw.action_type).toLowerCase();
    if (!["add", "modify", "delete"].includes(actionValue)) {
      warnings.push(`跳过行动类型 ${index + 1}：action_type 必须是 add/modify/delete`);
      return;
    }
    const name = asString(raw.name) || `操作${index + 1}`;
    const riskLevelRaw = asString(raw.riskLevel ?? raw.risk_level).toLowerCase();
    const riskLevel = ["low", "medium", "high"].includes(riskLevelRaw)
      ? (riskLevelRaw as "low" | "medium" | "high")
      : undefined;
    const trigger = asRecord(raw.trigger);
    const triggerField = asString(trigger.field);
    actionTypes.push({
      id: uniqueId(slugify(asString(raw.id) || name, `action_${index + 1}`), usedActionIds),
      name,
      description: asString(raw.description) || undefined,
      actionType: actionValue as IrActionTypeValue,
      boundObject,
      riskLevel,
      requiresApproval: raw.requiresApproval === true || raw.requires_approval === true,
      enabled: raw.enabled === true,
      trigger: triggerField
        ? {
            field: triggerField,
            operation: asString(trigger.operation) || "==",
            value: asString(trigger.value) || undefined,
          }
        : undefined,
      evidence: readEvidence(raw.evidence),
    });
  });

  const usedRiskIds = new Set<string>();
  const riskTypes: IrRiskType[] = [];
  asArray(root.riskTypes ?? root.risk_types).forEach((rawRisk, index) => {
    const raw = asRecord(rawRisk);
    const name = asString(raw.name) || `风险${index + 1}`;
    const policies = asArray(raw.policies ?? raw.controlPolicies)
      .map((item) => asString(item))
      .filter(Boolean);
    riskTypes.push({
      id: uniqueId(slugify(asString(raw.id) || name, `risk_${index + 1}`), usedRiskIds),
      name,
      description: asString(raw.description) || undefined,
      policies,
      evidence: readEvidence(raw.evidence),
    });
  });

  if (errors.length > 0) {
    return { ok: false, errors };
  }

  return {
    ok: true,
    warnings,
    ir: {
      networkId,
      networkName,
      description: asString(root.description) || undefined,
      objectTypes,
      relations,
      actionTypes,
      riskTypes,
      conceptGroups,
    },
  };
}
