import { describe, expect, it } from "vitest";

import { buildBindingKit } from "../datasource-kit.js";
import type { OntologyIr } from "../types/ir.js";

function buildIr(): OntologyIr {
  return {
    networkId: "kn_equipment",
    networkName: "设备运维知识网络",
    objectTypes: [
      {
        id: "equipment",
        name: "设备",
        sourceTable: "10-设备台账",
        primaryKey: "equipment_id",
        properties: [
          { name: "equipment_id", displayName: "设备编号", type: "string", mappedField: "设备编号" },
          { name: "equipment_name", displayName: "设备名称", type: "string", mappedField: "设备名称" },
          { name: "note", displayName: "备注", type: "string" },
        ],
      },
      {
        id: "fault",
        name: "故障",
        sourceTable: "12-故障记录",
        primaryKey: "fault_id",
        properties: [
          { name: "fault_id", displayName: "故障编号", type: "string", mappedField: "故障编号" },
          { name: "equipment_id", displayName: "设备编号", type: "string", mappedField: "设备编号" },
        ],
      },
      {
        id: "vendor",
        name: "供应商",
        properties: [{ name: "vendor_id", displayName: "供应商编号", type: "string" }],
      },
    ],
    relations: [
      {
        id: "equipment_has_fault",
        name: "发生故障",
        source: "equipment",
        target: "fault",
        mapping: [{ sourceProperty: "equipment_id", targetProperty: "equipment_id" }],
      },
      {
        id: "equipment_supplied_by",
        name: "供应商提供设备",
        source: "vendor",
        target: "equipment",
      },
    ],
  };
}

function readFile(files: { name: string; content: string }[], name: string): string {
  const file = files.find((item) => item.name === name);
  if (!file) {
    throw new Error(`missing file: ${name}`);
  }
  return file.content;
}

describe("buildBindingKit", () => {
  it("生成 MySQL 视图并用属性名做列别名", () => {
    const kit = buildBindingKit(buildIr());
    const sql = readFile(kit.files, "views.sql");

    expect(sql).toContain("create or replace view `v_equipment` as");
    expect(sql).toContain("`设备编号` as `equipment_id`");
    expect(sql).toContain("`设备名称` as `equipment_name`");
    expect(sql).toContain("from `10-设备台账`;");
    // 没有映射原列名的属性不进视图
    expect(sql).not.toContain("`note`");
  });

  it("按 PostgreSQL 方言改用双引号", () => {
    const kit = buildBindingKit(buildIr(), { dialect: "postgres" });
    const sql = readFile(kit.files, "views.sql");

    expect(sql).toContain('create or replace view "v_equipment" as');
    expect(sql).toContain('"设备编号" as "equipment_id"');
    expect(sql).toContain('from "10-设备台账";');
  });

  it("跳过没有来源表的对象并给出提示", () => {
    const kit = buildBindingKit(buildIr());
    const binding = JSON.parse(readFile(kit.files, "binding.json")) as {
      skipped: Array<{ objectId: string }>;
    };

    expect(binding.skipped.map((item) => item.objectId)).toEqual(["vendor"]);
    expect(kit.warnings.some((warning) => warning.includes("供应商"))).toBe(true);
    expect(kit.stats.views).toBe(2);
    expect(kit.stats.objects).toBe(3);
  });

  it("binding.json 记录主键、字段映射与关系键映射", () => {
    const kit = buildBindingKit(buildIr());
    const binding = JSON.parse(readFile(kit.files, "binding.json")) as {
      objects: Array<{
        objectId: string;
        view: string;
        primaryKey: string | null;
        properties: Array<{ property: string; column: string }>;
      }>;
      relations: Array<{ relationId: string; mapping: Array<{ sourceProperty: string }> }>;
    };

    const equipment = binding.objects.find((item) => item.objectId === "equipment");
    expect(equipment?.view).toBe("v_equipment");
    expect(equipment?.primaryKey).toBe("equipment_id");
    expect(equipment?.properties).toEqual([
      { property: "equipment_id", column: "设备编号" },
      { property: "equipment_name", column: "设备名称" },
    ]);
    expect(binding.relations).toHaveLength(1);
    expect(binding.relations[0]?.relationId).toBe("equipment_has_fault");
    expect(binding.relations[0]?.mapping[0]?.sourceProperty).toBe("equipment_id");
  });

  it("README 包含四步指引与对象清单", () => {
    const kit = buildBindingKit(buildIr());
    const readme = readFile(kit.files, "README.md");

    expect(readme).toContain("## 平台侧四步");
    expect(readme).toContain("## 对象清单");
    expect(readme).toContain("| 设备 | 10-设备台账 | v_equipment | equipment_id | 2 |");
    expect(readme).toContain("0.1.5 平台不支持导入 `mapped_field`");
    expect(readme).toContain("发生故障");
  });

  it("没有任何绑定表时给出明确警告", () => {
    const kit = buildBindingKit({
      ...buildIr(),
      objectTypes: [{ id: "vendor", name: "供应商", properties: [] }],
      relations: [],
    });

    expect(kit.stats.views).toBe(0);
    expect(kit.warnings.some((warning) => warning.includes("没有任何对象绑定到数据表"))).toBe(true);
  });
});
