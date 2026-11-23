/**
 * Copyright (c) 2026 OpenBKN
 * SPDX-License-Identifier: LicenseRef-OpenBKN
 * Licensed under the OpenBKN License, a modified Apache 2.0 with Additional
 * Conditions. See LICENSE for the full text.
 */

import type { IrObjectType, OntologyIr } from "./types/ir.js";


export type AttachDialect = "mysql" | "postgres";

export type AttachTarget = {
  dialect: AttachDialect;
  host: string;
  port?: number;
  database: string;
  user: string;
  password: string;
  schema?: string;
};

export type AttachPlanResource = {
  /** 平台资源（表/视图）名，默认用来源表名。 */
  name: string;
  /** 来源表名（catalog discover 时用于匹配）。 */
  sourceIdentifier: string;
  category: "table";
  description: string;
  schemaDefinition: Array<{
    name: string;
    display_name: string;
    type: string;
    original_name: string;
    description: string;
  }>;
  indexConfig: {
    primaryKeyFields: string[];
    incrementalFields: string[];
  };
};

export type AttachPlanBinding = {
  objectId: string;
  objectName: string;
  resourceName: string;
  primaryKey: string;
  properties: Array<{ property: string; column?: string }>;
};

export type AttachPlan = {
  dialect: AttachDialect;
  catalog: {
    name: string;
    connectorType: string;
    connectorConfig: Record<string, unknown>;
    description: string;
    tags: string[];
  };
  resources: AttachPlanResource[];
  bindings: AttachPlanBinding[];
  warnings: string[];
};

const DEFAULT_DATABASE_PORT: Record<AttachDialect, number> = { mysql: 3306, postgres: 5432 };

/** IR 属性类型 → 平台资源列类型（宽类型，避免抽样数据溢出）。 */
const PROPERTY_TYPE_TO_SQL: Record<string, string> = {
  string: "text",
  integer: "bigint",
  number: "double",
  boolean: "tinyint",
  datetime: "datetime",
};

function connectorType(dialect: AttachDialect): string {
  return dialect === "postgres" ? "postgresql" : "mysql";
}

/**
 * 组装「数据源 + 资源 + 对象绑定」接入计划。
 * 该函数只做确定性映射，真正的平台调用由前端带着用户令牌执行（见 services 层的 attachCopilotDataSource）。
 */
/**
 * 视图列必须包含主键且非空，否则平台的样本/对象查询会只返回总数而丢弃数据行。
 * 主键属性没有来源列时，借用第一个有列映射的属性列（例如把设备编号同时映射为 id）。
 */
function fillPrimaryKeyColumn(
  properties: Array<{ property: string; column?: string }>,
  primaryKey?: string,
): Array<{ property: string; column?: string }> {
  // ir-to-kn 在没有显式主键时会把 "id" 当作主键，因此这两个名字都要兜底。
  const targets = properties.filter((item) => item.property === (primaryKey ?? "id") || item.property === "id");
  const donor = properties.find((item) => Boolean(item.column));
  if (!donor?.column) return properties;
  for (const target of targets) {
    if (!target.column) target.column = donor.column;
  }
  return properties;
}
export function buildAttachPlan(
  ir: OntologyIr,
  target: AttachTarget,
  options: { catalogName?: string; networkName?: string } = {},
): AttachPlan {
  const warnings: string[] = [];
  const dialect = target.dialect;
  const port = target.port ?? DEFAULT_DATABASE_PORT[dialect];
  const networkName = options.networkName?.trim() || ir.networkName || ir.networkId;
  const catalogName = options.catalogName?.trim() || `${networkName} 数据源`;

  const connectorConfig: Record<string, unknown> =
    dialect === "postgres"
      ? {
          host: target.host,
          port,
          database: target.database,
          username: target.user,
          password: target.password,
          schema: target.schema?.trim() || "public",
          ssl_mode: "prefer",
        }
      : {
          host: target.host,
          port,
          database: target.database,
          username: target.user,
          password: target.password,
          charset: "utf8mb4",
        };

  const resources: AttachPlanResource[] = [];
  const bindings: AttachPlanBinding[] = [];

  for (const object of ir.objectTypes) {
    const table = object.sourceTable?.trim();
    if (!table) {
      warnings.push(`对象「${object.name}」没有来源表，跳过资源创建`);
      continue;
    }
    const schemaRows = buildSchemaDefinition(object);
    if (schemaRows.length === 0) {
      warnings.push(`对象「${object.name}」没有可映射的属性，跳过资源创建`);
      continue;
    }
    resources.push({
      name: table,
      sourceIdentifier: table,
      category: "table",
      description: object.description?.trim() || `${object.name}（来自 ${table}）`,
      schemaDefinition: schemaRows,
      indexConfig: {
        primaryKeyFields: [object.primaryKey ?? "id"].filter(Boolean),
        incrementalFields: [],
      },
    });
    bindings.push({
      objectId: object.id,
      objectName: object.name,
      resourceName: table,
      primaryKey: object.primaryKey ?? "id",
      properties: fillPrimaryKeyColumn(
        object.properties.map((property) => ({
          property: property.name,
          ...(property.mappedField?.trim() ? { column: property.mappedField.trim() } : {}),
        })),
        object.primaryKey,
      ),
    });
  }

  if (resources.length === 0) {
    warnings.push("没有可接入的对象：请先上传数据表/数据库结构并生成草案");
  }

  return {
    dialect,
    catalog: {
      name: catalogName,
      connectorType: connectorType(dialect),
      connectorConfig,
      description: `${networkName} 自动接入数据源`,
      tags: ["copilot", "auto-attach"],
    },
    resources,
    bindings,
    warnings,
  };
}

function buildSchemaDefinition(object: IrObjectType): AttachPlanResource["schemaDefinition"] {
  const rows: AttachPlanResource["schemaDefinition"] = [];
  const seen = new Set<string>();
  for (const property of object.properties) {
    const column = property.mappedField?.trim() || property.name;
    const name = column || property.name;
    if (!name || seen.has(name)) continue;
    seen.add(name);
    rows.push({
      name,
      display_name: property.displayName?.trim() || property.name,
      type: PROPERTY_TYPE_TO_SQL[property.type] ?? "text",
      original_name: name,
      description: property.description?.trim() || property.displayName?.trim() || property.name,
    });
  }
  return rows;
}
