/**
 * 数据库结构（DDL / mysqldump --no-data / pg_dump -s 等）解析与转换。
 *
 * 目标：在没有业务描述文档、只有数据库结构的真实场景下，先得到统一的 Schema 模型，
 * 再生成「给模型看的紧凑描述」与「无模型时的规则抽取结果」。
 */

import type { IrActionType, IrRiskType } from "./types/ir.js";

export type SchemaDialect = "mysql" | "postgres" | "oracle" | "mssql" | "unknown";

export type SchemaColumn = {
  name: string;
  type: string;
  comment?: string;
  nullable?: boolean;
  primaryKey?: boolean;
};

export type SchemaForeignKey = {
  column: string;
  refTable: string;
  refColumn: string;
};

export type SchemaTable = {
  name: string;
  comment?: string;
  columns: SchemaColumn[];
  foreignKeys: SchemaForeignKey[];
  /** 只在内省（直连数据库）时填充的样例值，用于类型/外键推断。 */
  samples?: string[][];
};

export type Schema = {
  dialect: SchemaDialect;
  tables: SchemaTable[];
  hasComments: boolean;
};

export type SchemaStats = {
  dialect: SchemaDialect;
  tables: number;
  columns: number;
  foreignKeys: number;
  commentedTables: number;
  commentedColumns: number;
  hasComments: boolean;
};

const SQL_KEYWORDS = new Set([
  "KEY",
  "INDEX",
  "UNIQUE",
  "CONSTRAINT",
  "CHECK",
  "PRIMARY",
  "FOREIGN",
  "FULLTEXT",
  "SPATIAL",
]);

const TYPE_MAP: Array<{ pattern: RegExp; type: string }> = [
  { pattern: /^(tinyint|smallint|mediumint|int|integer|bigint|serial|smallserial|bigserial)\b/i, type: "integer" },
  { pattern: /^(decimal|numeric|number|float|double|real|money)\b/i, type: "number" },
  { pattern: /^(tinyint\(1\)|bool|boolean|bit)\b/i, type: "boolean" },
  { pattern: /^(date|datetime|timestamp|time|timestamptz)\b/i, type: "datetime" },
];

/** SQL 基础类型映射到 IR 属性类型。 */
export function mapSqlType(raw: string): string {
  const type = raw.trim();
  for (const item of TYPE_MAP) {
    if (item.pattern.test(type)) {
      if (item.type === "integer" && /^tinyint\(1\)/i.test(type)) {
        return "boolean";
      }
      return item.type;
    }
  }
  return "string";
}

function stripQuotes(value: string): string {
  return value.replace(/^[`"[]|[`"\]]$/g, "").trim();
}

/** 把表名转成属性/对象 id（去掉常见前缀，非 ASCII 用兜底）。 */
export function slugName(raw: string, fallback: string): string {
  const ascii = raw
    .toLowerCase()
    .replace(/^(t_|tb_|tbl_|dim_|fact_|ods_|dwd_|dws_)/, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return ascii || fallback;
}

function splitTopLevel(body: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  let quote: string | null = null;
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (quote) {
      current += char;
      if (char === quote && body[index - 1] !== "\\") {
        quote = null;
      }
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
      current += char;
      continue;
    }
    if (char === "(") {
      depth += 1;
      current += char;
      continue;
    }
    if (char === ")") {
      depth = Math.max(0, depth - 1);
      current += char;
      continue;
    }
    if (char === "," && depth === 0) {
      parts.push(current.trim());
      current = "";
      continue;
    }
    current += char;
  }
  if (current.trim()) {
    parts.push(current.trim());
  }
  return parts;
}

function findMatchingParen(text: string, openIndex: number): number {
  let depth = 0;
  let quote: string | null = null;
  for (let index = openIndex; index < text.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === quote && text[index - 1] !== "\\") {
        quote = null;
      }
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
      continue;
    }
    if (char === "(") depth += 1;
    if (char === ")") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

/** 去掉 SQL 注释（保留字符串常量内容）。 */
function stripSqlComments(text: string): string {
  let result = "";
  let quote: string | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];
    if (quote) {
      result += char;
      if (char === quote && text[index - 1] !== "\\") quote = null;
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
      result += char;
      continue;
    }
    if (char === "-" && next === "-") {
      while (index < text.length && text[index] !== "\n") index += 1;
      result += "\n";
      continue;
    }
    if (char === "#") {
      while (index < text.length && text[index] !== "\n") index += 1;
      result += "\n";
      continue;
    }
    if (char === "/" && next === "*") {
      index += 2;
      while (index < text.length && !(text[index] === "*" && text[index + 1] === "/")) index += 1;
      index += 1;
      continue;
    }
    result += char;
  }
  return result;
}

