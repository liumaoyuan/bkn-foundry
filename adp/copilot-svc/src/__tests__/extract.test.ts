import { zipSync, strToU8 } from "fflate";
import { describe, expect, it } from "vitest";

import { extractDocument, SUPPORTED_EXTENSIONS } from "../extract.js";

const toBuffer = (text: string): Buffer => Buffer.from(text, "utf8");

describe("extractDocument", () => {
  it("passes csv text through", async () => {
    const result = await extractDocument("设备台账.csv", toBuffer("设备编号,设备名称\nEQ-001,空压机"));
    expect(result.text).toContain("EQ-001");
    expect(result.characters).toBeGreaterThan(0);
  });

  it("turns create-table sql into schema text", async () => {
    const sql = [
      "CREATE TABLE `device` (",
      "  `id` varchar(64) NOT NULL COMMENT '主键',",
      "  `device_name` varchar(128) COMMENT '设备名称',",
      "  PRIMARY KEY (`id`)",
      ") ENGINE=InnoDB;",
    ].join("\n");
    const result = await extractDocument("schema.sql", toBuffer(sql));
    expect(result.text).toContain("## 表 device");
    expect(result.text).toContain("- id VARCHAR：主键");
    expect(result.text).toContain("- device_name VARCHAR：设备名称");
  });

  it("extracts slide text from pptx", async () => {
    const slideXml =
      '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><a:t>设备包含部件</a:t><a:t>部件发生故障</a:t></p:sld>';
    const archive = zipSync({ "ppt/slides/slide1.xml": strToU8(slideXml) });
    const result = await extractDocument("方案.pptx", Buffer.from(archive));
    expect(result.text).toContain("第 1 页");
    expect(result.text).toContain("设备包含部件");
  });

  it("strips html tags", async () => {
    const html = "<html><head><style>p{}</style></head><body><h1>设备</h1><p>包含部件</p></body></html>";
    const result = await extractDocument("notes.html", toBuffer(html));
    expect(result.text).toContain("设备");
    expect(result.text).toContain("包含部件");
    expect(result.text).not.toContain("<h1>");
  });

  it("rejects legacy doc with an actionable hint", async () => {
    await expect(extractDocument("old.doc", toBuffer("x"))).rejects.toThrow(/另存为 .docx/);
  });

  it("keeps the advertised extension list in sync", () => {
    expect(SUPPORTED_EXTENSIONS).toContain(".xls");
    expect(SUPPORTED_EXTENSIONS).toContain(".pptx");
    expect(SUPPORTED_EXTENSIONS).toContain(".sql");
  });
});
