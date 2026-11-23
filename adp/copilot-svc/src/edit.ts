import type {
  IrActionType,
  IrActionTypeValue,
  IrConceptGroup,
  IrObjectType,
  IrProperty,
  IrPropertyType,
  IrRelation,
  IrRiskType,
  OntologyIr,
} from "./types/ir.js";

export type IrOp = Record<string, unknown>;

export type ApplyOpsResult = {
  ir: OntologyIr;
  applied: string[];
  warnings: string[];
};

const asText = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

const normalizeName = (value: string): string => value.replace(/\s+/g, "").toLowerCase();

const slugify = (value: string, fallback: string): string => {
  const slug = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[a-z][a-z0-9_]*$/.test(slug) ? slug : fallback;
};

const uniqueId = (candidate: string, used: Set<string>): string => {
  if (!used.has(candidate)) {
    return candidate;
  }
  let index = 2;
  while (used.has(`${candidate}_${index}`)) {
    index += 1;
  }
  return `${candidate}_${index}`;
};

/** 按 id 或（中文/英文）名称定位对象类型；名称比较忽略空格与大小写。 */
function findObjectType(
  draft: OntologyIr,
  reference: string,
): IrObjectType | undefined {
  const target = normalizeName(reference);
  return draft.objectTypes.find(
    (item) => item.id === reference || normalizeName(item.name) === target,
  );
}

function findRelation(draft: OntologyIr, reference: string): IrRelation | undefined {
  const target = normalizeName(reference);
  return draft.relations.find(
    (item) => item.id === reference || normalizeName(item.name) === target,
  );
}

function findAction(draft: OntologyIr, reference: string): IrActionType | undefined {
  const target = normalizeName(reference);
  return (draft.actionTypes ?? []).find(
    (item) => item.id === reference || normalizeName(item.name) === target,
  );
}

function findRisk(draft: OntologyIr, reference: string): IrRiskType | undefined {
  const target = normalizeName(reference);
  return (draft.riskTypes ?? []).find(
    (item) => item.id === reference || normalizeName(item.name) === target,
  );
}

const ACTION_TYPE_VALUES: readonly IrActionTypeValue[] = ["add", "modify", "delete"];

function readActionTypeValue(value: unknown, fallback: IrActionTypeValue): IrActionTypeValue {
  const text = asText(value).toLowerCase();
  return ACTION_TYPE_VALUES.find((item) => item === text) ?? fallback;
}

function readRiskLevel(value: unknown): "low" | "medium" | "high" | undefined {
  const text = asText(value).toLowerCase();
  return text === "low" || text === "medium" || text === "high" ? text : undefined;
}

const PROPERTY_TYPES: readonly IrPropertyType[] = [
  "string",
  "integer",
  "number",
  "boolean",
  "datetime",
];

const readPropertyType = (value: unknown): IrPropertyType => {
  const text = asText(value).toLowerCase();
  return PROPERTY_TYPES.find((item) => item === text) ?? "string";
};

function readProperties(raw: unknown): IrProperty[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const name = asText(record.name);
    if (!name) {
      return [];
    }
    return [
      {
        name,
        displayName: asText(record.displayName) || name,
        type: readPropertyType(record.type),
        description: asText(record.description) || "",
      },
    ];
  });
}

/**
 * 把模型返回的「操作列表」确定性地作用到当前草案上。
 * 这样「删除/改名/新增」不会只停留在模型嘴上：服务端实际执行并回传新草案。
 */