function parseColumnDefinition(body: string): SchemaColumn | null {
  const trimmed = body.trim();
  if (!trimmed) return null;
  const head = trimmed.split(/\s+/)[0] ?? "";
  const name = stripQuotes(head);
  if (!name || SQL_KEYWORDS.has(name.toUpperCase())) return null;
  if (/^(primary|foreign|unique|key|index|constraint|check)\b/i.test(trimmed)) return null;

  const rest = trimmed.slice(head.length).trim();
  const typeMatch = rest.match(/^([a-zA-Z_][\w]*\s*(?:\([^)]*\))?)/);
  const type = typeMatch?.[1]?.trim() ?? "varchar";
  const commentMatch = rest.match(/COMMENT\s+'((?:[^']|'')*)'/i);
  const comment = commentMatch ? commentMatch[1].replace(/''/g, "'").trim() : undefined;
  const nullable = !/NOT\s+NULL/i.test(rest);
  const primaryKey = /\bPRIMARY\s+KEY\b/i.test(rest);
  return {
    name,
    type,
    comment: comment || undefined,
    nullable,
    primaryKey,
  };
}

function parseKeyColumns(value: string): string[] {
  return value
    .split(",")
    .map((item) => stripQuotes(item.trim().split(/\s+/)[0] ?? ""))
    .filter(Boolean);
}

