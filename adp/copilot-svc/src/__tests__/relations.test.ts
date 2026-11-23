import { describe, expect, it } from "vitest";

import { dedupeRelations, dedupeWarning } from "../relations.js";
import type { IrRelation } from "../types/ir.js";

const relation = (
  id: string,
  name: string,
  source: string,
  target: string,
): IrRelation => ({ id, name, source, target });

describe("dedupeRelations", () => {
  it("合并同一对端点上的同义关系", () => {
    const result = dedupeRelations([
      relation("r1", "生成工单", "fault", "work_order"),
      relation("r2", "触发工单", "fault", "work_order"),
      relation("r3", "产生工单", "fault", "work_order"),
    ]);

    expect(result.relations).toHaveLength(1);
    expect(result.relations[0]?.name).toBe("生成工单");
    expect(result.removed).toBe(2);
    expect(result.mergedNames).toEqual(["触发工单", "产生工单"]);
  });

  it("保留指向不同对象的关系", () => {
    const result = dedupeRelations([
      relation("r1", "发生故障", "equipment", "fault"),
      relation("r2", "发生故障", "component", "fault"),
    ]);

    expect(result.relations).toHaveLength(2);
    expect(result.removed).toBe(0);
  });

  it("不同动词保留为不同关系", () => {
    const result = dedupeRelations([
      relation("r1", "包含部件", "equipment", "component"),
      relation("r2", "消耗备件", "equipment", "component"),
    ]);

    expect(result.relations).toHaveLength(2);
  });

  it("完全相同的关系也只保留一条", () => {
    const result = dedupeRelations([
      relation("r1", "部署于", "missile", "position"),
      relation("r2", "部署于", "missile", "position"),
    ]);

    expect(result.relations).toHaveLength(1);
    expect(result.removed).toBe(1);
  });

  it("无重复时返回提示 undefined", () => {
    const result = dedupeRelations([relation("r1", "包含部件", "equipment", "component")]);
    expect(dedupeWarning(result)).toBeUndefined();
  });

  it("有重复时生成可读提示", () => {
    const result = dedupeRelations([
      relation("r1", "生成工单", "fault", "work_order"),
      relation("r2", "触发工单", "fault", "work_order"),
    ]);
    expect(dedupeWarning(result)).toContain("已合并 1 条");
    expect(dedupeWarning(result)).toContain("触发工单");
  });
});
