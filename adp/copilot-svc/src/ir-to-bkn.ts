import type { IrActionType, IrConceptGroup, IrObjectType, IrRelation, IrRiskType, OntologyIr } from "./types/ir.js";

export type CompiledFile = {
  path: string;
  content: string;
};

const tableCell = (value: string): string => value.replace(/\|/g, "\\|").replace(/\r?\n/g, " ");

const frontmatter = (lines: [string, string][]): string =>
  ["---", ...lines.map(([key, value]) => `${key}: ${value}`), "---"].join("\n");

const tagsValue = (tags: string[] | undefined): string =>
  `[${(tags ?? []).map((tag) => tableCell(tag)).join(", ")}]`;

function objectTypeFile(object: IrObjectType): string {
  const properties = object.properties
    .map(
      (property) =>
        `| ${property.name} | ${tableCell(property.displayName ?? property.name)} | ${property.type} | ${tableCell(
          property.description ?? "",
        )} | ${property.mappedField ?? property.name} |`,
    )
    .join("\n");

  return [
    frontmatter([
      ["type", "object_type"],
      ["id", object.id],
      ["name", object.name],
      ["tags", tagsValue(object.tags)],
    ]),
    "",
    `## ObjectType: ${object.name}`,
    "",
    object.description ?? `${object.name}对象类型。`,
    "",
    "### Data Properties",
    "",
    "| Name | Display Name | Type | Description | Mapped Field |",
    "| --- | --- | --- | --- | --- |",
    properties,
    "",
    "### Logic Properties",
    "",
    "### Keys",
    "",
    `Primary Keys: ${object.primaryKey ?? "id"}`,
    `Display Key: ${object.displayKey ?? object.primaryKey ?? "id"}`,
    `Incremental Key: ${object.primaryKey ?? "id"}`,
    "",
  ].join("\n");
}

function relationTypeFile(relation: IrRelation): string {
  return [
    frontmatter([
      ["type", "relation_type"],
      ["id", relation.id],
      ["name", relation.name],
      ["tags", "[]"],
    ]),
    "",
    `## RelationType: ${relation.name}`,
    "",
    relation.description ?? `${relation.name}关系。`,
    "",
    "### Endpoint",
    "",
    "| Source | Target | Type |",
    "| --- | --- | --- |",
    `| ${relation.source} | ${relation.target} | direct |`,
    "",
    "### Mapping Rules",
    "",
    "| Source Property | Target Property |",
    "| --- | --- |",
    `| id | id |`,
    "",
  ].join("\n");
}

function conceptGroupFile(group: IrConceptGroup, objects: IrObjectType[]): string {
  const byId = new Map(objects.map((object) => [object.id, object]));
  const rows = group.objectTypes
    .map((id) => {
      const object = byId.get(id);
      return `| ${id} | ${tableCell(object?.name ?? id)} | ${tableCell(object?.description ?? "")} |`;
    })
    .join("\n");

  return [
    frontmatter([
      ["type", "concept_group"],
      ["id", group.id],
      ["name", group.name],
      ["tags", "[]"],
    ]),
    "",
    `## ConceptGroup: ${group.name}`,
    "",
    group.description ?? `${group.name}概念分组。`,
    "",
    "### Object Types",
    "",
    "| ID | Name | Description |",
    "| --- | --- | --- |",
    rows,
    "",
  ].join("\n");
}


function actionTypeFile(action: IrActionType): string {
  const frontmatterLines: [string, string][] = [
    ["type", "action_type"],
    ["id", action.id],
    ["name", action.name],
    ["tags", "[]"],
    ["action_type", action.actionType],
    ["enabled", action.enabled === true ? "true" : "false"],
  ];
  if (action.riskLevel) {
    frontmatterLines.push(["risk_level", action.riskLevel]);
  }
  if (action.requiresApproval !== undefined) {
    frontmatterLines.push(["requires_approval", action.requiresApproval ? "true" : "false"]);
  }

  const sections: string[] = [
    frontmatter(frontmatterLines),
    "",
    `## ActionType: ${action.name}`,
    "",
    action.description ?? `${action.name}操作。`,
    "",
    "### Bound Object",
    "",
    "| Bound Object | Action Type |",
    "| --- | --- |",
    `| ${action.boundObject} | ${action.actionType} |`,
    "",
  ];

  if (action.trigger) {
    const valueLine = action.trigger.value ? `  value: ${action.trigger.value}` : "";
    sections.push(
      "### Trigger Condition",
      "",
      "```yaml",
      "condition:",
      `  object_type_id: ${action.boundObject}`,
      `  field: ${action.trigger.field}`,
      `  operation: ${action.trigger.operation}`,
      ...(valueLine ? [valueLine] : []),
      "```",
      "",
    );
  }

  sections.push(
    "### Pre-conditions",
    "",
    "| Object | Check | Condition | Message |",
    "| --- | --- | --- | --- |",
    "",
    "### Scope of Impact",
    "",
    "| Object | Impact Description |",
    "| --- | --- |",
    `| ${action.boundObject} | ${tableCell(action.description ?? `${action.name}影响${action.boundObject}`)} |`,
    "",
    "### Execution Description",
    "",
    `1. 根据「${action.name}」对 ${action.boundObject} 执行 ${action.actionType} 操作`,
    "2. 记录操作前后状态，便于回滚",
    "",
  );

  return sections.join("\n");
}

