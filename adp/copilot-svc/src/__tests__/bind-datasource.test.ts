import { describe, expect, it } from "vitest";

import { inferIrBindings, inferColumnType, inferPrimaryKey } from "../bind-datasource.js";
import type { ExtractedTable } from "../extract.js";
import type { OntologyIr } from "../types/ir.js";

const ir: OntologyIr = {
  networkId: "kn_device",
  networkName: "设备运维知识网络",
  objectTypes: [
    {
      id: "device",
      name: "设备",
      properties: [
        { name: "id", displayName: "ID", type: "string" },
        { name: "device_code", displayName: "设备编号", type: "string" },
        { name: "device_name", displayName: "设备名称", type: "string" },
        { name: "rated_power", displayName: "设备功率", type: "string" },
        { name: "install_date", displayName: "投运日期", type: "string" },
        { name: "unknown_field", displayName: "不存在字段", type: "string" },
      ],
    },
    {
      id: "component",
      name: "部件",
      properties: [
        { name: "id", displayName: "ID", type: "string" },
        { name: "component_code", displayName: "部件编号", type: "string" },
        { name: "component_name", displayName: "部件名称", type: "string" },
        { name: "device_code", displayName: "设备编号", type: "string" },
      ],
    },
  ],
  relations: [
    { id: "device_has_component", name: "设备包含部件", source: "device", target: "component" },
  ],
  conceptGroups: [],
};

const tables: ExtractedTable[] = [
  {
    name: "设备台账",
    headers: ["设备编号", "设备名称", "设备功率", "投运日期"],
    rows: 5,
    samples: [
      ["EQ-001", "空压机-1号", "75.5", "2021-03-12"],
      ["EQ-002", "空压机-2号", "75.5", "2021-03-12"],
      ["EQ-003", "冷却塔-1号", "30", "2020-07-01"],
    ],
  },
  {
    name: "部件清单",
    headers: ["部件编号", "部件名称", "设备编号", "型号"],
    rows: 8,
    samples: [
      ["PT-001", "电机", "EQ-001", "Y2-100"],
      ["PT-002", "轴承", "EQ-001", "6205"],
      ["PT-003", "风机", "EQ-002", "F-200"],
    ],
  },
];

describe("inferColumnType", () => {
  it("按样例值推断整数/浮点/日期/字符串", () => {
    expect(inferColumnType(["1", "2", "3"])).toBe("integer");
    expect(inferColumnType(["1.5", "2", "3.25"])).toBe("number");
    expect(inferColumnType(["2024-01-02", "2024-03-05"])).toBe("datetime");
    expect(inferColumnType(["EQ-001", "EQ-002"])).toBe("string");
    expect(inferColumnType([])).toBeUndefined();
  });
});

describe("inferPrimaryKey", () => {
  it("优先选编号类且唯一的列", () => {
    expect(inferPrimaryKey(tables[0] as ExtractedTable)).toBe("设备编号");
    expect(inferPrimaryKey(tables[1] as ExtractedTable)).toBe("部件编号");
  });
});

describe("inferIrBindings", () => {
  it("按表名匹配对象类型并映射字段", () => {
    const result = inferIrBindings(ir, tables);
    const device = result.ir.objectTypes[0];
    const component = result.ir.objectTypes[1];

    expect(device?.sourceTable).toBe("设备台账");
    expect(component?.sourceTable).toBe("部件清单");
    expect(device?.properties.find((item) => item.name === "device_code")?.mappedField).toBe("设备编号");
    expect(device?.properties.find((item) => item.name === "device_name")?.mappedField).toBe("设备名称");
    expect(device?.properties.find((item) => item.name === "unknown_field")?.mappedField).toBeUndefined();
  });

  it("用样例值推断属性类型并补主键", () => {
    const result = inferIrBindings(ir, tables);
    const device = result.ir.objectTypes[0];
    expect(device?.properties.find((item) => item.name === "rated_power")?.type).toBe("number");
    expect(device?.properties.find((item) => item.name === "install_date")?.type).toBe("datetime");
    expect(device?.primaryKey).toBe("device_code");
    expect(result.stats.inferredPrimaryKeys).toBe(2);
    expect(result.details.objects[0]?.primaryKey).toEqual({ property: "device_code", column: "设备编号" });
  });

  it("按外键推断关系的键映射", () => {
    const result = inferIrBindings(ir, tables);
    const relation = result.ir.relations[0];
    expect(relation?.mapping).toEqual([{ sourceProperty: "device_code", targetProperty: "device_code" }]);
    expect(result.stats.inferredRelations).toBe(1);
    expect(result.details.relations[0]?.mapping).toHaveLength(1);
  });

  it("统计匹配到的对象与字段数量", () => {
    const result = inferIrBindings(ir, tables);
    expect(result.stats).toMatchObject({
      tables: 2,
      objects: 2,
      boundObjects: 2,
      properties: 10,
      mappedProperties: 7,
      inferredPrimaryKeys: 2,
      inferredRelations: 1,
    });
  });

  it("没有数据表时原样返回", () => {
    const result = inferIrBindings(ir, []);
    expect(result.ir).toBe(ir);
    expect(result.stats.boundObjects).toBe(0);
    expect(result.stats.mappedProperties).toBe(0);
    expect(result.warnings).toEqual([]);
  });

  it("表头为空时忽略该表", () => {
    const result = inferIrBindings(ir, [{ name: "设备台账", headers: [], rows: 0, samples: [] }]);
    expect(result.ir.objectTypes[0]?.sourceTable).toBeUndefined();
    expect(result.stats.tables).toBe(1);
    expect(result.stats.boundObjects).toBe(0);
    expect(result.warnings.length).toBeGreaterThan(0);
  });

  it("不在任何表中的对象保持未绑定", () => {
    const onlyDevice: ExtractedTable[] = [tables[0] as ExtractedTable];
    const result = inferIrBindings(ir, onlyDevice);
    expect(result.ir.objectTypes[0]?.sourceTable).toBe("设备台账");
    expect(result.ir.objectTypes[1]?.sourceTable).toBeUndefined();
    expect(result.stats.boundObjects).toBe(1);
  });
});
