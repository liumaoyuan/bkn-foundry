import { describe, expect, it } from "vitest";
import { transformRecords, type ImportMapping } from "../data-import.js";

const mapping: ImportMapping = {
  objects: [
    {
      objectTypeId: "ship",
      objectTypeName: "舰艇",
      primaryKeyColumn: "ship_id",
      fields: [
        { sourceColumn: "ship_id", propertyName: "ship_id", type: "string" },
        { sourceColumn: "ship_class", propertyName: "ship_class", type: "string" },
      ],
    },
    {
      objectTypeId: "officer",
      objectTypeName: "军官",
      primaryKeyColumn: "officer_id",
      fields: [
        { sourceColumn: "officer_id", propertyName: "officer_id", type: "string" },
        { sourceColumn: "name", propertyName: "name", type: "string" },
      ],
    },
  ],
};

describe("transformRecords row routing", () => {
  it("routes each row to a single object type instead of writing all types", () => {
    const rows = [
      { ship_id: "S1", ship_class: "驱逐舰" },
      { officer_id: "O1", name: "张三" },
      { ship_id: "S2", ship_class: "护卫舰" },
    ];
    const result = transformRecords(rows, mapping);
    expect(result.documents.ship).toHaveLength(2);
    expect(result.documents.officer).toHaveLength(1);
    expect(result.stats.byObjectType).toEqual({ ship: 2, officer: 1 });
    expect(result.stats.documents).toBe(3);
    expect(result.stats.skipped).toBe(0);
  });

  it("records skipped reasons when a row matches no object type", () => {
    const result = transformRecords([{ something: "x" }], mapping);
    expect(result.stats.documents).toBe(0);
    expect(result.stats.skipped).toBe(1);
    expect(result.stats.skippedReasons?.no_object_match).toBe(1);
  });

  it("dedupes repeated business keys inside one object type", () => {
    const rows = [
      { ship_id: "S1", ship_class: "驱逐舰" },
      { ship_id: "S1", ship_class: "驱逐舰改" },
    ];
    const result = transformRecords(rows, mapping);
    expect(result.documents.ship).toHaveLength(1);
    expect(result.stats.deduped).toBe(1);
  });
});
