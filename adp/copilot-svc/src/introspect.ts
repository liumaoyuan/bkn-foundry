/**
 * 直连数据库内省（P3）：只读连接 MySQL / PostgreSQL，读取 information_schema 与少量样例数据，
 * 组装成与 DDL 解析同构的 Schema，复用后续「结构 → 本体」的分析链路。
 *
 * 安全约束：
 * - 只执行固定的元数据查询与 `LIMIT n` 抽样，不接受用户 SQL；
 * - 会话级只读（MySQL `SET SESSION TRANSACTION READ ONLY` / PG `default_transaction_read_only=on`）；
 * - 连接超时 8s、查询超时 15s、表数上限 200、抽样表数上限 30；
 * - 密码不落日志、不持久化。
 */
import { createConnection, type RowDataPacket } from "mysql2/promise";
import { Client } from "pg";

import type { Schema, SchemaColumn, SchemaForeignKey, SchemaTable } from "./schema.js";

export type IntrospectDialect = "mysql" | "postgres";

export type IntrospectTarget = {
  dialect: IntrospectDialect;
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
  /** PostgreSQL 的 schema，默认 public。 */
  schema?: string;
  maxTables?: number;
};

export type IntrospectResult = {
  schema: Schema;
  scannedTables: number;
  serverVersion: string;
};

const MAX_TABLES = 200;
const MAX_SAMPLED_TABLES = 30;
const SAMPLE_ROWS = 5;
const CONNECT_TIMEOUT_MS = 8_000;
const QUERY_TIMEOUT_MS = 15_000;

const DEFAULT_PORTS: Record<IntrospectDialect, number> = { mysql: 3306, postgres: 5432 };

function readText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** 校验并补全连接参数；错误信息面向界面，不包含密码。 */
export function normalizeIntrospectTarget(
  raw: unknown,
): { ok: true; target: IntrospectTarget } | { ok: false; errors: string[] } {
  const body = (raw ?? {}) as Record<string, unknown>;
  const errors: string[] = [];
  const dialect = readText(body.dialect).toLowerCase();

  if (dialect !== "mysql" && dialect !== "postgres") {
    errors.push("请选择数据库类型：MySQL 或 PostgreSQL");
  }

  const host = readText(body.host);
  if (!host) {
    errors.push("请填写数据库地址");
  }

  const database = readText(body.database);
  if (!database) {
    errors.push("请填写数据库名");
  }

  const user = readText(body.user);
  if (!user) {
    errors.push("请填写数据库用户名");
  }

  const password = typeof body.password === "string" ? body.password : "";
  if (!password) {
    errors.push("请填写数据库密码");
  }

  const portInput = Number(body.port);
  const fallbackPort = dialect === "postgres" ? DEFAULT_PORTS.postgres : DEFAULT_PORTS.mysql;
  const port = Number.isFinite(portInput) && portInput > 0 && portInput < 65536 ? Math.trunc(portInput) : fallbackPort;

  const maxTablesInput = Number(body.maxTables);
  const maxTables =
    Number.isFinite(maxTablesInput) && maxTablesInput > 0
      ? Math.min(Math.trunc(maxTablesInput), MAX_TABLES)
      : MAX_TABLES;

  if (errors.length > 0) {
    return { ok: false, errors };
  }

  const schema = readText(body.schema) || undefined;
  return {
    ok: true,
    target: {
      dialect: dialect as IntrospectDialect,
      host,
      port,
      database,
      user,
      password,
      schema,
      maxTables,
    },
  };
}

/** 把元数据行组装成 Schema（导出以便单测，不依赖真实数据库）。 */
export function buildSchemaFromMetadata(input: {
  dialect: IntrospectDialect;
  tables: Array<{ name: string; comment?: string; samples?: string[][] }>;
  columns: Array<{
    table: string;
    name: string;
    type: string;
    comment?: string;
    nullable?: boolean;
    primaryKey?: boolean;
  }>;
  foreignKeys: Array<{ table: string; column: string; refTable: string; refColumn: string }>;
}): Schema {
  const byTable = new Map<string, SchemaTable>();
  for (const table of input.tables) {
    byTable.set(table.name, {
      name: table.name,
      comment: table.comment || undefined,
      columns: [],
      foreignKeys: [],
      samples: table.samples,
    });
  }

  for (const column of input.columns) {
    const table = byTable.get(column.table);
    if (!table) {
      continue;
    }
    const item: SchemaColumn = {
      name: column.name,
      type: column.type,
      comment: column.comment || undefined,
      nullable: column.nullable,
      primaryKey: column.primaryKey,
    };
    table.columns.push(item);
  }

  for (const fk of input.foreignKeys) {
    byTable.get(fk.table)?.foreignKeys.push({
      column: fk.column,
      refTable: fk.refTable,
      refColumn: fk.refColumn,
    });
  }

  const tables = Array.from(byTable.values()).filter((table) => table.columns.length > 0);
  const hasComments = tables.some(
    (table) => Boolean(table.comment) || table.columns.some((column) => Boolean(column.comment)),
  );

  return { dialect: input.dialect, tables, hasComments };
}

