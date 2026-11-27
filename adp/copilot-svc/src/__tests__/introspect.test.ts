import { describe, expect, it } from "vitest";

import {
  buildAlignmentViewSql,
  buildSchemaFromMetadata,
  normalizeIntrospectTarget,
  sanitizeDbError,
} from "../introspect.js";

describe("normalizeIntrospectTarget", () => {
  it("补全默认端口并保留可选 schema", () => {
    const result = normalizeIntrospectTarget({
      dialect: "postgres",
      host: " 10.0.0.5 ",
      database: "erp",
      user: "reader",
      password: "secret",
      schema: "biz",
    });

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.target).toMatchObject({
      dialect: "postgres",
      host: "10.0.0.5",
      port: 5432,
      database: "erp",
      user: "reader",
      schema: "biz",
      maxTables: 200,
    });
  });

  it("MySQL 默认端口 3306，maxTables 超限时收敛到 200", () => {
    const result = normalizeIntrospectTarget({
      dialect: "mysql",
      host: "127.0.0.1",
      database: "app",
      user: "root",
      password: "x",
      port: "3307",
      maxTables: 9999,
    });

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.target.port).toBe(3307);
    expect(result.target.maxTables).toBe(200);
  });

  it("缺少必填项时逐条给出提示", () => {
    const result = normalizeIntrospectTarget({ dialect: "oracle" });

    expect(result.ok).toBe(false);
    if (result.ok) {
      return;
    }
    expect(result.errors).toContain("请选择数据库类型：MySQL 或 PostgreSQL");
    expect(result.errors).toContain("请填写数据库地址");
    expect(result.errors).toContain("请填写数据库名");
    expect(result.errors).toContain("请填写数据库用户名");
    expect(result.errors).toContain("请填写数据库密码");
  });
});

describe("buildSchemaFromMetadata", () => {
  it("按元数据组装表、字段、主键、外键与注释", () => {
    const schema = buildSchemaFromMetadata({
      dialect: "mysql",
      tables: [
        { name: "t_equip", comment: "设备台账", samples: [["EQ-001", "空压机"]] },
        { name: "empty_table" },
      ],
      columns: [
        { table: "t_equip", name: "equip_code", type: "varchar", comment: "设备编号", primaryKey: true },
        { table: "t_equip", name: "equip_name", type: "varchar", comment: "设备名称", nullable: true },
      ],
      foreignKeys: [{ table: "t_equip", column: "workshop_id", refTable: "t_workshop", refColumn: "id" }],
    });

    expect(schema.dialect).toBe("mysql");
    expect(schema.hasComments).toBe(true);
    expect(schema.tables).toHaveLength(1);
    const table = schema.tables[0];
    expect(table?.comment).toBe("设备台账");
    expect(table?.columns.map((column) => column.name)).toEqual(["equip_code", "equip_name"]);
    expect(table?.columns[0]?.primaryKey).toBe(true);
    expect(table?.columns[1]?.nullable).toBe(true);
    expect(table?.foreignKeys).toEqual([{ column: "workshop_id", refTable: "t_workshop", refColumn: "id" }]);
    expect(table?.samples?.[0]).toEqual(["EQ-001", "空压机"]);
  });

  it("没有任何注释时 hasComments 为 false", () => {
    const schema = buildSchemaFromMetadata({
      dialect: "postgres",
      tables: [{ name: "orders" }],
      columns: [{ table: "orders", name: "id", type: "uuid" }],
      foreignKeys: [],
    });

    expect(schema.hasComments).toBe(false);
    expect(schema.tables[0]?.columns).toHaveLength(1);
  });
});

describe("sanitizeDbError", () => {
  it("去掉错误信息里可能出现的密码片段", () => {
    const message = sanitizeDbError(
      new Error("Access denied for user 'root' (using password: YES) password=secret123 pwd=abc"),
    );

    expect(message).not.toContain("secret123");
    expect(message).not.toContain("pwd=abc");
    expect(message.length).toBeLessThanOrEqual(300);
  });
});

describe("buildAlignmentViewSql", () => {
  it("生成 MySQL 的 drop + create 语句并引用库名", () => {
    const result = buildAlignmentViewSql(
      {
        viewName: "v_device",
        tableName: "t_equip",
        columns: [
          { column: "equip_code", property: "device_code" },
          { column: "equip_name", property: "device_name" },
        ],
      },
      "mysql",
      "erp_demo",
    );

    expect(result).not.toHaveProperty("error");
    if ("error" in result) {
      return;
    }
    expect(result.drop).toBe("DROP VIEW IF EXISTS `erp_demo`.`v_device`");
    expect(result.create).toBe(
      "CREATE VIEW `erp_demo`.`v_device` AS SELECT `equip_code` AS `device_code`, `equip_name` AS `device_name` FROM `erp_demo`.`t_equip`",
    );
  });

  it("按方言使用双引号并拒绝非法标识符与重复属性", () => {
    const pg = buildAlignmentViewSql(
      { viewName: "v_fault", tableName: "t_fault", columns: [{ column: "fault_code", property: "code" }] },
      "postgres",
    );
    expect("error" in pg).toBe(false);
    if (!("error" in pg)) {
      expect(pg.create).toContain('"t_fault"');
      expect(pg.create).toContain('"fault_code" AS "code"');
    }

    const invalid = buildAlignmentViewSql(
      { viewName: "v_bad;drop", tableName: "t", columns: [{ column: "a", property: "b" }] },
      "mysql",
    );
    expect("error" in invalid).toBe(true);

    const duplicated = buildAlignmentViewSql(
      {
        viewName: "v_dup",
        tableName: "t",
        columns: [
          { column: "a", property: "same" },
          { column: "b", property: "same" },
        ],
      },
      "mysql",
    );
    expect("error" in duplicated).toBe(true);
  });
});