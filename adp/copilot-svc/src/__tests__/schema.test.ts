import { describe, expect, it } from "vitest";

import {
  buildSchemaBrief,
  deriveActionsAndRisks,
  mapSqlType,
  mergeSchemas,
  parseSqlSchema,
  schemaStats,
  schemaToIr,
  schemaToTables,
} from "../schema.js";

const MYSQL_DUMP = `
-- MySQL dump 10.13  Distrib 8.0.36
/*!40101 SET NAMES utf8mb4 */;

CREATE TABLE \`t_equip\` (
  \`equip_code\` varchar(32) NOT NULL COMMENT '设备编号',
  \`equip_name\` varchar(64) NOT NULL COMMENT '设备名称',
  \`equip_type\` varchar(32) DEFAULT NULL COMMENT '设备类型',
  \`run_status\` varchar(16) DEFAULT NULL COMMENT '运行状态',
  \`install_date\` date DEFAULT NULL COMMENT '投运日期',
  \`rated_power\` decimal(10,2) DEFAULT NULL COMMENT '额定功率',
  PRIMARY KEY (\`equip_code\`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备台账';

CREATE TABLE \`t_fault\` (
  \`fault_no\` varchar(32) NOT NULL,
  \`equip_code\` varchar(32) NOT NULL COMMENT '设备编号',
  \`fault_desc\` varchar(255) DEFAULT NULL COMMENT '故障现象',
  \`occur_time\` datetime DEFAULT NULL,
  PRIMARY KEY (\`fault_no\`),
  CONSTRAINT \`fk_fault_equip\` FOREIGN KEY (\`equip_code\`) REFERENCES \`t_equip\` (\`equip_code\`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='故障记录';
`;

const PG_SCHEMA = `
CREATE TABLE "equipment" (
  id serial PRIMARY KEY,
  code varchar(32) NOT NULL,
  name varchar(64) NOT NULL,
  workspace varchar(64)
);

CREATE TABLE "fault" (
  id serial PRIMARY KEY,
  equipment_id integer NOT NULL,
  description text
);

ALTER TABLE "fault" ADD CONSTRAINT "fk_fault_equipment" FOREIGN KEY ("equipment_id") REFERENCES "equipment" ("id");

COMMENT ON TABLE "equipment" IS '设备台账';
COMMENT ON COLUMN "equipment"."code" IS '设备编号';
COMMENT ON TABLE "fault" IS '故障记录';
COMMENT ON COLUMN "fault"."equipment_id" IS '所属设备';
`;

const NO_COMMENT_SQL = `
CREATE TABLE tb_order (
  order_id bigint NOT NULL,
  customer_id bigint,
  amount numeric(12,2),
  created_at timestamp,
  PRIMARY KEY (order_id)
);
CREATE TABLE tb_customer (
  customer_id bigint NOT NULL,
  customer_name varchar(64),
  PRIMARY KEY (customer_id)
);
`;

const ORACLE_SQL = `
CREATE TABLE T_EQUIP (
  EQUIP_CODE VARCHAR2(32) NOT NULL,
  EQUIP_NAME VARCHAR2(64),
  STATUS NUMBER(1),
  PRIMARY KEY (EQUIP_CODE)
);
COMMENT ON TABLE T_EQUIP IS '设备';
COMMENT ON COLUMN T_EQUIP.EQUIP_NAME IS '设备名称';
`;

describe("parseSqlSchema", () => {
  it("parses a MySQL dump with comments, primary keys and foreign keys", () => {
    const schema = parseSqlSchema(MYSQL_DUMP);
    expect(schema.dialect).toBe("mysql");
    expect(schema.tables).toHaveLength(2);
    expect(schema.hasComments).toBe(true);

    const equip = schema.tables[0];
    expect(equip.name).toBe("t_equip");
    expect(equip.comment).toBe("设备台账");
    expect(equip.columns[0]).toMatchObject({
      name: "equip_code",
      comment: "设备编号",
      primaryKey: true,
    });
    expect(equip.columns.find((column) => column.name === "rated_power")?.type).toMatch(/decimal/i);

    const fault = schema.tables[1];
    expect(fault.comment).toBe("故障记录");
    expect(fault.foreignKeys).toEqual([
      { column: "equip_code", refTable: "t_equip", refColumn: "equip_code" },
    ]);
  });

  it("parses PostgreSQL quoted identifiers, COMMENT ON and ALTER TABLE foreign keys", () => {
    const schema = parseSqlSchema(PG_SCHEMA);
    expect(schema.dialect).toBe("postgres");
    expect(schema.tables.map((table) => table.name)).toEqual(["equipment", "fault"]);
    expect(schema.tables[0].comment).toBe("设备台账");
    expect(schema.tables[0].columns.find((column) => column.name === "code")?.comment).toBe(
      "设备编号",
    );
    expect(schema.tables[1].foreignKeys).toEqual([
      { column: "equipment_id", refTable: "equipment", refColumn: "id" },
    ]);
  });

  it("detects a schema without comments", () => {
    const schema = parseSqlSchema(NO_COMMENT_SQL);
    expect(schema.hasComments).toBe(false);
    expect(schemaStats(schema)).toMatchObject({
      tables: 2,
      columns: 6,
      foreignKeys: 0,
      hasComments: false,
    });
  });

  it("parses Oracle style types and comments", () => {
    const schema = parseSqlSchema(ORACLE_SQL);
    expect(["oracle", "unknown"]).toContain(schema.dialect);
    expect(schema.tables[0].columns.map((column) => mapSqlType(column.type))).toEqual([
      "string",
      "string",
      "number",
    ]);
    expect(schema.tables[0].comment).toBe("设备");
  });

  it("merges multiple schemas without duplicating tables", () => {
    const first = parseSqlSchema(MYSQL_DUMP);
    const second = parseSqlSchema(MYSQL_DUMP);
    const merged = mergeSchemas([first, second]);
    expect(merged.tables).toHaveLength(2);
    expect(schemaToTables(merged)[0].headers).toContain("equip_code");
  });
});