function riskTypeFile(risk: IrRiskType): string {
  const sections: string[] = [
    frontmatter([
      ["type", "risk_type"],
      ["id", risk.id],
      ["name", risk.name],
      ["tags", "[]"],
    ]),
    "",
    `## RiskType: ${risk.name}`,
    "",
    risk.description ?? `${risk.name}风险管控。`,
    "",
  ];

  if (risk.policies.length > 0) {
    sections.push("### Control Policy", "", ...risk.policies.map((policy) => `- ${policy}`), "");
  }

  sections.push(
    "### Audit Requirements",
    "",
    "- 记录操作人、操作时间与审批链",
    "- 保留操作前后的对象状态快照",
    "",
  );

  return sections.join("\n");
}
function networkFile(ir: OntologyIr): string {
  const objects = ir.objectTypes.map((object) => `${object.name}（${object.id}）`).join("、") || "无";
  const relations = ir.relations.map((relation) => relation.name).join("、") || "无";
  const groups = (ir.conceptGroups ?? []).map((group) => group.name).join("、") || "无";
  const actionTypes = (ir.actionTypes ?? []).map((item) => item.id).join(", ") || "无";
  const riskTypes = (ir.riskTypes ?? []).map((item) => item.id).join(", ") || "无";

  return [
    frontmatter([
      ["type", "knowledge_network"],
      ["id", ir.networkId],
      ["name", ir.networkName],
      ["tags", "[]"],
    ]),
    "",
    `# ${ir.networkName}`,
    "",
    ir.description ?? `${ir.networkName}（由本体构建助手生成）。`,
    "",
    "## Network Overview",
    "",
    `- **ObjectTypes** (object_types/): ${objects}`,
    `- **RelationTypes** (relation_types/): ${relations}`,
    `- **ConceptGroups** (concept_groups/): ${groups}`,
    ...(actionTypes.length > 0 ? [`- **ActionTypes** (action_types/): ${actionTypes}`] : []),
    ...(riskTypes.length > 0 ? [`- **RiskTypes** (risk_types/): ${riskTypes}`] : []),
    "",
  ].join("\n");
}

