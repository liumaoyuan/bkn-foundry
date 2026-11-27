/**
 * 把上传材料拼成给模型的分析输入：设计报告用于理解业务语义，数据表用于推导结构。
 */

export type AnalyzeSourceParts = {
  /** 设计报告类文件（Word/PDF/PPT/Markdown 等）解析后的文本块。 */
  report: string[];
  /** 数据类文件（Excel/CSV/JSON/SQL 等）解析后的文本块。 */
  data: string[];
  /** 未标明类型的文件（兼容旧的 files 字段）。 */
  plain: string[];
  /** 用户粘贴的文字。 */
  pasted: string;
};

const CROSS_HINT =
  "（说明：本次同时提供了设计报告与数据表。请交叉分析：以数据表的字段与外键推导对象类型和关系，以设计报告校准对象命名、业务含义与关系方向。）";

/** 拼装分析输入；两类文件同时存在时追加交叉分析说明。 */
export function composeAnalyzeSource(parts: AnalyzeSourceParts): string {
  const sections: string[] = [];
  if (parts.report.length > 0) {
    sections.push(`# 设计报告（用于理解业务语义、对象命名与规则）\n${parts.report.join("\n\n")}`);
  }
  if (parts.data.length > 0) {
    sections.push(`# 数据表（用于推导对象类型、属性与关系）\n${parts.data.join("\n\n")}`);
  }
  if (parts.plain.length > 0) {
    sections.push(`# 其他材料\n${parts.plain.join("\n\n")}`);
  }
  if (parts.pasted.trim()) {
    sections.push(parts.pasted);
  }
  if (parts.report.length > 0 && parts.data.length > 0) {
    sections.push(CROSS_HINT);
  }
  return sections.join("\n\n").trim();
}
