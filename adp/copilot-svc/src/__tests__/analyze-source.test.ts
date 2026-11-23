import { describe, expect, it } from "vitest";

import { composeAnalyzeSource } from "../analyze-source.js";

describe("composeAnalyzeSource", () => {
  it("keeps a report-only input without the cross-analysis hint", () => {
    const text = composeAnalyzeSource({
      report: ["## 文件：设计说明.md\n设备台账与故障工单的业务流程"],
      data: [],
      plain: [],
      pasted: "",
    });

    expect(text).toContain("# 设计报告");
    expect(text).toContain("设备台账与故障工单");
    expect(text).not.toContain("# 数据表");
    expect(text).not.toContain("交叉分析");
  });

  it("keeps a data-only input with its own section", () => {
    const text = composeAnalyzeSource({
      report: [],
      data: ["## 文件：设备台账.xlsx\n设备编号 | 设备名称"],
      plain: [],
      pasted: "",
    });

    expect(text).toContain("# 数据表");
    expect(text).toContain("设备编号");
    expect(text).not.toContain("# 设计报告");
    expect(text).not.toContain("交叉分析");
  });

  it("adds the cross-analysis hint when both kinds are present", () => {
    const text = composeAnalyzeSource({
      report: ["## 文件：设计说明.md\n报告正文"],
      data: ["## 文件：台账.xlsx\n台账数据"],
      plain: [],
      pasted: "请构建运维本体",
    });

    expect(text.indexOf("# 设计报告")).toBeLessThan(text.indexOf("# 数据表"));
    expect(text).toContain("报告正文");
    expect(text).toContain("台账数据");
    expect(text).toContain("交叉分析");
    expect(text.trim().endsWith("）")).toBe(true);
  });

  it("keeps untyped files under an '其他材料' section", () => {
    const text = composeAnalyzeSource({
      report: [],
      data: [],
      plain: ["## 文件：notes.txt\n随手记"],
      pasted: "",
    });

    expect(text).toContain("# 其他材料");
    expect(text).toContain("随手记");
  });
});