/** 把 IR 编译成一套可被 `openbkn bkn validate/push` 使用的 .bkn 文件。 */
export function compileIrToBkn(ir: OntologyIr): CompiledFile[] {
  const files: CompiledFile[] = [{ path: "network.bkn", content: networkFile(ir) }];

  for (const object of ir.objectTypes) {
    files.push({ path: `object_types/${object.id}.bkn`, content: objectTypeFile(object) });
  }
  for (const relation of ir.relations) {
    files.push({ path: `relation_types/${relation.id}.bkn`, content: relationTypeFile(relation) });
  }
  for (const action of ir.actionTypes ?? []) {
    files.push({ path: `action_types/${action.id}.bkn`, content: actionTypeFile(action) });
  }
  for (const risk of ir.riskTypes ?? []) {
    files.push({ path: `risk_types/${risk.id}.bkn`, content: riskTypeFile(risk) });
  }
  for (const group of ir.conceptGroups ?? []) {
    files.push({ path: `concept_groups/${group.id}.bkn`, content: conceptGroupFile(group, ir.objectTypes) });
  }

  const bindingRows = ir.objectTypes
    .filter((object) => object.sourceTable)
    .map((object) => {
      const mapped = object.properties.filter((property) => property.mappedField).length;
      const primary = object.properties.find((property) => property.name === object.primaryKey);
      const primaryText = primary?.mappedField ? `\`${primary.mappedField}\`` : "（需在平台指定）";
      return `| ${object.name} | ${tableCell(object.sourceTable ?? "")} | ${mapped}/${object.properties.length} | ${primaryText} |`;
    });

  const relationRows = ir.relations
    .filter((relation) => (relation.mapping?.length ?? 0) > 0)
    .map((relation) => {
      const pairs = (relation.mapping ?? [])
        .map((item) => `${item.sourceProperty} → ${item.targetProperty}`)
        .join("<br>");
      return `| ${relation.name} | ${pairs} |`;
    });

  const evidenceRows = ir.objectTypes
    .filter((object) => (object.evidence?.length ?? 0) > 0)
    .map((object) => {
      const first = object.evidence?.[0];
      const source = first ? `${tableCell(first.source)}${first.location ? ` · ${tableCell(first.location)}` : ""}` : "";
      const quote = first ? tableCell(first.quote) : "";
      const withEvidence = object.properties.filter((property) => (property.evidence?.length ?? 0) > 0).length;
      return `| ${object.name} | ${source} | ${withEvidence}/${object.properties.length} | ${quote} |`;
    });

  const evidencePayload = {
    networkId: ir.networkId,
    networkName: ir.networkName,
    objects: ir.objectTypes
      .map((object) => ({
        id: object.id,
        name: object.name,
        evidence: object.evidence ?? [],
        properties: object.properties
          .filter((property) => (property.evidence?.length ?? 0) > 0)
          .map((property) => ({
            name: property.displayName ?? property.name,
            mappedField: property.mappedField ?? null,
            evidence: property.evidence ?? [],
          })),
      }))
      .filter((object) => object.evidence.length > 0 || object.properties.length > 0),
    relations: ir.relations
      .filter((relation) => (relation.evidence?.length ?? 0) > 0)
      .map((relation) => ({ id: relation.id, name: relation.name, evidence: relation.evidence ?? [] })),
    actions: (ir.actionTypes ?? [])
      .filter((action) => (action.evidence?.length ?? 0) > 0)
      .map((action) => ({ id: action.id, name: action.name, evidence: action.evidence ?? [] })),
    risks: (ir.riskTypes ?? [])
      .filter((risk) => (risk.evidence?.length ?? 0) > 0)
      .map((risk) => ({ id: risk.id, name: risk.name, evidence: risk.evidence ?? [] })),
  };
  const hasEvidence =
    evidencePayload.objects.length > 0 ||
    evidencePayload.relations.length > 0 ||
    evidencePayload.actions.length > 0 ||
    evidencePayload.risks.length > 0;
  if (hasEvidence) {
    files.push({ path: "evidence.json", content: JSON.stringify(evidencePayload, null, 2) });
  }

  files.push({
    path: "README.md",
    content: [
      `# ${ir.networkName}`,
      "",
      "本目录由「九格多源本体构建系统」的智能构建功能生成：",
      "",
      "```bash",
      "openbkn bkn validate ./",
      "openbkn bkn push ./ --binding-policy detach",
      "```",
      "",
      `- 对象类型：${ir.objectTypes.length}`,
      `- 关系类型：${ir.relations.length}`,
      `- 行动类型：${(ir.actionTypes ?? []).length}`,
      `- 风险类型：${(ir.riskTypes ?? []).length}`,
      `- 概念分组：${(ir.conceptGroups ?? []).length}`,
      "",
      ...(bindingRows.length > 0
        ? [
            "## 数据绑定建议（导入平台后按此表绑定数据来源）",
            "",
            "| 对象类型 | 来源表 | 字段映射 | 主键列 |",
            "| --- | --- | --- | --- |",
            ...bindingRows,
            "",
          ]
        : []),
      ...(relationRows.length > 0
        ? [
            "## 关系键映射（外键推断，供平台建立关系时使用）",
            "",
            "| 关系 | 源字段 → 目标字段 |",
            "| --- | --- |",
            ...relationRows,
            "",
          ]
        : []),
      ...(evidenceRows.length > 0
        ? [
            "## 证据引用（对象/属性在输入材料中的出处）",
            "",
            "| 对象类型 | 来源 | 有证据的属性 | 原文片段 |",
            "| --- | --- | --- | --- |",
            ...evidenceRows,
            "",
            "完整的逐项证据见本目录 `evidence.json`。",
            "",
          ]
        : []),
    ].join("\n"),
  });

  return files;
}
