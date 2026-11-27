import { describe, expect, it } from "vitest";

import { buildIndexPlan } from "../index-plan.js";
import type { OntologyIr } from "../types/ir.js";

const ir: OntologyIr = {
  networkId: "kn_demo",
  networkName: "演示网络",
  objectTypes: [
    {
      id: "equipment",
      name: "设备",
      sourceTable: "t_equip",
      primaryKey: "id",
      properties: [
        { name: "id", displayName: "设备编号", type: "string", mappedField: "equip_code" },
        { name: "name", displayName: "设备名称", type: "string", mappedField: "equip_name" },
        { name: "remark", displayName: "描述", type: "string", mappedField: "remark" },
        { name: "status", displayName: "运行状态", type: "string", mappedField: "status" },
        { name: "created_at", displayName: "创建时间", type: "datetime", mappedField: "created_at" },
      ],
    },
    {
      id: "fault",
      name: "故障",
      sourceTable: "t_fault",
      primaryKey: "id",
      properties: [
        { name: "id", displayName: "故障编号", type: "string", mappedField: "fault_code" },
        { name: "phenomenon", displayName: "故障现象", type: "string", mappedField: "phenomenon" },
      ],
    },
    {
      id: "ghost",
      name: "无来源表对象",
      properties: [{ name: "id", type: "string" }],
    },
  ],
  relations: [],
};

describe("buildIndexPlan", () => {
  it("按主键与字段名建议全文/向量索引字段", () => {
    const plan = buildIndexPlan(ir);
    expect(plan.summary.objects).toBe(2);
    const equipment = plan.suggestions.find((item) => item.objectId === "equipment");
    expect(equipment?.resourceName).toBe("t_equip");
    expect(equipment?.primaryKeyField).toBe("equip_code");
    expect(equipment?.fulltextFields).toEqual(["equip_name", "remark"]);
    expect(equipment?.vectorFields).toEqual(["remark"]);
    expect(equipment?.incrementalField).toBe("created_at");
    const fault = plan.suggestions.find((item) => item.objectId === "fault");
    expect(fault?.fulltextFields).toEqual(["phenomenon"]);
    expect(fault?.vectorFields).toEqual([]);
    expect(fault?.incrementalField).toBeUndefined();
  });

  it("没有来源表的对象只给警告不建索引", () => {
    const plan = buildIndexPlan(ir);
    expect(plan.suggestions.map((item) => item.objectId)).not.toContain("ghost");
    expect(plan.warnings.some((item) => item.includes("无来源表对象"))).toBe(true);
  });

  it("缺省主键时退回第一个属性并去重字段", () => {
    const plan = buildIndexPlan({
      networkId: "kn_demo",
      networkName: "演示网络",
      objectTypes: [
        {
          id: "device",
          name: "设备",
          sourceTable: "t_device",
          properties: [
            { name: "name", displayName: "名称", type: "string", mappedField: "device_name" },
            { name: "title", displayName: "名称", type: "string", mappedField: "device_name" },
          ],
        },
      ],
      relations: [],
    });
    expect(plan.suggestions[0]?.primaryKeyField).toBe("device_name");
    expect(plan.suggestions[0]?.fulltextFields).toEqual(["device_name"]);
  });
});