function toSamples(
  rows: Record<string, unknown>[],
  columns: string[],
): string[][] {
  return rows.map((row) =>
    columns.map((column) => {
      const value = row[column];
      if (value === null || value === undefined) {
        return "";
      }
      if (value instanceof Date) {
        return value.toISOString();
      }
      return String(value);
    }),
  );
}

async function introspectMySql(target: IntrospectTarget): Promise<IntrospectResult> {
  const connection = await createConnection({
    host: target.host,
    port: target.port,
    user: target.user,
    password: target.password,
    database: target.database,
    connectTimeout: CONNECT_TIMEOUT_MS,
    multipleStatements: false,
    charset: "utf8mb4",
  });

  try {
    // 只读会话：即使驱动意外拼错语句，也不会写入业务库。
    await connection.query("SET SESSION TRANSACTION READ ONLY").catch(() => undefined);
    await connection.query("SET SESSION MAX_EXECUTION_TIME = 15000").catch(() => undefined);

    const [versionRows] = await connection.query<RowDataPacket[]>("SELECT VERSION() AS version");
    const serverVersion = String(versionRows[0]?.version ?? "");

    const [tableRows] = await connection.query<RowDataPacket[]>(
      `SELECT TABLE_NAME AS tableName, TABLE_COMMENT AS tableComment
         FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        LIMIT ${Math.trunc(target.maxTables ?? MAX_TABLES)}`,
      [target.database],
    );

    const [columnRows] = await connection.query<RowDataPacket[]>(
      `SELECT TABLE_NAME AS tableName, COLUMN_NAME AS columnName, DATA_TYPE AS dataType,
              IS_NULLABLE AS isNullable, COLUMN_KEY AS columnKey, COLUMN_COMMENT AS columnComment
         FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = ?
        ORDER BY TABLE_NAME, ORDINAL_POSITION`,
      [target.database],
    );

    const [fkRows] = await connection.query<RowDataPacket[]>(
      `SELECT TABLE_NAME AS tableName, COLUMN_NAME AS columnName,
              REFERENCED_TABLE_NAME AS refTable, REFERENCED_COLUMN_NAME AS refColumn
         FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = ? AND REFERENCED_TABLE_NAME IS NOT NULL`,
      [target.database],
    );

    const tableNames = tableRows.map((row) => String(row.tableName));
    const samples = new Map<string, string[][]>();
    for (const tableName of tableNames.slice(0, MAX_SAMPLED_TABLES)) {
      const columns = columnRows.filter((row) => String(row.tableName) === tableName).map((row) => String(row.columnName));
      const escaped = tableName.split("`").join("``");
      try {
        const [rows] = await connection.query<RowDataPacket[]>(
          `SELECT * FROM \`${escaped}\` LIMIT ${SAMPLE_ROWS}`,
        );
        samples.set(tableName, toSamples(rows, columns));
      } catch {
        // 单表抽样失败（权限/大字段）不影响整体内省。
        samples.set(tableName, []);
      }
    }

    const schema = buildSchemaFromMetadata({
      dialect: "mysql",
      tables: tableRows.map((row) => ({
        name: String(row.tableName),
        comment: String(row.tableComment ?? ""),
        samples: samples.get(String(row.tableName)) ?? [],
      })),
      columns: columnRows.map((row) => ({
        table: String(row.tableName),
        name: String(row.columnName),
        type: String(row.dataType),
        comment: String(row.columnComment ?? ""),
        nullable: String(row.isNullable).toUpperCase() === "YES",
        primaryKey: String(row.columnKey).toUpperCase() === "PRI",
      })),
      foreignKeys: fkRows.map((row) => ({
        table: String(row.tableName),
        column: String(row.columnName),
        refTable: String(row.refTable),
        refColumn: String(row.refColumn),
      })),
    });

    return { schema, scannedTables: schema.tables.length, serverVersion };
  } finally {
    await connection.end().catch(() => undefined);
  }
}