describe("buildSchemaBrief / schemaToIr", () => {
  it("renders comments, keys and foreign keys into the brief", () => {
    const schema = parseSqlSchema(MYSQL_DUMP);
    const brief = buildSchemaBrief(schema);
    expect(brief).toContain("t_equip（设备台账）");
    expect(brief).toContain("equip_code（设备编号） varchar(32) [主键,非空]");
    expect(brief).toContain("外键：equip_code → t_equip.equip_code");
  });

  it("builds a rule-based IR with table/column bindings and questions", () => {
    const schema = parseSqlSchema(NO_COMMENT_SQL);
    const result = schemaToIr(schema, "订单网络");
    expect(result.objects).toHaveLength(2);
    expect(result.relations).toHaveLength(0);
    expect(result.questions.length).toBeGreaterThan(0);

    const order = result.objects[0] as {
      sourceTable: string;
      primaryKey?: string;
      properties: Array<{ name: string; mappedField?: string }>;
    };
    expect(order.sourceTable).toBe("tb_order");
    expect(order.primaryKey).toBe("order_id");
    expect(order.properties[0]).toMatchObject({ name: "order_id", mappedField: "order_id" });
  });

  it("creates relations from foreign keys", () => {
    const schema = parseSqlSchema(MYSQL_DUMP);
    const result = schemaToIr(schema, "设备网络");
    expect(result.relations).toHaveLength(1);
    expect(result.relations[0]).toMatchObject({ source: "fault", target: "equip" });
    expect(result.relations[0].mapping).toEqual([
      { sourceProperty: "equip_code", targetProperty: "equip_code" },
    ]);
  });

  it("maps sql types to ontology property types", () => {
    expect(mapSqlType("int(11)")).toBe("integer");
    expect(mapSqlType("bigint")).toBe("integer");
    expect(mapSqlType("tinyint(1)")).toBe("boolean");
    expect(mapSqlType("decimal(10,2)")).toBe("number");
    expect(mapSqlType("datetime")).toBe("datetime");
    expect(mapSqlType("varchar(64)")).toBe("string");
  });
  it("derives actions from status columns and risks from level columns", () => {
    const schema = {
      dialect: "mysql" as const,
      hasComments: false,
      tables: [
        {
          name: "t_work_order",
          columns: [
            { name: "order_code", type: "varchar(32)", primaryKey: true },
            { name: "status", type: "varchar(16)" },
            { name: "created_at", type: "datetime" },
          ],
          foreignKeys: [],
        },
        {
          name: "t_alert",
          columns: [
            { name: "alert_code", type: "varchar(32)", primaryKey: true },
            { name: "level", type: "varchar(16)" },
          ],
          foreignKeys: [],
        },
        {
          name: "t_equip",
          columns: [{ name: "equip_code", type: "varchar(32)", primaryKey: true }],
          foreignKeys: [],
        },
      ],
    };

    const governance = deriveActionsAndRisks(schema, [
      { id: "work_order", name: "工单", sourceTable: "t_work_order" },
      { id: "alert", name: "告警", sourceTable: "t_alert" },
      { id: "equip", name: "设备", sourceTable: "t_equip" },
    ]);

    expect(governance.actionTypes.map((item) => item.name)).toEqual(
      expect.arrayContaining(["创建工单", "处理工单", "关闭工单"]),
    );
    expect(governance.actionTypes.every((item) => item.boundObject === "work_order")).toBe(true);
    expect(governance.actionTypes.length).toBeLessThanOrEqual(6);
    expect(governance.riskTypes).toHaveLength(1);
    expect(governance.riskTypes[0].name).toBe("告警高等级未及时处理");
    expect(governance.riskTypes[0].policies.length).toBeGreaterThanOrEqual(2);
  });

  it("returns no governance when the schema has no status or level columns", () => {
    const schema = {
      dialect: "mysql" as const,
      hasComments: false,
      tables: [
        {
          name: "t_config",
          columns: [{ name: "config_key", type: "varchar(32)", primaryKey: true }],
          foreignKeys: [],
        },
      ],
    };
    const governance = deriveActionsAndRisks(schema, [
      { id: "config", name: "配置", sourceTable: "t_config" },
    ]);
    expect(governance.actionTypes).toHaveLength(0);
    expect(governance.riskTypes).toHaveLength(0);
  });
});