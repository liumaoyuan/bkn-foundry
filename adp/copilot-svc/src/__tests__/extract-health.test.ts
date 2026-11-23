import { describe, expect, it } from "vitest";
import { read, utils, write } from "xlsx";
import { extractTables, inspectPdf } from "../extract.js";

/** 用二维数组造一个 xlsx 文件（可带合并单元格）。 */
function buildWorkbook(rows: unknown[][], merges?: Array<{ s: { r: number; c: number }; e: { r: number; c: number } }>): Buffer {
  const sheet = utils.aoa_to_sheet(rows);
  if (merges && merges.length > 0) {
    sheet["!merges"] = merges;
  }
  const workbook = utils.book_new();
  utils.book_append_sheet(workbook, sheet, "Sheet1");
  return write(workbook, { type: "buffer", bookType: "xlsx" }) as Buffer;
}

describe("inspectPdf", () => {
  it("没有文字层时判定为扫描件", () => {
    const health = inspectPdf(Buffer.from("%PDF-1.4\n%%EOF"), "");
    expect(health.map((item) => item.code)).toEqual(["pdf-no-text"]);
  });

  it("每页字数过少时判定为疑似扫描件", () => {
    const buffer = Buffer.from("%PDF-1.4\n/Type /Page\n/Type /Page\n%%EOF", "latin1");
    const health = inspectPdf(buffer, "扫描件正文".repeat(5));
    expect(health.map((item) => item.code)).toEqual(["pdf-likely-scanned"]);
    expect(health[0]?.message).toContain("疑似扫描件");
  });

  it("正常文字层不报警", () => {
    const buffer = Buffer.from("%PDF-1.4\n/Type /Page\n%%EOF", "latin1");
    const health = inspectPdf(buffer, "设备运维知识网络".repeat(100));
    expect(health).toEqual([]);
  });
});

describe("extractTables 表头还原", () => {
  it("合并的分组表头会展开成一级/二级", async () => {
    const rows = [
      ["设备信息", "", "运行数据"],
      ["设备编号", "设备名称", "运行状态"],
      ["EQ-001", "空压机", "运行"],
    ];
    const buffer = buildWorkbook(rows, [{ s: { r: 0, c: 0 }, e: { r: 0, c: 1 } }]);
    const tables = await extractTables("分组表头.xlsx", buffer);
    expect(tables[0]?.headers).toEqual(["设备信息/设备编号", "设备信息/设备名称", "运行数据/运行状态"]);
    expect(tables[0]?.headerRowCount).toBe(2);
    const codes = (tables[0]?.health ?? []).map((item) => item.code);
    expect(codes).toContain("excel-merged-header");
    expect(codes).toContain("excel-multirow-header");
  });

  it("整行大标题不会被并入列名", async () => {
    const rows = [
      ["设备台账表", "", ""],
      ["设备编号", "设备名称", "运行状态"],
      ["EQ-001", "空压机", "运行"],
    ];
    const buffer = buildWorkbook(rows, [{ s: { r: 0, c: 0 }, e: { r: 0, c: 2 } }]);
    const tables = await extractTables("大标题.xlsx", buffer);
    expect(tables[0]?.headers).toEqual(["设备编号", "设备名称", "运行状态"]);
    expect(tables[0]?.headerRowCount).toBe(2);
  });

  it("三级表头（大标题 + 分组 + 字段）能逐层还原", async () => {
    const rows = [
      ["设备台账表", "", "", ""],
      ["设备信息", "", "运行数据", ""],
      ["设备编号", "设备名称", "运行状态", "额定功率(kW)"],
      ["EQ-001", "空压机", "运行", "75"],
    ];
    const buffer = buildWorkbook(rows, [
      { s: { r: 0, c: 0 }, e: { r: 0, c: 3 } },
      { s: { r: 1, c: 0 }, e: { r: 1, c: 1 } },
      { s: { r: 1, c: 2 }, e: { r: 1, c: 3 } },
    ]);
    const tables = await extractTables("三级表头.xlsx", buffer);
    expect(tables[0]?.headerRowCount).toBe(3);
    expect(tables[0]?.headers).toEqual([
      "设备信息/设备编号",
      "设备信息/设备名称",
      "运行数据/运行状态",
      "运行数据/额定功率(kW)",
    ]);
    expect(tables[0]?.samples[0]).toEqual(["EQ-001", "空压机", "运行", "75"]);
  });

  it("单行表头保持原样，不产生健康提示", async () => {
    const rows = [
      ["设备编号", "设备名称", "额定功率(kW)"],
      ["EQ-001", "空压机", "75"],
      ["EQ-002", "冷却塔", "30"],
    ];
    const tables = await extractTables("普通表.xlsx", buildWorkbook(rows));
    expect(tables[0]?.headers).toEqual(["设备编号", "设备名称", "额定功率(kW)"]);
    expect(tables[0]?.headerRowCount).toBe(1);
    expect(tables[0]?.health).toBeUndefined();
    expect(tables[0]?.samples[0]).toEqual(["EQ-001", "空压机", "75"]);
  });

  it("数据区有值但表头为空的列会给出提示", async () => {
    const rows = [
      ["设备编号", "", "运行状态"],
      ["EQ-001", "空压机", "运行"],
      ["EQ-002", "冷却塔", "停机"],
    ];
    const tables = await extractTables("空表头.xlsx", buildWorkbook(rows));
    expect(tables[0]?.headers).toEqual(["设备编号", "运行状态"]);
    expect((tables[0]?.health ?? []).map((item) => item.code)).toContain("excel-empty-header");
  });
});
