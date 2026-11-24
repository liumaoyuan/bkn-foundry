import { createRequire } from "node:module";
import { extname } from "node:path";
import { unzipSync } from "fflate";

/** 输入体检发现的问题（不静默截断/不静默丢内容，返回给前端提示用户）。 */
export type ExtractHealth = {
  code:
    | "pdf-no-text"
    | "pdf-likely-scanned"
    | "pdf-short-text"
    | "excel-merged-header"
    | "excel-multirow-header"
    | "excel-empty-header";
  message: string;
};

export type ExtractedDocument = {
  fileName: string;
  text: string;
  characters: number;
  /** 解析体检结果：扫描件、表头异常等，供前端提示。 */
  health?: ExtractHealth[];
};

const MAX_CHARS_PER_FILE = 200_000;

/** 上传框允许选择的扩展名（前端 accept 与后端校验共用这份清单）。 */
export const SUPPORTED_EXTENSIONS = [
  ".docx",
  ".pdf",
  ".xlsx",
  ".xls",
  ".csv",
  ".tsv",
  ".pptx",
  ".sql",
  ".json",
  ".html",
  ".htm",
  ".md",
  ".markdown",
  ".txt",
  ".yaml",
  ".yml",
] as const;

/** 明确不支持、需要用户先转换的类型，给出可操作提示。 */
const UNSUPPORTED_HINTS: Record<string, string> = {
  ".doc": "旧版 .doc 无法直接解析，请另存为 .docx 后重试",
  ".ppt": "旧版 .ppt 无法直接解析，请另存为 .pptx 后重试",
  ".png": "图片暂不支持 OCR，请截图转成 PDF 或直接粘贴文字",
  ".jpg": "图片暂不支持 OCR，请截图转成 PDF 或直接粘贴文字",
  ".jpeg": "图片暂不支持 OCR，请截图转成 PDF 或直接粘贴文字",
  ".rtf": "富文本 .rtf 暂不支持，请另存为 .docx 后重试",
};

const truncate = (text: string): string =>
  text.length > MAX_CHARS_PER_FILE ? text.slice(0, MAX_CHARS_PER_FILE) : text;

const stripBom = (text: string): string => (text.charCodeAt(0) === 0xfeff ? text.slice(1) : text);

async function extractDocx(buffer: Buffer): Promise<string> {
  const mammoth = await import("mammoth");
  const result = await mammoth.extractRawText({ buffer });
  return result.value;
}

async function extractSpreadsheet(buffer: Buffer): Promise<string> {
  const { read, utils } = await import("xlsx");
  const workbook = read(buffer, { type: "buffer" });
  const blocks: string[] = [];

  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    const csv = utils.sheet_to_csv(sheet, { blankrows: false });
    if (csv.trim()) {
      blocks.push(`## 工作表：${sheetName}\n${csv.trim()}`);
    }
  }

  return blocks.join("\n\n");
}

async function extractPdf(buffer: Buffer): Promise<string> {
  const require = createRequire(import.meta.url);
  // pdf-parse 是 CJS 包且没有 exports 映射，用 require 加载避免 bundler 解析深路径。
  const pdfParse = require("pdf-parse") as (data: Buffer) => Promise<{ text: string }>;
  const result = await pdfParse(buffer);
  return result.text;
}

/** pptx：解压后按页码顺序抽取幻灯片里的文字（含文本框/表格单元格）。 */
function extractPptx(buffer: Buffer): string {
  const archive = unzipSync(new Uint8Array(buffer));
  const slides = Object.keys(archive)
    .filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path))
    .sort((left, right) => {
      const leftNo = Number(left.match(/slide(\d+)\.xml$/)?.[1] ?? 0);
      const rightNo = Number(right.match(/slide(\d+)\.xml$/)?.[1] ?? 0);
      return leftNo - rightNo;
    });

  const decoder = new TextDecoder("utf-8");
  const blocks: string[] = [];
  for (const path of slides) {
    const xml = decoder.decode(archive[path]);
    const texts = [...xml.matchAll(/<a:t>([\s\S]*?)<\/a:t>/g)]
      .map((match) => match[1].replace(/<[^>]+>/g, "").trim())
      .filter(Boolean);
    if (texts.length > 0) {
      blocks.push(`## 第 ${path.match(/slide(\d+)\.xml$/)?.[1] ?? "?"} 页\n${texts.join("\n")}`);
    }
  }
  return blocks.join("\n\n");
}

/**
 * sql：把建表语句翻译成「表名 + 列名/类型/注释」的文本，便于模型理解表结构。
 * 只处理 CREATE TABLE，其余语句（INSERT 等）忽略，避免把业务数据当成 schema。
 */