async function introspectPostgres(target: IntrospectTarget): Promise<IntrospectResult> {
  const schemaName = target.schema ?? "public";
  const client = new Client({
    host: target.host,
    port: target.port,
    database: target.database,
    user: target.user,
    password: target.password,
    connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
    query_timeout: QUERY_TIMEOUT_MS,
  });

  await client.connect();
  try {
    await client.query("SET default_transaction_read_only = on").catch(() => undefined);
    await client.query("SET statement_timeout = 15000").catch(() => undefined);

    const version = await client.query<{ version: string }>("SELECT version() AS version");
    const serverVersion = String(version.rows[0]?.version ?? "");

    const tableResult = await client.query<{ table_name: string; comment: string | null }>(
      `SELECT c.relname AS table_name, obj_description(c.oid) AS comment
         FROM pg_class c
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relkind = 'r'
        ORDER BY c.relname
        LIMIT $2`,
      [schemaName, target.maxTables ?? MAX_TABLES],
    );

    const columnResult = await client.query<{
      table_name: string;
      column_name: string;
      data_type: string;
      is_nullable: string;
      comment: string | null;
    }>(
      `SELECT c.relname AS table_name, a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
              CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
              col_description(a.attrelid, a.attnum) AS comment
         FROM pg_attribute a
         JOIN pg_class c ON c.oid = a.attrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind = 'r'
        ORDER BY c.relname, a.attnum`,
      [schemaName],
    );

    const pkResult = await client.query<{ table_name: string; column_name: string }>(
      `SELECT tc.table_name, kcu.column_name
         FROM information_schema.table_constraints tc
         JOIN information_schema.key_column_usage kcu
           ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1`,
      [schemaName],
    );

    const fkResult = await client.query<{
      table_name: string;
      column_name: string;
      ref_table: string;
      ref_column: string;
    }>(
      `SELECT tc.table_name, kcu.column_name,
              ccu.table_name AS ref_table, ccu.column_name AS ref_column
         FROM information_schema.table_constraints tc
         JOIN information_schema.key_column_usage kcu
           ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
         JOIN information_schema.constraint_column_usage ccu
           ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = $1`,
      [schemaName],
    );

    const primaryKeys = new Set(pkResult.rows.map((row) => `${row.table_name}.${row.column_name}`));
    const tableNames = tableResult.rows.map((row) => row.table_name);
    const samples = new Map<string, string[][]>();
    for (const tableName of tableNames.slice(0, MAX_SAMPLED_TABLES)) {
      const columns = columnResult.rows
        .filter((row) => row.table_name === tableName)
        .map((row) => row.column_name);
      const quotedSchema = `"${schemaName.split('"').join('""')}"`;
      const quotedTable = `"${tableName.split('"').join('""')}"`;
      try {
        const rows = await client.query<Record<string, unknown>>(
          `SELECT * FROM ${quotedSchema}.${quotedTable} LIMIT ${SAMPLE_ROWS}`,
        );
        samples.set(tableName, toSamples(rows.rows, columns));
      } catch {
        samples.set(tableName, []);
      }
    }

    const schema = buildSchemaFromMetadata({
      dialect: "postgres",
      tables: tableResult.rows.map((row) => ({
        name: row.table_name,
        comment: row.comment ?? "",
        samples: samples.get(row.table_name) ?? [],
      })),
      columns: columnResult.rows.map((row) => ({
        table: row.table_name,
        name: row.column_name,
        type: row.data_type,
        comment: row.comment ?? "",
        nullable: row.is_nullable === "YES",
        primaryKey: primaryKeys.has(`${row.table_name}.${row.column_name}`),
      })),
      foreignKeys: fkResult.rows.map((row) => ({
        table: row.table_name,
        column: row.column_name,
        refTable: row.ref_table,
        refColumn: row.ref_column,
      })),
    });

    return { schema, scannedTables: schema.tables.length, serverVersion };
  } finally {
    await client.end().catch(() => undefined);
  }
}

/** 只连一次、跑一条查询，用于界面上的「测试连接」。 */
export async function testIntrospectConnection(
  target: IntrospectTarget,
): Promise<{ ok: true; serverVersion: string; tables: number } | { ok: false; error: string }> {
  try {
    const result =
      target.dialect === "mysql" ? await introspectMySql(target) : await introspectPostgres(target);
    return { ok: true, serverVersion: result.serverVersion, tables: result.scannedTables };
  } catch (error) {
    return { ok: false, error: sanitizeDbError(error) };
  }
}

export async function introspectSchema(
  target: IntrospectTarget,
): Promise<{ ok: true; result: IntrospectResult } | { ok: false; error: string }> {
  try {
    const result =
      target.dialect === "mysql" ? await introspectMySql(target) : await introspectPostgres(target);
    return { ok: true, result };
  } catch (error) {
    return { ok: false, error: sanitizeDbError(error) };
  }
}

/** 把驱动错误转成界面可读信息，并去掉可能出现的连接串/密码片段。 */
export function sanitizeDbError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/password[^,;)\s]*/gi, "password")
    .replace(/\b(?:pwd|passwd)=\S+/gi, "pwd=***")
    .slice(0, 300);
}

