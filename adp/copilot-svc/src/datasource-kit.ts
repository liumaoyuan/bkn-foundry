/**
 * 数据接入包：把「本体草案 + 推断出的数据绑定」编译成平台侧可执行的接入材料。
 *
 * 三个文件：
 * - views.sql     按对象属性名生成视图列别名（0.1.5 不接受 mapped_field，只能靠列名对齐）
 * - binding.json  对象 ↔ 表/视图、主键、属性列映射、关系键映射清单
 * - README.md     平台侧四步操作指引
 */
import type { IrObjectType, IrRelation, OntologyIr } from "./types/ir.js";

export type BindingKitFile = { name: string; content: string };

export type BindingKitResult = {
  files: BindingKitFile[];
  warnings: string[];
  stats: { objects: number; views: number; properties: number; relations: number };
};

type BindingKitOptions = {
  /** 生成 SQL 时使用的方言：mysql 用反引号，postgres 用双引号。 */
  dialect?: "mysql" | "postgres";
};

const IDENTIFIER_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

function viewName(objectType: IrObjectType): string {
  const base = objectType.id.replace(/[^A-Za-z0-9_]/g, "_");
  const safe = base.length > 0 ? base : "object";
  return `v_${safe}`;
}

function quoteIdent(name: string, quote: "`" | '"'): string {
  if (IDENTIFIER_PATTERN.test(name)) {
    return name;
  }
  return `${quote}${name.split(quote).join(quote + quote)}${quote}`;
}

function quoteTableName(name: string, quote: "`" | '"'): string {
  const segments = name
    .split(".")
    .map((segment) => quoteIdent(segment.trim(), quote))
    .filter((segment) => segment.length > 0);
  return segments.length > 0 ? segments.join(".") : quoteIdent(name.trim(), quote);
}

function buildView(
  objectType: IrObjectType,
  quote: "`" | '"',
  warnings: string[],
): { sql: string; properties: Array<{ property: string; column: string }> } | null {
  if (!objectType.sourceTable) {
    warnings.push(`对象「${objectType.name}」没有来源表，未生成视图，需要在平台手工指定数据来源。`);
    return null;
  }

  const mapped = objectType.properties
    .filter((property) => Boolean(property.mappedField))
    .map((property) => ({ property: property.name, column: property.mappedField ?? property.name }));

  if (mapped.length === 0) {
    warnings.push(`对象「${objectType.name}」的字段没有推断出原列名，未生成视图。`);
    return null;
  }

  const columns = mapped
    .map((item) => `  ${quoteIdent(item.column, quote)} as ${quote}${item.property}${quote}`)
    .join(",\n");

  // 视图名统一加引号，避免不同库对大小写/保留字的处理差异。
  const quotedView = `${quote}${viewName(objectType)}${quote}`;

  const sql = [
    `-- ${objectType.name}（${objectType.id}）← ${objectType.sourceTable}`,
    `create or replace view ${quotedView} as`,
    "select",
    columns,
    `from ${quoteTableName(objectType.sourceTable, quote)};`,
  ].join("\n");

  return { sql, properties: mapped };
}

