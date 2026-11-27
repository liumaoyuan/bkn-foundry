/**
 * Copyright (c) 2026 OpenBKN
 * SPDX-License-Identifier: LicenseRef-OpenBKN
 * Licensed under the OpenBKN License, a modified Apache 2.0 with Additional
 * Conditions. See LICENSE for the full text.
 */

import { describe, expect, it } from "vitest";

import { applyIrOps } from "../edit.js";
import { normalizeIr } from "../types/ir.js";

const baseIr = {
  networkId: "equipment_maintenance",
  networkName: "设备运维",
  objectTypes: [
    { id: "work_order", name: "维修工单", properties: [{ name: "id", displayName: "编号", type: "string" as const }] },
    { id: "fault", name: "故障", properties: [{ name: "id", displayName: "编号", type: "string" as const }] },
  ],
  relations: [],
};

describe("four elements normalize", () => {
  it("keeps valid action and risk types", () => {
    const result = normalizeIr({
      ...baseIr,
      actionTypes: [
        {
          id: "act_create_order",
          name: "创建工单",
          actionType: "add",
          boundObject: "work_order",
          riskLevel: "medium",
          trigger: { field: "status", operation: "==", value: "待处理" },
        },
      ],
      riskTypes: [
        {
          id: "risk_overdue",
          name: "工单超期",
          description: "超过 24 小时未关闭",
          policies: ["超过 24 小时升级班组长", "  "],
        },
      ],
    });
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.ir.actionTypes).toHaveLength(1);
    expect(result.ir.actionTypes?.[0]?.actionType).toBe("add");
    expect(result.ir.actionTypes?.[0]?.trigger?.field).toBe("status");
    expect(result.ir.riskTypes?.[0]?.policies).toEqual(["超过 24 小时升级班组长"]);
  });

  it("drops action types with unknown bound object or invalid action value", () => {
    const result = normalizeIr({
      ...baseIr,
      actionTypes: [
        { id: "act_a", name: "无对象动作", actionType: "add", boundObject: "missing" },
        { id: "act_b", name: "非法动作", actionType: "upsert", boundObject: "fault" },
        { id: "act_c", name: "合法动作", actionType: "delete", boundObject: "fault" },
      ],
    });
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.ir.actionTypes?.map((item) => item.id)).toEqual(["act_c"]);
    expect(result.warnings.length).toBeGreaterThan(0);
  });
});

describe("action type cascade", () => {
  it("removes action types bound to a deleted object", () => {
    const result = applyIrOps(
      {
        ...baseIr,
        actionTypes: [
          { id: "act_fault", name: "标记故障已处理", actionType: "modify", boundObject: "fault" },
          { id: "act_order", name: "关闭工单", actionType: "modify", boundObject: "work_order" },
        ],
      },
      [{ op: "remove_object", name: "故障" }],
    );
    expect(result.ir.objectTypes.map((item) => item.id)).toEqual(["work_order"]);
    expect(result.ir.actionTypes?.map((item) => item.id)).toEqual(["act_order"]);
    expect(result.applied.join()).toContain("行动类型");
  });
});