/** 一列对齐映射：把来源列 column 暴露成视图列 property（= 本体属性名）。 */
export type AlignmentViewColumn = { column?: string; property: string };

export type AlignmentView = {
  /** 视图名（同时用作平台资源名）。 */
  viewName: string;
  /** 来源表名。 */
  tableName: string;
  columns: AlignmentViewColumn[];
};

export type AlignmentViewResult = {
  created: string[];
  failed: Array<{ viewName: string; error: string }>;
};

/** 只允许常规标识符，避免把用户输入拼进 DDL 造成注入。 */
const SAFE_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;

function quoteIdentifier(raw: string, dialect: IntrospectDialect): string {
  return dialect === "mysql" ? `\`${raw}\`` : `"${raw}"`;
}

/**
 * 生成对齐视图的 DDL（纯函数，便于单测）。
 * 目的：平台 0.1.5/0.2.1 都不接受 data_properties[].mapped_field，
 * 只能让资源列名与本体属性名一致，从而走平台「同名列」隐式绑定。
 */
export function buildAlignmentViewSql(
  view: AlignmentView,
  dialect: IntrospectDialect,
  database?: string,
): { drop: string; create: string } | { error: string } {
  if (!SAFE_IDENTIFIER.test(view.viewName)) return { error: `视图名不合法：${view.viewName}` };
  if (!SAFE_IDENTIFIER.test(view.tableName)) return { error: `表名不合法：${view.tableName}` };
  if (view.columns.length === 0) return { error: `视图 ${view.viewName} 没有列映射` };
  const seen = new Set<string>();
  for (const item of view.columns) {
    const column = item.column?.trim();
    if (column && !SAFE_IDENTIFIER.test(column)) return { error: `列名不合法：${column}` };
    if (!SAFE_IDENTIFIER.test(item.property)) return { error: `属性名不合法：${item.property}` };
    if (seen.has(item.property)) return { error: `属性名重复：${item.property}` };
    seen.add(item.property);
  }

  const prefix = database && SAFE_IDENTIFIER.test(database) ? `${quoteIdentifier(database, dialect)}.` : "";
  const target = `${prefix}${quoteIdentifier(view.viewName, dialect)}`;
  const source = `${prefix}${quoteIdentifier(view.tableName, dialect)}`;
  const selectList = view.columns
    .map((item) => {
      const column = item.column?.trim();
      const expression = column ? quoteIdentifier(column, dialect) : "NULL";
      return `${expression} AS ${quoteIdentifier(item.property, dialect)}`;
    })
    .join(", ");
  return {
    drop: `DROP VIEW IF EXISTS ${target}`,
    create: `CREATE VIEW ${target} AS SELECT ${selectList} FROM ${source}`,
  };
}

/**
 * 在业务库上创建/替换「列名对齐本体属性名」的视图（需要数据库写权限）。
 * 注意：这里刻意不设置只读会话，因为要执行 DDL；除固定模板外不接受任何 SQL。
 */
export async function createAlignmentViews(
  target: IntrospectTarget,
  views: AlignmentView[],
): Promise<AlignmentViewResult> {
  const created: string[] = [];
  const failed: Array<{ viewName: string; error: string }> = [];
  const statements: Array<{ viewName: string; drop: string; create: string }> = [];

  for (const view of views) {
    const built = buildAlignmentViewSql(view, target.dialect, target.database);
    if ("error" in built) {
      failed.push({ viewName: view.viewName, error: built.error });
      continue;
    }
    statements.push({ viewName: view.viewName, ...built });
  }
  if (statements.length === 0) return { created, failed };

  if (target.dialect === "mysql") {
    const connection = await createConnection({
      host: target.host,
      port: target.port,
      database: target.database,
      user: target.user,
      password: target.password,
      connectTimeout: CONNECT_TIMEOUT_MS,
      multipleStatements: false,
    });
    try {
      for (const statement of statements) {
        try {
          await connection.query(statement.drop);
          await connection.query(statement.create);
          created.push(statement.viewName);
        } catch (error) {
          failed.push({ viewName: statement.viewName, error: sanitizeDbError(error) });
        }
      }
    } finally {
      await connection.end().catch(() => undefined);
    }
    return { created, failed };
  }

  const client = new Client({
    host: target.host,
    port: target.port,
    database: target.database,
    user: target.user,
    password: target.password,
    connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
    query_timeout: QUERY_TIMEOUT_MS,
  });
  await client.connect();
  try {
    for (const statement of statements) {
      try {
        await client.query(statement.drop);
        await client.query(statement.create);
        created.push(statement.viewName);
      } catch (error) {
        failed.push({ viewName: statement.viewName, error: sanitizeDbError(error) });
      }
    }
  } finally {
    await client.end().catch(() => undefined);
  }
  return { created, failed };
}