/** 从 `(` 位置向后找配对的 `)`，跳过字符串常量里的括号。 */
function findMatchingParen(text: string, openIndex: number): number {
  let depth = 0;
  let quote = "";
  for (let index = openIndex; index < text.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === quote) {
        quote = "";
      }
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
    } else if (char === "(") {
      depth += 1;
    } else if (char === ")") {
      depth -= 1;
      if (depth === 0) {
        return index;
      }
    }
  }
  return -1;
}

function extractSql(buffer: Buffer): string {
  const source = stripBom(buffer.toString("utf8"));
  const blocks: string[] = [];

  for (const match of source.matchAll(
    /create\s+table\s+(?:if\s+not\s+exists\s+)?[`"[]?([\w.]+)[`"\]]?\s*\(/gi,
  )) {
    const openIndex = (match.index ?? 0) + match[0].length - 1;
    const closeIndex = findMatchingParen(source, openIndex);
    if (closeIndex < 0) {
      continue;
    }
    const tableName = match[1];
    const body = source.slice(openIndex + 1, closeIndex);
    const columns: string[] = [];
    for (const line of body.split(/\r?\n/)) {
      const trimmed = line.trim().replace(/,$/, "");
      if (!trimmed) {
        continue;
      }
      if (/^(primary|unique|key|index|constraint|foreign)\b/i.test(trimmed)) {
        continue;
      }
      const column = trimmed.match(/^[`"[]?(\w+)[`"\]]?\s+([a-z]+)/i);
      if (column) {
        const comment = trimmed.match(/comment\s+'([^']*)'/i)?.[1];
        columns.push(`  - ${column[1]} ${column[2].toUpperCase()}${comment ? `：${comment}` : ""}`);
      }
    }
    if (columns.length > 0) {
      blocks.push(`## 表 ${tableName}\n${columns.join("\n")}`);
    }
  }

  if (blocks.length > 0) {
    return blocks.join("\n\n");
  }
  // 没有 CREATE TABLE（比如只有 INSERT）：给模型看前若干行原始 SQL 作为兜底。
  return source.slice(0, 20_000);
}

/** json：能解析就格式化输出；看起来像知识网络草案（含 objectTypes/objects）时保留结构。 */
function extractJson(buffer: Buffer): string {
  const source = stripBom(buffer.toString("utf8"));
  try {
    const parsed: unknown = JSON.parse(source);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return source;
  }
}

/** html：去掉脚本、样式和标签，只留正文文字。 */
function extractHtml(buffer: Buffer): string {
  const source = stripBom(buffer.toString("utf8"));
  return source
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n");
}

/** 粗略数 PDF 页数（用于判断每页文字密度是否像扫描件）。 */
function countPdfPages(buffer: Buffer): number {
  const raw = buffer.toString("latin1");
  const matches = raw.match(/\/Type\s*\/Page[^s]/g);
  return matches ? matches.length : 0;
}

/** PDF 输入体检：无文字层/每页字数过少 → 判定扫描件或图片型 PDF。 */
export function inspectPdf(buffer: Buffer, text: string): ExtractHealth[] {
  const textLength = text.replace(/\s+/g, "").length;
  const pageCount = countPdfPages(buffer);
  if (textLength === 0) {
    return [
      {
        code: "pdf-no-text",
        message: "这个 PDF 没有可提取的文字层（大概率是扫描件）。请上传电子版 PDF，或先做 OCR 再上传。",
      },
    ];
  }
  if (pageCount > 0 && textLength / pageCount < 80) {
    return [
      {
        code: "pdf-likely-scanned",
        message: `这个 PDF 平均每页只有约 ${Math.round(textLength / pageCount)} 个字符，疑似扫描件或图片型 PDF，解析结果可能缺少正文。`,
      },
    ];
  }
  if (pageCount === 0 && textLength < 200) {
    return [
      {
        code: "pdf-short-text",
        message: "这个 PDF 提取到的正文很少，请确认它包含可复制的文字内容。",
      },
    ];
  }
  return [];
}

/** 把上传文件转成纯文本；不支持的类型抛出带操作提示的错误。 */
export async function extractDocument(fileName: string, buffer: Buffer): Promise<ExtractedDocument> {
  const extension = extname(fileName).toLowerCase();
  const hint = UNSUPPORTED_HINTS[extension];
  if (hint) {
    throw new Error(`${fileName}：${hint}`);
  }

  let text = "";
  if (extension === ".docx") {
    text = await extractDocx(buffer);
  } else if (extension === ".pdf") {
    text = await extractPdf(buffer);
  } else if (extension === ".xlsx" || extension === ".xls") {
    text = await extractSpreadsheet(buffer);
  } else if (extension === ".pptx") {
    text = extractPptx(buffer);
  } else if (extension === ".sql") {
    text = extractSql(buffer);
  } else if (extension === ".json") {
    text = extractJson(buffer);
  } else if (extension === ".html" || extension === ".htm") {
    text = extractHtml(buffer);
  } else {
    text = stripBom(buffer.toString("utf8"));
  }

  text = truncate(text);
  const health = extension === ".pdf" ? inspectPdf(buffer, text) : [];
  return {
    fileName,
    text,
    characters: text.length,
    ...(health.length > 0 ? { health } : {}),
  };
}

/** 表格类文件抽出的「表」信息：表名 + 表头 + 行数（不保留数据行）。 */
export type ExtractedTable = {
  name: string;
  headers: string[];
  rows: number;
  /**
   * 前若干行的样例值（按列对齐，仅用于推断字段类型/主键，不落盘）。
   * 长度 = headers.length；每行一个数组。
   */
  samples: string[][];
  /** 表头占用的行数（多层表头时为 2–3）。 */
  headerRowCount?: number;
  /** 表头体检结果（合并单元格/多层表头/空表头）。 */
  health?: ExtractHealth[];
};

const TABLE_EXTENSIONS = new Set([".xlsx", ".xls", ".csv", ".tsv"]);

const HEADER_ROW_LIMIT = 40;
const SAMPLE_ROWS = 40;
const MAX_HEADER_ROWS = 3;

type SheetMerge = { s: { r: number; c: number }; e: { r: number; c: number } };

function cellText(value: unknown): string {
  return String(value ?? "").trim();
}

function countFilled(row: unknown[] | undefined): number {
  if (!row) return 0;
  return row.filter((cell) => cellText(cell) !== "").length;
}

/** 识别表头占几行：首行大标题、次行分组名、第三行字段名时逐层识别。 */
function detectHeaderRowCount(matrix: unknown[][], merges: SheetMerge[] | undefined): number {
  const first = countFilled(matrix[0]);
  const second = matrix.length > 1 ? countFilled(matrix[1]) : 0;
  const third = matrix.length > 2 ? countFilled(matrix[2]) : 0;
  if (second < 2) return 1;
  const all = merges ?? [];
  const groupRowMerges = all.filter((merge) => merge.s.r === 1 && merge.e.r === 1 && merge.e.c > merge.s.c);
  if (groupRowMerges.length > 0 && third >= 2) return 3;
  const groupingMerges = all.filter((merge) => merge.s.r === 0 && merge.e.r === 0 && merge.e.c > merge.s.c);
  if (groupingMerges.length > 0) return 2;
  if (first <= 1 || second >= first * 2) {
    if (second <= 1 && third >= 2) return 3;
    return 2;
  }
  return 1;
}

type HeaderInfo = {
  headers: string[];
  /** headers 中每一列在原表里的列下标（缺表头的列会被过滤掉）。 */
  columns: number[];
  headerRowCount: number;
  health: ExtractHealth[];
};

function buildHeaderInfo(matrix: unknown[][], merges: SheetMerge[] | undefined, sheetName: string): HeaderInfo {
  const headerRowCount = Math.min(MAX_HEADER_ROWS, detectHeaderRowCount(matrix, merges));
  const grid: string[][] = [];
  for (let row = 0; row < headerRowCount; row += 1) {
    grid.push((matrix[row] ?? []).map(cellText));
  }
  const headerHealth: ExtractHealth[] = [];
  const headerMerges = (merges ?? []).filter((merge) => merge.s.r < headerRowCount);
  for (const merge of headerMerges) {
    const value = cellText((matrix[merge.s.r] ?? [])[merge.s.c]);
    if (!value) continue;
    for (let row = merge.s.r; row <= Math.min(merge.e.r, headerRowCount - 1); row += 1) {
      for (let column = merge.s.c; column <= merge.e.c; column += 1) {
        grid[row][column] = value;
      }
    }
  }
  if (headerMerges.length > 0) {
    headerHealth.push({
      code: "excel-merged-header",
      message: `工作表「${sheetName}」的表头有合并单元格，已按合并区域展开。`,
    });
  }
  if (headerRowCount > 1) {
    headerHealth.push({
      code: "excel-multirow-header",
      message: `工作表「${sheetName}」有多层表头，已合并成「一级/二级」形式的列名。`,
    });
  }

  const width = Math.min(HEADER_ROW_LIMIT, Math.max(0, ...grid.map((row) => row.length)));

  // 整行只有一个值（通常是"×××台账"这类大标题）→ 该行只当标题，不并入列名。
  const titleRows = new Set<number>();
  for (let row = 0; row < grid.length; row += 1) {
    const values = new Set(grid[row].slice(0, width).map((value) => value.trim()).filter(Boolean));
    if (values.size === 1 && headerRowCount > 1 && row < headerRowCount - 1) titleRows.add(row);
  }

  const headers: string[] = [];
  const columns: number[] = [];
  for (let column = 0; column < width; column += 1) {
    const labels: string[] = [];
    for (let row = 0; row < headerRowCount; row += 1) {
      if (titleRows.has(row)) continue;
      const label = (grid[row][column] ?? "").trim();
      if (label && labels[labels.length - 1] !== label) labels.push(label);
    }
    if (labels.length === 0) continue;
    headers.push(labels.join("/"));
    columns.push(column);
  }

  const firstDataRow = matrix[headerRowCount] ?? [];
  const emptyHeaderCount = firstDataRow.filter(
    (cell, index) => cellText(cell) !== "" && !columns.includes(index),
  ).length;
  if (emptyHeaderCount > 0) {
    headerHealth.push({
      code: "excel-empty-header",
      message: `工作表「${sheetName}」有 ${emptyHeaderCount} 列没有表头，这些列会被忽略。`,
    });
  }
  return { headers, columns, headerRowCount, health: headerHealth };
}

export type ExtractedRecords = {
  name: string;
  headers: string[];
  rows: number;
  records: Array<Record<string, unknown>>;
  health?: ExtractHealth[];
};

/**
 * 抽出表格类文件的全部数据行（用于「导入数据」按本体写入）。
 * 只取第一张有数据的 sheet；非表格文件返回空数组。
 */
export async function extractRecords(
  fileName: string,
  buffer: Buffer,
  maxRows = 20000,
): Promise<ExtractedRecords[]> {
  const extension = extname(fileName).toLowerCase();
  if (!TABLE_EXTENSIONS.has(extension)) return [];

  const { read, utils } = await import("xlsx");
  const workbook = read(buffer, { type: "buffer" });
  const result: ExtractedRecords[] = [];

  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    const matrix = utils.sheet_to_json<unknown[]>(sheet, { header: 1, blankrows: false, defval: "" });
    if (matrix.length === 0) continue;
    const merged = (sheet as unknown as { "!merges"?: SheetMerge[] })["!merges"];
    const info = buildHeaderInfo(matrix, merged, sheetName);
    if (info.headers.length === 0) continue;

    const records: Array<Record<string, unknown>> = [];
    for (let index = info.headerRowCount; index < matrix.length && records.length < maxRows; index += 1) {
      const row = matrix[index] ?? [];
      if (row.every((cell) => cellText(cell) === "")) continue;
      const record: Record<string, unknown> = {};
      info.headers.forEach((header, position) => {
        const cell = row[info.columns[position] ?? position];
        record[header] = cell === undefined || cell === null ? "" : cell;
      });
      records.push(record);
    }
    if (records.length === 0) continue;
    result.push({
      name: sheetName,
      headers: info.headers,
      rows: records.length,
      records,
      ...(info.health.length > 0 ? { health: info.health } : {}),
    });
  }

  return result;
}

/**
 * 抽出表格类文件的表结构，供数据绑定推断使用。
 * 非表格文件返回空数组。
 */
export async function extractTables(fileName: string, buffer: Buffer): Promise<ExtractedTable[]> {
  const extension = extname(fileName).toLowerCase();
  if (!TABLE_EXTENSIONS.has(extension)) return [];

  const { read, utils } = await import("xlsx");
  const workbook = read(buffer, { type: "buffer" });
  const tables: ExtractedTable[] = [];

  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    const matrix = utils.sheet_to_json<unknown[]>(sheet, { header: 1, blankrows: false, defval: "" });
    if (matrix.length === 0) continue;

    const merged = (sheet as unknown as { "!merges"?: SheetMerge[] })["!merges"];
    const info = buildHeaderInfo(matrix, merged, sheetName);
    if (info.headers.length === 0) continue;

    const dataRows = matrix.slice(info.headerRowCount, info.headerRowCount + SAMPLE_ROWS);
    const samples = dataRows.map((row) => info.columns.map((column) => cellText((row ?? [])[column])));
    tables.push({
      name: sheetName,
      headers: info.headers,
      rows: Math.max(0, matrix.length - info.headerRowCount),
      samples,
      headerRowCount: info.headerRowCount,
      ...(info.health.length > 0 ? { health: info.health } : {}),
    });
  }

  return tables;
}