/** 解析单个 CREATE TABLE 语句。 */
function parseCreateTable(statement: string, dialect: SchemaDialect): SchemaTable | null {
  const match = statement.match(
    /CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`"[]?[\w.]+[`"\]]?)\s*\(/i,
  );
  if (!match) return null;
  const rawName = match[1];
  const tableName = stripQuotes(rawName).split(".").pop() ?? stripQuotes(rawName);
  if (!tableName) return null;
  const openIndex = statement.indexOf("(", match.index ?? 0);
  if (openIndex < 0) return null;
  const closeIndex = findMatchingParen(statement, openIndex);
  if (closeIndex < 0) return null;
  const body = statement.slice(openIndex + 1, closeIndex);
  const tail = statement.slice(closeIndex + 1);

  const columns: SchemaColumn[] = [];
  const foreignKeys: SchemaForeignKey[] = [];
  const primaryKeys: string[] = [];

  for (const item of splitTopLevel(body)) {
    const upper = item.toUpperCase();
    if (/^\s*PRIMARY\s+KEY\s*\(/i.test(item)) {
      const inside = item.slice(item.indexOf("(") + 1, item.lastIndexOf(")"));
      primaryKeys.push(...parseKeyColumns(inside));
      continue;
    }
    if (/^\s*(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(/i.test(item)) {
      const fkMatch = item.match(
        /FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+([`"[]?[\w.]+[`"\]]?)\s*\(([^)]+)\)/i,
      );
      if (fkMatch) {
        const localColumns = parseKeyColumns(fkMatch[1]);
        const refTable = stripQuotes(fkMatch[2]).split(".").pop() ?? stripQuotes(fkMatch[2]);
        const refColumns = parseKeyColumns(fkMatch[3]);
        localColumns.forEach((column, index) => {
          foreignKeys.push({
            column,
            refTable,
            refColumn: refColumns[index] ?? refColumns[0] ?? "id",
          });
        });
      }
      continue;
    }
    if (/^\s*(UNIQUE|KEY|INDEX|CHECK|CONSTRAINT|FULLTEXT|SPATIAL)\b/i.test(upper)) {
      continue;
    }
    const column = parseColumnDefinition(item);
    if (column) {
      if (column.primaryKey) primaryKeys.push(column.name);
      columns.push(column);
    }
  }

  // MySQL 表注释：ENGINE=InnoDB ... COMMENT='设备台账'
  const tableCommentMatch = tail.match(/COMMENT\s*=\s*'((?:[^']|'')*)'/i);
  const tableComment = tableCommentMatch
    ? tableCommentMatch[1].replace(/''/g, "'").trim()
    : undefined;

  for (const key of primaryKeys) {
    const column = columns.find((item) => item.name.toLowerCase() === key.toLowerCase());
    if (column) column.primaryKey = true;
  }

  return {
    name: tableName,
    comment: tableComment || undefined,
    columns,
    foreignKeys,
  };
}

/** 解析 COMMENT ON TABLE/COLUMN 语句（PostgreSQL / Oracle 风格）。 */
function applyCommentStatements(text: string, tables: SchemaTable[]): boolean {
  let hit = false;
  const tableCommentRegex =
    /COMMENT\s+ON\s+TABLE\s+([`"[]?[\w.]+[`"\]]?)\s+IS\s+'((?:[^']|'')*)'/gi;
  for (const match of text.matchAll(tableCommentRegex)) {
    const tableName = stripQuotes(match[1]).split(".").pop() ?? stripQuotes(match[1]);
    const table = tables.find((item) => item.name.toLowerCase() === tableName.toLowerCase());
    if (table) {
      table.comment = match[2].replace(/''/g, "'").trim() || undefined;
      hit = true;
    }
  }
  const columnCommentRegex =
    /COMMENT\s+ON\s+COLUMN\s+([`"[]?[\w.]+[`"\]]?)\s*\.\s*([`"[]?[\w.]+[`"\]]?)\s+IS\s+'((?:[^']|'')*)'/gi;
  for (const match of text.matchAll(columnCommentRegex)) {
    const tableName = stripQuotes(match[1]);
    const columnName = stripQuotes(match[2]);
    const table = tables.find((item) => item.name.toLowerCase() === tableName.toLowerCase());
    const column = table?.columns.find(
      (item) => item.name.toLowerCase() === columnName.toLowerCase(),
    );
    if (column) {
      column.comment = match[3].replace(/''/g, "'").trim() || undefined;
      hit = true;
    }
  }
  return hit;
}

/** 解析 ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY 语句。 */
function applyAlterForeignKeys(text: string, tables: SchemaTable[]): void {
  const regex =
    /ALTER\s+TABLE\s+([`"[]?[\w.]+[`"\]]?)\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+([`"[]?[\w.]+[`"\]]?)\s*\(([^)]+)\)/gi;
  for (const match of text.matchAll(regex)) {
    const tableName = stripQuotes(match[1]).split(".").pop() ?? stripQuotes(match[1]);
    const refTable = stripQuotes(match[3]).split(".").pop() ?? stripQuotes(match[3]);
    const localColumns = parseKeyColumns(match[2]);
    const refColumns = parseKeyColumns(match[4]);
    const table = tables.find((item) => item.name.toLowerCase() === tableName.toLowerCase());
    if (!table) continue;
    localColumns.forEach((column, index) => {
      if (table.foreignKeys.some((item) => item.column === column)) return;
      table.foreignKeys.push({
        column,
        refTable,
        refColumn: refColumns[index] ?? refColumns[0] ?? "id",
      });
    });
  }
}

function detectDialect(text: string): SchemaDialect {
  const sample = text.slice(0, 60_000);
  if (/`\w+`/.test(sample) || /\bENGINE\s*=/i.test(sample)) return "mysql";
  if (/VARCHAR2\s*\(/i.test(sample) || /\bNUMBER\s*\(/i.test(sample)) return "oracle";
  if (/\[dbo\]|\[[A-Za-z_][\w]*\]\s+(?:int|nvarchar)/i.test(sample) || /\bIDENTITY\s*\(/i.test(sample)) {
    return "mssql";
  }
  if (/::\s*\w+|SERIAL\b|\bJSONB\b|\bUUID\b/i.test(sample)) return "postgres";
  return "unknown";
}

/** 解析 SQL 文本为统一 Schema；多个文件的结果可再次合并。 */
export function parseSqlSchema(text: string): Schema {
  const dialect = detectDialect(text);
  const cleaned = stripSqlComments(text);
  const tables: SchemaTable[] = [];
  const createRegex = /CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?/gi;
  for (const match of cleaned.matchAll(createRegex)) {
    const start = match.index ?? 0;
    const openIndex = cleaned.indexOf("(", start);
    if (openIndex < 0) continue;
    const closeIndex = findMatchingParen(cleaned, openIndex);
    if (closeIndex < 0) continue;
    const end = cleaned.indexOf(";", closeIndex);
    const statement = cleaned.slice(start, end < 0 ? cleaned.length : end + 1);
    const table = parseCreateTable(statement, dialect);
    if (table && table.columns.length > 0) {
      tables.push(table);
    }
  }

  applyCommentStatements(cleaned, tables);
  applyAlterForeignKeys(cleaned, tables);

  const commentedTables = tables.filter((table) => Boolean(table.comment)).length;
  const commentedColumns = tables.reduce(
    (total, table) => total + table.columns.filter((column) => Boolean(column.comment)).length,
    0,
  );
  const hasComments = commentedTables > 0 || commentedColumns > 0;
  return { dialect, tables, hasComments };
}

/** 合并多个 SQL 文件解析结果（同名表按列取并集）。 */
export function mergeSchemas(schemas: Schema[]): Schema {
  const merged = new Map<string, SchemaTable>();
  let dialect: SchemaDialect = "unknown";
  for (const schema of schemas) {
    if (dialect === "unknown" && schema.dialect !== "unknown") dialect = schema.dialect;
    for (const table of schema.tables) {
      const key = table.name.toLowerCase();
      const existing = merged.get(key);
      if (!existing) {
        merged.set(key, {
          name: table.name,
          comment: table.comment,
          columns: [...table.columns],
          foreignKeys: [...table.foreignKeys],
        });
        continue;
      }
      existing.comment = existing.comment ?? table.comment;
      for (const column of table.columns) {
        const hit = existing.columns.find(
          (item) => item.name.toLowerCase() === column.name.toLowerCase(),
        );
        if (!hit) {
          existing.columns.push(column);
        } else {
          hit.comment = hit.comment ?? column.comment;
          hit.type = hit.type || column.type;
          hit.primaryKey = hit.primaryKey || column.primaryKey;
        }
      }
      for (const fk of table.foreignKeys) {
        if (!existing.foreignKeys.some((item) => item.column === fk.column)) {
          existing.foreignKeys.push(fk);
        }
      }
    }
  }
  const tables = [...merged.values()];
  const commentedTables = tables.filter((table) => Boolean(table.comment)).length;
  const commentedColumns = tables.reduce(
    (total, table) => total + table.columns.filter((column) => Boolean(column.comment)).length,
    0,
  );
  return {
    dialect,
    tables,
    hasComments: commentedTables > 0 || commentedColumns > 0,
  };
}

/** 生成给模型看的紧凑结构描述（带注释、主键、外键）。 */
export function buildSchemaBrief(schema: Schema, maxChars = 120_000): string {
  const chunks: string[] = [];
  chunks.push(`# 数据库结构（方言：${schema.dialect}）`);
  if (!schema.hasComments) {
    chunks.push("（说明：该库没有表/列注释，请根据表名、列名与常见命名规范推断业务语义。）");
  }
  for (const table of schema.tables) {
    const tableTitle = table.comment ? `${table.name}（${table.comment}）` : table.name;
    chunks.push(`## 表 ${tableTitle}`);
    const lines: string[] = [];
    for (const column of table.columns) {
      const flags: string[] = [];
      if (column.primaryKey) flags.push("主键");
      if (column.nullable === false) flags.push("非空");
      const columnTitle = column.comment
        ? `${column.name}（${column.comment}）`
        : column.name;
      lines.push(
        `- ${columnTitle} ${column.type}${flags.length > 0 ? ` [${flags.join(",")}]` : ""}`,
      );
    }
    for (const fk of table.foreignKeys) {
      lines.push(`- 外键：${fk.column} → ${fk.refTable}.${fk.refColumn}`);
    }
    chunks.push(lines.join("\n"));
    if (chunks.join("\n").length > maxChars) {
      chunks.push("（结构过大，已截断）");
      break;
    }
  }
  return chunks.join("\n");
}

export function schemaStats(schema: Schema): SchemaStats {
  const commentedTables = schema.tables.filter((table) => Boolean(table.comment)).length;
  const commentedColumns = schema.tables.reduce(
    (total, table) => total + table.columns.filter((column) => Boolean(column.comment)).length,
    0,
  );
  return {
    dialect: schema.dialect,
    tables: schema.tables.length,
    columns: schema.tables.reduce((total, table) => total + table.columns.length, 0),
    foreignKeys: schema.tables.reduce((total, table) => total + table.foreignKeys.length, 0),
    commentedTables,
    commentedColumns,
    hasComments: schema.hasComments,
  };
}

/** 把 Schema 转成 binding 推断需要的表格结构（列名作为表头）。 */
export function schemaToTables(schema: Schema): Array<{
  name: string;
  headers: string[];
  rows: number;
  samples: string[][];
}> {
  return schema.tables.map((table) => ({
    name: table.name,
    headers: table.columns.map((column) => column.name),
    rows: table.samples?.length ?? 0,
    samples: table.samples ?? [],
  }));
}

function humanizeTableName(name: string): string {
  return name
    .replace(/^(t_|tb_|tbl_|dim_|fact_)/i, "")
    .replace(/[_-]+/g, " ")
    .trim();
}

/** 无模型时的规则抽取：表→对象、列→属性、外键→关系。 */
export function schemaToIr(
  schema: Schema,
  networkName: string,
): {
  networkName: string;
  objects: Array<Record<string, unknown>>;
  relations: Array<Record<string, unknown>>;
  actionTypes: Array<Record<string, unknown>>;
  riskTypes: Array<Record<string, unknown>>;
  questions: string[];
} {
  const idByTable = new Map<string, string>();
  const objectByTable = new Map<string, Record<string, unknown>>();
  const objects: Array<Record<string, unknown>> = [];

  schema.tables.forEach((table, tableIndex) => {
    const objectId = slugName(table.name, `object_${tableIndex + 1}`);
    idByTable.set(table.name.toLowerCase(), objectId);
    const properties = table.columns.map((column, columnIndex) => ({
      name: slugName(column.name, `column_${columnIndex + 1}`),
      displayName: column.comment || column.name,
      type: mapSqlType(column.type),
      mappedField: column.name,
    }));
    const primary = table.columns.find((column) => column.primaryKey);
    const primaryProperty = primary
      ? properties.find((property) => property.mappedField === primary.name)?.name
      : undefined;
    const object: Record<string, unknown> = {
      id: objectId,
      name: table.comment || humanizeTableName(table.name),
      description: table.comment || undefined,
      sourceTable: table.name,
      primaryKey: primaryProperty,
      properties,
    };
    objects.push(object);
    objectByTable.set(table.name.toLowerCase(), object);
  });

  const relations: Array<Record<string, unknown>> = [];
  const seen = new Set<string>();
  for (const table of schema.tables) {
    const sourceId = idByTable.get(table.name.toLowerCase());
    const sourceObject = objectByTable.get(table.name.toLowerCase());
    if (!sourceId || !sourceObject) continue;
    for (const fk of table.foreignKeys) {
      const targetId = idByTable.get(fk.refTable.toLowerCase());
      if (!targetId || targetId === sourceId) continue;
      const key = `${sourceId}->${targetId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const targetObject = objectByTable.get(fk.refTable.toLowerCase());
      const sourceProperties = (sourceObject.properties ?? []) as Array<Record<string, unknown>>;
      const targetProperties = (targetObject?.properties ?? []) as Array<Record<string, unknown>>;
      const sourceProperty = sourceProperties.find(
        (property) => property.mappedField === fk.column,
      );
      const targetProperty = targetProperties.find(
        (property) => property.mappedField === fk.refColumn,
      );
      relations.push({
        id: `${sourceId}_${targetId}`,
        name: `引用${String(targetObject?.name ?? fk.refTable)}`,
        source: sourceId,
        target: targetId,
        mapping:
          sourceProperty && targetProperty
            ? [
                {
                  sourceProperty: String(sourceProperty.name),
                  targetProperty: String(targetProperty.name),
                },
              ]
            : undefined,
      });
    }
  }

  const questions: string[] = [];
  if (!schema.hasComments) {
    questions.push("这个库没有表/列注释，请确认：主要业务对象（表）分别代表什么？");
  }
  const withoutPrimary = objects.filter((object) => !object.primaryKey).length;
  if (withoutPrimary > 0) {
    questions.push(`有 ${withoutPrimary} 张表没有主键，请确认用什么列唯一标识一条记录。`);
  }
  const fkCount = schema.tables.reduce((total, table) => total + table.foreignKeys.length, 0);
  if (fkCount === 0 && schema.tables.length > 1) {
    questions.push("库中没有外键约束，对象之间的关系需要人工确认（如订单表与设备表如何关联）。");
  }

    const governance = deriveActionsAndRisks(
    schema,
    objects as unknown as Array<{ id: string; name: string; sourceTable?: string }>,
  );

  return {
    networkName,
    objects,
    relations,
    actionTypes: governance.actionTypes,
    riskTypes: governance.riskTypes,
    questions,
  };
}

const STATUS_COLUMN_PATTERN = /(^|_)(status|state|stage|status_code|workflow_status)$/i;
const STATUS_COLUMN_CJK = /(状态|进展|阶段)$/;
const LEVEL_COLUMN_PATTERN = /(^|_)(level|severity|priority|grade|risk_level)$/i;
const LEVEL_COLUMN_CJK = /(级别|严重程度|优先级|等级)$/;

export type DerivedGovernance = {
  actionTypes: IrActionType[];
  riskTypes: IrRiskType[];
};

/**
 * 无业务描述时，从结构语义推断「行动类型 / 风险类型」：
 * - 有状态类列（status/state/状态）→ 创建 / 处理 / 关闭 三个动作；
 * - 有等级类列（level/severity/优先级）→ 一条「高等级未及时处理」风险与管控策略。
 */
export function deriveActionsAndRisks(
  schema: Schema,
  objects: Array<{ id: string; name: string; sourceTable?: string }>,
): DerivedGovernance {
  const tableByName = new Map(schema.tables.map((table) => [table.name.toLowerCase(), table]));
  const actionTypes: IrActionType[] = [];
  const riskTypes: IrRiskType[] = [];
  const seenActions = new Set<string>();
  const maxActions = 6;
  const maxRisks = 3;

  for (const object of objects) {
    if (!object.sourceTable) continue;
    const table = tableByName.get(object.sourceTable.toLowerCase());
    if (!table) continue;
    const statusColumn = table.columns.find(
      (column) => STATUS_COLUMN_PATTERN.test(column.name) || STATUS_COLUMN_CJK.test(column.name),
    );
    const levelColumn = table.columns.find(
      (column) => LEVEL_COLUMN_PATTERN.test(column.name) || LEVEL_COLUMN_CJK.test(column.name),
    );

    if (statusColumn) {
      const candidates: Array<{ verb: string; actionType: "add" | "modify"; riskLevel: "low" | "medium" }> = [
        { verb: "创建", actionType: "add", riskLevel: "low" },
        { verb: "处理", actionType: "modify", riskLevel: "medium" },
        { verb: "关闭", actionType: "modify", riskLevel: "low" },
      ];
      for (const candidate of candidates) {
        if (actionTypes.length >= maxActions) break;
        const name = `${candidate.verb}${object.name}`.slice(0, 12);
        if (seenActions.has(name)) continue;
        seenActions.add(name);
        actionTypes.push({
          id: `action_${object.id}_${actionTypes.length + 1}`,
          name,
          description: `根据 ${table.name}.${statusColumn.name} 的状态流转推断：${candidate.verb}${object.name}`,
          actionType: candidate.actionType,
          boundObject: object.id,
          riskLevel: candidate.riskLevel,
          enabled: true,
          requiresApproval: false,
        });
      }
    }

    if (levelColumn && riskTypes.length < maxRisks) {
      riskTypes.push({
        id: `risk_${object.id}`,
        name: `${object.name}高等级未及时处理`,
        description: `根据 ${table.name}.${levelColumn.name} 的分级字段推断`,
        policies: [
          `高等级${object.name}超过 2 小时未处理时升级到班组负责人`,
          "值班人员收到短信或站内通知，超时未响应自动再升级",
          `关闭${object.name}前必须填写处理结论与处理人`,
        ],
      });
    }
  }

  return { actionTypes, riskTypes };
}