export function applyIrOps(draft: OntologyIr, ops: unknown): ApplyOpsResult {
  const next: OntologyIr = {
    ...draft,
    objectTypes: draft.objectTypes.map((item) => ({ ...item, properties: [...item.properties] })),
    relations: draft.relations.map((item) => ({ ...item })),
    conceptGroups: (draft.conceptGroups ?? []).map((item) => ({
      ...item,
      objectTypes: [...item.objectTypes],
    })),
    actionTypes: (draft.actionTypes ?? []).map((item) => ({ ...item })),
    riskTypes: (draft.riskTypes ?? []).map((item) => ({ ...item, policies: [...item.policies] })),
  };
  const groups: IrConceptGroup[] = next.conceptGroups ?? [];
  const applied: string[] = [];
  const warnings: string[] = [];

  if (!Array.isArray(ops)) {
    return { ir: next, applied, warnings: ["模型没有返回可执行的操作"] };
  }

  for (const raw of ops) {
    if (typeof raw !== "object" || raw === null) {
      continue;
    }
    const op = raw as IrOp;
    const kind = asText(op.op);

    if (kind === "add_object") {
      const name = asText(op.name);
      if (!name) {
        warnings.push("add_object 缺少 name，已忽略");
        continue;
      }
      const used = new Set(next.objectTypes.map((item) => item.id));
      const id = uniqueId(slugify(asText(op.id) || name, "obj"), used);
      next.objectTypes.push({
        id,
        name,
        description: asText(op.description) || "",
        tags: [],
        properties:
          readProperties(op.properties).length > 0
            ? readProperties(op.properties)
            : [
                {
                  name: "id",
                  displayName: "ID",
                  type: "string" as const,
                  description: "主键",
                },
                {
                  name: "name",
                  displayName: "名称",
                  type: "string" as const,
                  description: "名称",
                },
              ],
        primaryKey: "id",
        displayKey: "name",
      });
      applied.push(`新增对象类型「${name}」`);
      continue;
    }

    if (kind === "remove_object") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findObjectType(next, reference) : undefined;
      if (!target) {
        warnings.push(`要删除的对象「${reference || "?"}」在草案里不存在`);
        continue;
      }
      const removedIds = new Set([target.id]);
      next.objectTypes = next.objectTypes.filter((item) => item.id !== target.id);
      const before = next.relations.length;
      next.relations = next.relations.filter(
        (item) => !removedIds.has(item.source) && !removedIds.has(item.target),
      );
      next.conceptGroups = groups.map((group) => ({
        ...group,
        objectTypes: group.objectTypes.filter((id) => !removedIds.has(id)),
      }));
      const beforeActions = next.actionTypes?.length ?? 0;
      next.actionTypes = (next.actionTypes ?? []).filter((item) => !removedIds.has(item.boundObject));
      const removedActions = beforeActions - (next.actionTypes?.length ?? 0);
      applied.push(
        `删除对象类型「${target.name}」（连带 ${before - next.relations.length} 条关系${
          removedActions > 0 ? `、${removedActions} 个行动类型` : ""
        }）`,
      );
      continue;
    }

    if (kind === "rename_object") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findObjectType(next, reference) : undefined;
      const newName = asText(op.newName) || asText(op.renameTo);
      if (!target || !newName) {
        warnings.push("rename_object 缺少可定位的对象或新名称");
        continue;
      }
      target.name = newName;
      const ascii = slugify(newName, "");
      if (ascii) {
        const used = new Set(next.objectTypes.filter((item) => item.id !== target.id).map((item) => item.id));
        const newId = uniqueId(ascii, used);
        const oldId = target.id;
        target.id = newId;
        next.relations = next.relations.map((item) => ({
          ...item,
          source: item.source === oldId ? newId : item.source,
          target: item.target === oldId ? newId : item.target,
        }));
      }
      applied.push(`对象类型「${target.name}」改名（id 同步）`);
      continue;
    }

    if (kind === "add_relation") {
      const source = findObjectType(next, asText(op.source));
      const target = findObjectType(next, asText(op.target));
      const name = asText(op.name) || (source && target ? `${source.name}关联${target.name}` : "关联");
      if (!source || !target) {
        warnings.push(`add_relation 的端点「${asText(op.source)} → ${asText(op.target)}」不在草案里`);
        continue;
      }
      const used = new Set(next.relations.map((item) => item.id));
      const id = uniqueId(slugify(`${source.id}_${target.id}`, "rel"), used);
      next.relations.push({
        id,
        name,
        description: asText(op.description) || "",
        source: source.id,
        target: target.id,
      });
      applied.push(`新增关系「${name}」（${source.name} → ${target.name}）`);
      continue;
    }

    if (kind === "remove_relation") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findRelation(next, reference) : undefined;
      if (!target) {
        warnings.push(`要删除的关系「${reference || "?"}」在草案里不存在`);
        continue;
      }
      next.relations = next.relations.filter((item) => item.id !== target.id);
      applied.push(`删除关系「${target.name}」`);
      continue;
    }

    if (kind === "add_action") {
      const name = asText(op.name);
      const boundName = asText(op.boundObject) || asText(op.bound_object);
      const bound = boundName ? findObjectType(next, boundName) : undefined;
      if (!name || !bound) {
        warnings.push("add_action 缺少 name 或绑定的对象不存在，已忽略");
        continue;
      }
      const used = new Set((next.actionTypes ?? []).map((item) => item.id));
      const id = uniqueId(slugify(asText(op.id) || name, ""), used);
      const action: IrActionType = {
        id: id || uniqueId("action_1", used),
        name,
        description: asText(op.description) || "",
        actionType: readActionTypeValue(op.actionType ?? op.action_type, "modify"),
        boundObject: bound.id,
        riskLevel: readRiskLevel(op.riskLevel ?? op.risk_level),
        requiresApproval: Boolean(op.requiresApproval ?? op.requires_approval),
        enabled: op.enabled === undefined ? false : Boolean(op.enabled),
      };
      const triggerField = asText(op.field) || asText((op.trigger as Record<string, unknown> | undefined)?.field);
      if (triggerField) {
        const triggerRecord = (op.trigger ?? {}) as Record<string, unknown>;
        action.trigger = {
          field: triggerField,
          operation: asText(triggerRecord.operation) || asText(op.operation) || "==",
          value: asText(triggerRecord.value) || asText(op.value) || undefined,
        };
      }
      next.actionTypes = [...(next.actionTypes ?? []), action];
      applied.push(`新增行动类型「${name}」（${action.actionType} · ${bound.name}）`);
      continue;
    }

    if (kind === "update_action") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findAction(next, reference) : undefined;
      if (!target) {
        warnings.push(`要修改的行动类型「${reference || "?"}」在草案里不存在`);
        continue;
      }
      const newName = asText(op.newName) || asText(op.renameTo);
      if (newName) {
        target.name = newName;
      }
      const actionValue = asText(op.actionType) || asText(op.action_type);
      if (actionValue) {
        target.actionType = readActionTypeValue(actionValue, target.actionType);
      }
      const boundName = asText(op.boundObject) || asText(op.bound_object);
      if (boundName) {
        const bound = findObjectType(next, boundName);
        if (!bound) {
          warnings.push(`行动类型「${target.name}」要绑定到不存在的对象「${boundName}」，已忽略该字段`);
        } else {
          target.boundObject = bound.id;
        }
      }
      if (op.description !== undefined) {
        target.description = asText(op.description);
      }
      const riskLevelValue = asText(op.riskLevel) || asText(op.risk_level);
      if (riskLevelValue) {
        const level = readRiskLevel(riskLevelValue);
        if (level) {
          target.riskLevel = level;
        }
      }
      if (op.requiresApproval !== undefined || op.requires_approval !== undefined) {
        target.requiresApproval = Boolean(op.requiresApproval ?? op.requires_approval);
      }
      if (op.enabled !== undefined) {
        target.enabled = Boolean(op.enabled);
      }
      applied.push(`修改行动类型「${target.name}」`);
      continue;
    }

    if (kind === "remove_action") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findAction(next, reference) : undefined;
      if (!target) {
        warnings.push(`要删除的行动类型「${reference || "?"}」在草案里不存在`);
        continue;
      }
      next.actionTypes = (next.actionTypes ?? []).filter((item) => item.id !== target.id);
      applied.push(`删除行动类型「${target.name}」`);
      continue;
    }

    if (kind === "add_risk") {
      const name = asText(op.name);
      if (!name) {
        warnings.push("add_risk 缺少 name，已忽略");
        continue;
      }
      const used = new Set((next.riskTypes ?? []).map((item) => item.id));
      const policies = Array.isArray(op.policies)
        ? op.policies.map((item) => asText(item)).filter((item) => item.length > 0)
        : [];
      const risk: IrRiskType = {
        id: uniqueId(slugify(asText(op.id) || name, ""), used) || uniqueId("risk_1", used),
        name,
        description: asText(op.description) || "",
        policies,
      };
      next.riskTypes = [...(next.riskTypes ?? []), risk];
      applied.push(`新增风险类型「${name}」（${policies.length} 条管控策略）`);
      continue;
    }

    if (kind === "update_risk") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findRisk(next, reference) : undefined;
      if (!target) {
        warnings.push(`要修改的风险类型「${reference || "?"}」在草案里不存在`);
        continue;
      }
      const newName = asText(op.newName) || asText(op.renameTo);
      if (newName) {
        target.name = newName;
      }
      if (op.description !== undefined) {
        target.description = asText(op.description);
      }
      if (Array.isArray(op.policies)) {
        target.policies = op.policies.map((item) => asText(item)).filter((item) => item.length > 0);
      }
      applied.push(`修改风险类型「${target.name}」`);
      continue;
    }

    if (kind === "remove_risk") {
      const reference = asText(op.id) || asText(op.name);
      const target = reference ? findRisk(next, reference) : undefined;
      if (!target) {
        warnings.push(`要删除的风险类型「${reference || "?"}」在草案里不存在`);
        continue;
      }
      next.riskTypes = (next.riskTypes ?? []).filter((item) => item.id !== target.id);
      applied.push(`删除风险类型「${target.name}」`);
      continue;
    }
    warnings.push(`未知操作「${kind || "?"}」已忽略`);
  }

  return { ir: next, applied, warnings };
}
