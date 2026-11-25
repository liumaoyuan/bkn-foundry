import type { IrActionType, IrObjectType, IrProperty, IrRelation, OntologyIr } from "./types/ir.js";

export type IrIssueLevel = "error" | "warning";

export type IrIssue = {
  level: IrIssueLevel;
  code: string;
  message: string;
  path?: string;
  /** 是否已被自动修复。 */
  fixed?: boolean;
};

export type IrCheckResult = {
  ir: OntologyIr;
  issues: IrIssue[];
  changed: boolean;
};

const RELATION_NAME = /^[\u4e00-\u9fa5]{2,8}$/;
const IDENTIFIER = /^[a-z][a-z0-9_]*$/;

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};

const normalizeIdentifier = (value: string, fallback: string): string => {
  const text = String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  if (!text) return fallback;
  return /^[a-z]/.test(text) ? text : `n_${text}`;
};

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/**
 * 确定性检查并修复 IR 草案。
 *
 * 目标：把「小模型常见的结构性错误」在进入编辑器/平台之前消掉，
 * 减少人工逐条修改的成本。只做保守修复，不改变业务语义。
 */
export function checkAndRepairIr(input: OntologyIr): IrCheckResult {
  const ir = clone(input);
  const issues: IrIssue[] = [];
  const original = JSON.stringify(input);

  // 1) 对象类型：id 规范、重名合并、空对象剔除
  const objectById = new Map<string, IrObjectType>();
  const nameToId = new Map<string, string>();
  let usedFallback = 0;
  const keptObjects: IrObjectType[] = [];
  for (const object of ir.objectTypes ?? []) {
    const rawId = String(object.id ?? "").trim();
    const fallback = `object_${++usedFallback}`;
    let id = IDENTIFIER.test(rawId) ? rawId : normalizeIdentifier(rawId, fallback);
    if (IDENTIFIER.test(rawId) === false) {
      issues.push({
        level: "warning",
        code: "object_id_normalized",
        path: rawId || "(空)",
        message: `对象类「${object.name || rawId}」的 id 不是合法标识符，已改为 ${id}`,
        fixed: true,
      });
    }
    const name = String(object.name ?? "").trim() || id;
    const existingByName = nameToId.get(name);
    if (existingByName) {
      issues.push({
        level: "warning",
        code: "object_duplicated",
        path: name,
        message: `对象类「${name}」重复出现，已合并为一个`,
        fixed: true,
      });
      const target = objectById.get(existingByName);
      if (target) {
        for (const property of object.properties ?? []) {
          if (!target.properties.some((item) => item.name === property.name)) {
            target.properties.push(property);
          }
        }
      }
      continue;
    }
    let uniqueId = id;
    let suffix = 2;
    while (objectById.has(uniqueId) && objectById.get(uniqueId)?.name !== name) {
      uniqueId = `${id}_${suffix++}`;
    }
    if (uniqueId !== id) {
      issues.push({
        level: "warning",
        code: "object_id_conflict",
        path: id,
        message: `对象类 id「${id}」冲突，已改为 ${uniqueId}`,
        fixed: true,
      });
      id = uniqueId;
    }
    object.id = id;
    object.name = name;
    object.properties = Array.isArray(object.properties) ? object.properties : [];
    // 属性名规范去重
    const propertyNames = new Set<string>();
    const properties: IrProperty[] = [];
    for (const property of object.properties) {
      const propertyName = normalizeIdentifier(property.name, `field_${properties.length + 1}`);
      if (propertyNames.has(propertyName)) {
        issues.push({
          level: "warning",
          code: "property_duplicated",
          path: `${name}.${property.name}`,
          message: `属性「${property.name}」重复，已去重`,
          fixed: true,
        });
        continue;
      }
      propertyNames.add(propertyName);
      properties.push({ ...property, name: propertyName });
    }
    object.properties = properties;
    objectById.set(id, object);
    nameToId.set(name, id);
    keptObjects.push(object);
  }
  ir.objectTypes = keptObjects;

  // 2) 关系：端点存在性、id 规范、重名去重
  const relationIds = new Set<string>();
  const relationKeys = new Set<string>();
  const keptRelations: IrRelation[] = [];
  for (const relation of ir.relations ?? []) {
    const source = objectById.get(String(relation.source ?? "").trim());
    const target = objectById.get(String(relation.target ?? "").trim());
    if (!source || !target) {
      issues.push({
        level: "error",
        code: "relation_endpoint_missing",
        path: relation.name || relation.id,
        message: `关系「${relation.name || relation.id}」的端点不存在（${relation.source} → ${relation.target}），已移除`,
        fixed: true,
      });
      continue;
    }
    const key = `${source.id}->${target.id}:${String(relation.name ?? "").trim()}`;
    if (relationKeys.has(key)) {
      issues.push({
        level: "warning",
        code: "relation_duplicated",
        path: relation.name || relation.id,
        message: `关系「${relation.name}」重复，已去重`,
        fixed: true,
      });
      continue;
    }
    relationKeys.add(key);
    let id = IDENTIFIER.test(String(relation.id ?? ""))
      ? String(relation.id)
      : normalizeIdentifier(String(relation.id ?? relation.name ?? ""), `rel_${keptRelations.length + 1}`);
    let suffix = 2;
    while (relationIds.has(id)) id = `${id}_${suffix++}`;
    relationIds.add(id);
    if (relation.mapping && relation.mapping.length > 0) {
      const sourceProps = new Set(source.properties.map((item) => item.name));
      const targetProps = new Set(target.properties.map((item) => item.name));
      const validMapping = relation.mapping.filter(
        (item) => sourceProps.has(item.sourceProperty) && targetProps.has(item.targetProperty),
      );
      if (validMapping.length !== relation.mapping.length) {
        issues.push({
          level: "warning",
          code: "relation_mapping_invalid",
          path: relation.name,
          message: `关系「${relation.name}」有 ${relation.mapping.length - validMapping.length} 条连接字段在端点对象类中不存在，已移除`,
          fixed: true,
        });
        relation.mapping = validMapping;
      }
    }
    if (String(relation.name ?? "").trim() && !RELATION_NAME.test(String(relation.name))) {
      issues.push({
        level: "warning",
        code: "relation_name_style",
        path: relation.name,
        message: `关系名「${relation.name}」不是 2-8 个汉字的动词短语，建议人工确认`,
      });
    }
    keptRelations.push({ ...relation, id });
  }
  ir.relations = keptRelations;

  // 3) 空对象：没有属性且没有关系的对象类剔除
  const relatedIds = new Set<string>();
  for (const relation of keptRelations) {
    relatedIds.add(relation.source);
    relatedIds.add(relation.target);
  }
  const before = ir.objectTypes.length;
  const removedNames: string[] = [];
  ir.objectTypes = ir.objectTypes.filter((object) => {
    if (object.properties.length === 0 && !relatedIds.has(object.id)) {
      removedNames.push(object.name || object.id);
      return false;
    }
    return true;
  });
  if (removedNames.length > 0) {
    issues.push({
      level: "warning",
      code: "empty_object_removed",
      message: `空对象类（无属性、无关系）已移除：${removedNames.join("、")}`,
      fixed: true,
    });
    if (before !== ir.objectTypes.length) {
      const alive = new Set(ir.objectTypes.map((item) => item.id));
      ir.relations = ir.relations.filter(
        (relation) => alive.has(relation.source) && alive.has(relation.target),
      );
    }
  }

  // 4) 主键：缺失或不在属性集合里时尝试推断
  for (const object of ir.objectTypes) {
    const names = object.properties.map((item) => item.name);
    if (!object.primaryKey || !names.includes(object.primaryKey)) {
      const guess =
        names.find((name) => name === "id") ??
        names.find((name) => name.endsWith("_id")) ??
        names.find((name) => name.endsWith("_code")) ??
        names[0];
      if (guess && guess !== object.primaryKey) {
        issues.push({
          level: "warning",
          code: "primary_key_inferred",
          path: object.name,
          message: `对象类「${object.name}」的${object.primaryKey ? "主键不存在" : "未设置主键"}，已推断为 ${guess}`,
          fixed: true,
        });
        object.primaryKey = guess;
      } else if (!guess) {
        issues.push({
          level: "error",
          code: "primary_key_missing",
          path: object.name,
          message: `对象类「${object.name}」没有属性，无法设置主键`,
        });
      }
    }
  }

  // 5) 行动类型：绑定对象必须存在
  const keptActions: IrActionType[] = [];
  const actionIds = new Set<string>();
  for (const action of ir.actionTypes ?? []) {
    const objectId = String(action.boundObject ?? "").trim();
    if (!objectById.has(objectId) && !ir.objectTypes.some((item) => item.id === objectId)) {
      issues.push({
        level: "error",
        code: "action_bound_object_missing",
        path: action.name,
        message: `行动「${action.name}」绑定的对象类不存在（${objectId}），已移除`,
        fixed: true,
      });
      continue;
    }
    let id = IDENTIFIER.test(String(action.id ?? ""))
      ? String(action.id)
      : normalizeIdentifier(String(action.id ?? action.name ?? ""), `action_${keptActions.length + 1}`);
    let suffix = 2;
    while (actionIds.has(id)) id = `${id}_${suffix++}`;
    actionIds.add(id);
    keptActions.push({ ...action, id });
  }
  if (ir.actionTypes) ir.actionTypes = keptActions;

  // 6) 风险类型：策略为空/过短给出提示
  for (const risk of ir.riskTypes ?? []) {
    const policies = Array.isArray(risk.policies) ? risk.policies.filter((item) => String(item).trim()) : [];
    if (policies.length === 0) {
      issues.push({
        level: "warning",
        code: "risk_policies_missing",
        path: risk.name,
        message: `风险「${risk.name}」没有管控策略，建议补充可执行动作`,
      });
    }
    risk.policies = policies;
  }

  // 7) 概念分组：剔除不存在的对象引用
  if (ir.conceptGroups) {
    const alive = new Set(ir.objectTypes.map((item) => item.id));
    for (const group of ir.conceptGroups) {
      const refs = (group.objectTypes ?? []).filter((item) => alive.has(item));
      if (refs.length !== (group.objectTypes ?? []).length) {
        issues.push({
          level: "warning",
          code: "concept_group_ref_invalid",
          path: group.name,
          message: `概念分组「${group.name}」引用了不存在的对象类，已移除这些引用`,
          fixed: true,
        });
      }
      group.objectTypes = refs;
    }
  }

  const changed = JSON.stringify(ir) !== original;
  return { ir, issues, changed };
}

/** 供前端展示用的简要统计。 */
export function summarizeIssues(issues: IrIssue[]): {
  errors: number;
  warnings: number;
  fixed: number;
} {
  let errors = 0;
  let warnings = 0;
  let fixed = 0;
  for (const issue of issues) {
    if (issue.level === "error") errors += 1;
    else warnings += 1;
    if (issue.fixed) fixed += 1;
  }
  return { errors, warnings, fixed };
}

export type { IrObjectType, IrRelation, unknownRecord as _unusedRecord };
type unknownRecord = Record<string, unknown>;
