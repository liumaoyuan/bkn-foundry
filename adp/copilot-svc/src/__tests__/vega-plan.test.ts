/**
 * Copyright (c) 2026 OpenBKN
 * SPDX-License-Identifier: LicenseRef-OpenBKN
 * Licensed under the OpenBKN License, a modified Apache 2.0 with Additional
 * Conditions. See LICENSE for the full text.
 */

import { describe, expect, it } from "vitest";

import { buildAttachPlan } from "../vega-plan.js";
import type { OntologyIr } from "../types/ir.js";

const ir: OntologyIr = {
  networkId: "kn_demo",
  networkName: "设备运维网络",
  objectTypes: [
    {
      id: "equipment",
      name: "设备",
      description: "生产设备",
      sourceTable: "t_equip",
      primaryKey: "equip_code",
      properties: [
        { name: "equip_code", displayName: "设备编号", type: "string", mappedField: "equip_code" },
        { name: "equip_name", displayName: "设备名称", type: "string", mappedField: "equip_name" },
        { name: "rated_power", displayName: "额定功率", type: "number", mappedField: "rated_power" },
      ],
    },
    {
      id: "fault",
      name: "故障",
      sourceTable: "t_fault",
      primaryKey: "fault_code",
      properties: [{ name: "fault_code", displayName: "故障编号", type: "string", mappedField: "fault_code" }],
    },
    {
      id: "manual",
      name: "手工对象",
      properties: [{ name: "id", displayName: "ID", type: "string" }],
    },
  ],
  relations: [],
};

const target = {
  dialect: "mysql" as const,
  host: "192.168.202.100",
  port: 3306,
  database: "industrial_ops",
  user: "root",
  password: "123456",
};

describe("buildAttachPlan", () => {
  it("builds a mysql catalog with resources and bindings", () => {
    const plan = buildAttachPlan(ir, target);
    expect(plan.catalog.connectorType).toBe("mysql");
    expect(plan.catalog.connectorConfig).toMatchObject({
      host: "192.168.202.100",
      port: 3306,
      database: "industrial_ops",
      username: "root",
      password: "123456",
      charset: "utf8mb4",
    });
    expect(plan.catalog.name).toBe("设备运维网络 数据源");
    expect(plan.resources.map((item) => item.name)).toEqual(["t_equip", "t_fault"]);
    expect(plan.resources[0]?.schemaDefinition[2]).toMatchObject({
      name: "rated_power",
      display_name: "额定功率",
      type: "double",
    });
    expect(plan.resources[0]?.indexConfig.primaryKeyFields).toEqual(["equip_code"]);
    expect(plan.bindings[0]).toMatchObject({
      objectId: "equipment",
      resourceName: "t_equip",
      primaryKey: "equip_code",
    });
    expect(plan.bindings[0]?.properties).toHaveLength(3);
  });

  it("uses postgres connector defaults and schema", () => {
    const plan = buildAttachPlan(ir, { ...target, dialect: "postgres", port: undefined, schema: "ops" });
    expect(plan.catalog.connectorType).toBe("postgresql");
    expect(plan.catalog.connectorConfig).toMatchObject({ port: 5432, schema: "ops", ssl_mode: "prefer" });
  });

  it("warns about objects without a source table", () => {
    const plan = buildAttachPlan(ir, target);
    expect(plan.warnings).toEqual(["对象「手工对象」没有来源表，跳过资源创建"]);
  });
});
