import { describe, expect, it } from "vitest";

import { applyIrOps } from "../edit.js";
import type { OntologyIr } from "../types/ir.js";

const draft = (): OntologyIr => ({
  networkId: "kn_test",
  networkName: "测试网络",
  description: "",
  objectTypes: [
    {
      id: "missile_site",
      name: "导弹阵地",
      description: "",
      tags: [],
      properties: [],
      primaryKey: "id",
      displayKey: "name",
    },
    {
      id: "equipment",
      name: "装备",
      description: "",
      tags: [],
      properties: [],
      primaryKey: "id",
      displayKey: "name",
    },
  ],
  relations: [
    {
      id: "equipment_missile_site",
      name: "部署于",
      description: "",
      source: "equipment",
      target: "missile_site",
    },
  ],
  conceptGroups: [{ id: "cg_x", name: "组", objectTypes: ["equipment"] }],
});

describe("applyIrOps", () => {
  it("removes an object case-insensitively by name and cascades relations", () => {
    const result = applyIrOps(draft(), [{ op: "remove_object", name: "装备" }]);
    expect(result.applied[0]).toContain("删除对象类型「装备」");
    expect(result.ir.objectTypes.map((item) => item.id)).toEqual(["missile_site"]);
    expect(result.ir.relations).toHaveLength(0);
    expect(result.ir.conceptGroups?.[0]?.objectTypes).toEqual([]);
  });

  it("reports a warning when the object does not exist", () => {
    const result = applyIrOps(draft(), [{ op: "remove_object", name: "不存在的对象" }]);
    expect(result.applied).toHaveLength(0);
    expect(result.warnings[0]).toContain("不存在");
    expect(result.ir.objectTypes).toHaveLength(2);
  });

  it("adds an object with a unique id and can add a relation to it", () => {
    const withObject = applyIrOps(draft(), [{ op: "add_object", name: "组织" }]).ir;
    const added = withObject.objectTypes.at(-1);
    expect(added?.name).toBe("组织");
    expect(added?.id).toMatch(/^obj/);

    const withRelation = applyIrOps(withObject, [
      { op: "add_relation", source: "装备", target: "组织", name: "归属" },
    ]);
    expect(withRelation.applied[0]).toContain("新增关系「归属」");
    expect(withRelation.ir.relations.at(-1)).toMatchObject({
      source: "equipment",
      target: added?.id,
      name: "归属",
    });
  });

  it("renames an object, keeps relations pointing at the new id", () => {
    const result = applyIrOps(draft(), [
      { op: "rename_object", id: "equipment", newName: "weapon" },
    ]);
    expect(result.ir.objectTypes.map((item) => item.id)).toEqual(["missile_site", "weapon"]);
    expect(result.ir.relations[0].source).toBe("weapon");
  });

  it("removes a relation by name", () => {
    const result = applyIrOps(draft(), [{ op: "remove_relation", name: "部署于" }]);
    expect(result.ir.relations).toHaveLength(0);
    expect(result.applied[0]).toContain("删除关系「部署于」");
  });

  it("ignores unknown ops with a warning", () => {
    const result = applyIrOps(draft(), [{ op: "drop_database" }]);
    expect(result.warnings[0]).toContain("未知操作");
    expect(result.ir.objectTypes).toHaveLength(2);
  });
it("adds an action type bound to an existing object", () => {
    const result = applyIrOps(draft(), [
      {
        op: "add_action",
        name: "派工",
        actionType: "modify",
        boundObject: "导弹阵地",
        riskLevel: "high",
        requiresApproval: true,
      },
    ]);
    expect(result.ir.actionTypes).toHaveLength(1);
    const action = result.ir.actionTypes?.[0];
    expect(action?.name).toBe("派工");
    expect(action?.boundObject).toBe("missile_site");
    expect(action?.riskLevel).toBe("high");
    expect(result.applied[0]).toContain("新增行动类型「派工」");
  });

  it("updates an existing action to require approval", () => {
    const withAction = applyIrOps(draft(), [
      { op: "add_action", name: "派工", actionType: "modify", boundObject: "导弹阵地" },
    ]).ir;
    const result = applyIrOps(withAction, [
      { op: "update_action", name: "派工", riskLevel: "high", requiresApproval: true },
    ]);
    const action = result.ir.actionTypes?.[0];
    expect(action?.riskLevel).toBe("high");
    expect(action?.requiresApproval).toBe(true);
    expect(result.applied[0]).toContain("修改行动类型「派工」");
  });

  it("removes an action type by name", () => {
    const withAction = applyIrOps(draft(), [
      { op: "add_action", name: "派工", actionType: "modify", boundObject: "导弹阵地" },
    ]).ir;
    const result = applyIrOps(withAction, [{ op: "remove_action", name: "派工" }]);
    expect(result.ir.actionTypes).toHaveLength(0);
    expect(result.applied[0]).toContain("删除行动类型「派工」");
  });

  it("adds and updates a risk type with policies", () => {
    const withRisk = applyIrOps(draft(), [
      {
        op: "add_risk",
        name: "工单超期",
        policies: ["超过24小时升级", ""],
      },
    ]).ir;
    expect(withRisk.riskTypes).toHaveLength(1);
    expect(withRisk.riskTypes?.[0].policies).toEqual(["超过24小时升级"]);

    const updated = applyIrOps(withRisk, [
      { op: "update_risk", name: "工单超期", policies: ["超过12小时提醒", "超过24小时升级"] },
    ]);
    expect(updated.ir.riskTypes?.[0].policies).toHaveLength(2);
    expect(updated.applied[0]).toContain("修改风险类型「工单超期」");
  });

  it("removes risk type bound by name", () => {
    const withRisk = applyIrOps(draft(), [{ op: "add_risk", name: "工单超期", policies: ["升级"] }]).ir;
    const result = applyIrOps(withRisk, [{ op: "remove_risk", name: "工单超期" }]);
    expect(result.ir.riskTypes).toHaveLength(0);
    expect(result.applied[0]).toContain("删除风险类型「工单超期」");
  });
});