import { describe, expect, it } from "vitest";

import { attachEvidence, type EvidenceSource } from "../evidence.js";
import type { OntologyIr } from "../types/ir.js";

const baseIr = (): OntologyIr => ({
  networkId: "kn_demo",
  networkName: "设备运维网络",
  objectTypes: [
    {
      id: "equipment",
      name: "设备",
      properties: [
        { name: "id", displayName: "ID", type: "string" },
        { name: "equipment_code", displayName: "设备编号", type: "string", mappedField: "设备编号" },
      ],
      primaryKey: "id",
      displayKey: "equipment_code",
    },
    {
      id: "fault",
      name: "故障",
      properties: [{ name: "id", displayName: "ID", type: "string" }],
    },
  ],
  relations: [
    {
      id: "equipment_has_fault",
      name: "发生故障",
      source: "equipment",
      target: "fault",
      mapping: [{ sourceProperty: "id", targetProperty: "id" }],
    },
  ],
  actionTypes: [
    {
      id: "create_order",
      name: "创建设备工单",
      actionType: "add",
      boundObject: "equipment",
    },
  ],
  riskTypes: [
    {
      id: "equipment_overdue",
      name: "设备超期未保养",
      policies: ["超过保养周期自动提醒"],
    },
  ],
});

describe("attachEvidence", () => {
  it("matches object and property names with surrounding quotes", () => {
    const sources: EvidenceSource[] = [
      {
        fileName: "设备运维设计报告.md",
        text: "设备台账记录所有设备。设备编号是唯一标识，设备发生故障时需要记录。",
      },
    ];
    const result = attachEvidence(baseIr(), sources);
    const equipment = result.ir.objectTypes.find((item) => item.id === "equipment");
    expect(equipment?.evidence?.[0]?.source).toBe("设备运维设计报告.md");
    expect(equipment?.evidence?.[0]?.quote).toContain("设备");
    const code = equipment?.properties.find((item) => item.name === "equipment_code");
    expect(code?.evidence?.length).toBeGreaterThan(0);
    expect(code?.evidence?.[0]?.quote).toContain("设备编号");
    expect(result.stats.objectsWithEvidence).toBeGreaterThan(0);
    expect(result.stats.propertiesWithEvidence).toBeGreaterThan(0);
  });

  it("records table location for header-only sources", () => {
    const sources: EvidenceSource[] = [
      { fileName: "设备运维数据包.xlsx", location: "10-设备台账", text: "设备编号 设备名称 运行状态" },
    ];
    const result = attachEvidence(baseIr(), sources);
    const code = result.ir.objectTypes
      .find((item) => item.id === "equipment")
      ?.properties.find((item) => item.name === "equipment_code");
    expect(code?.evidence?.[0]?.location).toBe("10-设备台账");
  });

  it("adds evidence for relations, actions and risks", () => {
    const sources: EvidenceSource[] = [
      {
        fileName: "设计报告.md",
        text: "设备发生故障后需要创建工单；设备超期未保养属于风险，超过保养周期自动提醒。",
      },
    ];
    const result = attachEvidence(baseIr(), sources);
    expect(result.stats.relationsWithEvidence).toBeGreaterThan(0);
    expect(result.stats.actionsWithEvidence).toBeGreaterThan(0);
    expect(result.stats.risksWithEvidence).toBeGreaterThan(0);
  });

  it("leaves items without matches untouched and returns zero stats", () => {
    const ir = baseIr();
    const result = attachEvidence(ir, [{ fileName: "无关.md", text: "今天天气不错，适合出门散步。" }]);
    expect(result.stats.objectsWithEvidence).toBe(0);
    expect(result.stats.propertiesWithEvidence).toBe(0);
    expect(result.ir.objectTypes[0]?.evidence).toBeUndefined();
    expect(result.ir.relations[0]?.evidence).toBeUndefined();
  });

  it("ignores empty sources and single-character terms", () => {
    const result = attachEvidence(baseIr(), [
      { fileName: "空.md", text: "   " },
      { fileName: "短.md", text: "设" },
    ]);
    expect(result.stats.objectsWithEvidence).toBe(0);
    expect(result.stats.objects).toBe(2);
    expect(result.stats.properties).toBe(3);
  });

  it("fuzzy-matches abstract risk names by keyword fragments", () => {
    const ir = baseIr();
    ir.riskTypes = [
      { id: "order_overdue", name: "工单超期未关闭", policies: ["超期 24 小时升级至班组长"] },
    ];
    const sources: EvidenceSource[] = [
      {
        fileName: "运维手册.md",
        text: "工单超期会影响考核，维修班组需关注工单处理时效。",
      },
    ];
    const result = attachEvidence(ir, sources);
    expect(result.stats.risksWithEvidence).toBe(1);
    const risk = result.ir.riskTypes?.find((item) => item.id === "order_overdue");
    expect(risk?.evidence?.[0]?.quote).toContain("工单超期");
  });

  it("truncates long quotes", () => {
    const longText = `${"设备编号".repeat(30)}${"描述".repeat(80)}`;
    const result = attachEvidence(baseIr(), [{ fileName: "长文.md", text: longText }]);
    const quote = result.ir.objectTypes.find((item) => item.id === "equipment")?.evidence?.[0]?.quote ?? "";
    expect(quote.length).toBeLessThanOrEqual(121);
  });
});