/** 生成 views.sql / binding.json / README.md 三个文件。 */
export function buildBindingKit(ir: OntologyIr, options: BindingKitOptions = {}): BindingKitResult {
  const dialect = options.dialect === "postgres" ? "postgres" : "mysql";
  const quote = dialect === "postgres" ? '"' : "`";
  const warnings: string[] = [];
  const views: string[] = [];
  const objects: Array<{
    objectId: string;
    objectName: string;
    sourceTable: string;
    view: string;
    primaryKey: string | null;
    properties: Array<{ property: string; column: string }>;
  }> = [];
  let propertyCount = 0;

  for (const objectType of ir.objectTypes) {
    const built = buildView(objectType, quote, warnings);
    if (!built) {
      continue;
    }
    views.push(built.sql);
    objects.push({
      objectId: objectType.id,
      objectName: objectType.name,
      sourceTable: objectType.sourceTable ?? "",
      view: viewName(objectType),
      primaryKey: objectType.primaryKey ?? null,
      properties: built.properties,
    });
    propertyCount += built.properties.length;
  }

  const relations = ir.relations
    .filter((relation) => (relation.mapping?.length ?? 0) > 0)
    .map((relation: IrRelation) => ({
      relationId: relation.id,
      relationName: relation.name,
      source: relation.source,
      target: relation.target,
      mapping: relation.mapping ?? [],
    }));

  if (views.length === 0) {
    warnings.push("没有任何对象绑定到数据表；请先上传数据表或 SQL 结构，再重新生成。");
  }

  const header = [
    `-- 数据接入视图（${ir.networkName} / ${ir.networkId}）`,
    "-- 目的：把原表列名别名成对象属性名，平台建资源后即可按字段同名绑定。",
    `-- 方言：${dialect}；每段可单独执行。`,
    "",
  ].join("\n");
  const viewSql = views.length > 0 ? `${header}\n${views.join("\n\n")}\n` : `${header}\n`;

  const bindingJson = `${JSON.stringify(
    {
      network: { id: ir.networkId, name: ir.networkName },
      generatedAt: new Date().toISOString(),
      dialect,
      objects,
      relations,
      skipped: ir.objectTypes
        .filter((objectType) => !objects.some((item) => item.objectId === objectType.id))
        .map((objectType) => ({ objectId: objectType.id, objectName: objectType.name })),
    },
    null,
    2,
  )}\n`;

  const readme = [
    `# 数据接入包：${ir.networkName}`,
    "",
    "本目录由「九格多源本体构建系统」的智能构建功能生成，用于把本体草案接到真实数据上。",
    "",
    "| 文件 | 用途 |",
    "| --- | --- |",
    "| `views.sql` | 在原库执行，生成列名与对象属性一致的视图 |",
    "| `binding.json` | 对象 ↔ 表/视图、主键、字段与关系键映射清单，可交给平台管理员核对 |",
    "| `README.md` | 本文档 |",
    "",
    "## 平台侧四步",
    "",
    "1. **执行视图**：在业务库执行 `views.sql`（只建视图，不改原表）。",
    "2. **建数据源**：进入「数据资源知识网络 → 数据源」，新建与业务库对应的数据源（类型选 `resource`）。",
    "3. **建资源**：在数据源下新建资源，选择上一步生成的 `<对象 id> 视图`，确认主键列。",
    "4. **绑定对象**：打开本知识网络的「对象类型」，逐个对象在数据来源处选择对应资源；字段按同名自动匹配，未匹配的手工指定。",
    "",
    "## 注意事项",
    "",
    "- 0.1.5 平台不支持导入 `mapped_field`，所以必须用视图把原列名别名成属性名。",
    `- 本次共 ${objects.length} 个对象生成视图、${propertyCount} 个字段参与映射；其余对象见 \`binding.json\` 的 \`skipped\`。`,
    targetRelationsHint(relations) +
      "",
    "## 对象清单",
    "",
    "| 对象 | 来源表 | 视图 | 主键 | 字段数 |",
    "| --- | --- | --- | --- | --- |",
    ...objects.map(
      (item) =>
        `| ${item.objectName} | ${item.sourceTable} | ${item.view} | ${item.primaryKey ?? "（需在平台指定）"} | ${item.properties.length} |`,
    ),
    "",
  ].join("\n");

  return {
    files: [
      { name: "views.sql", content: viewSql },
      { name: "binding.json", content: bindingJson },
      { name: "README.md", content: readme },
    ],
    warnings,
    stats: {
      objects: ir.objectTypes.length,
      views: objects.length,
      properties: propertyCount,
      relations: relations.length,
    },
  };
}

function targetRelationsHint(relations: Array<{ relationName: string; mapping: unknown[] }>): string {
  if (relations.length === 0) {
    return "";
  }
  const names = relations.map((relation) => relation.relationName).join("、");
  return `- 以下 ${relations.length} 条关系已推断出字段映射，导入后可在关系类型里核对：${names}。\n`;
}
