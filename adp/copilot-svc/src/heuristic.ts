import type { OntologyIr } from "./types/ir.js";

const GENERIC_HEADINGS = new Set([
  "目录",
  "引言",
  "前言",
  "概述",
  "简介",
  "背景",
  "目标",
  "结论",
  "总结",
  "附录",
  "参考",
  "参考文献",
  "目录结构",
]);

const NUMBER_PREFIX = /^(\d+(\.\d+)*[.、)]?\s*|[一二三四五六七八九十百]+[、.)]\s*)/;
const HEADING = /^(#{1,4})\s+(.+)$/;
const SECTION_LINE = /^(\d+(\.\d+)*[.、)]?\s*|[一二三四五六七八九十百]+[、.)]\s*)(\S.{1,24})$/;

const clean = (raw: string): string =>
  raw
    .replace(/[·•]\s*/g, "")
    .replace(/[:：]\s*$/, "")
    .replace(/\s+/g, " ")
    .trim();

const isCandidateName = (raw: string): boolean => {
  const name = clean(raw);
  if (name.length < 2 || name.length > 24) {
    return false;
  }
  if (GENERIC_HEADINGS.has(name)) {
    return false;
  }
  if (/[。；，、,;:：]$/.test(name)) {
    return false;
  }
  return true;
};

const stripNumber = (raw: string): string => clean(raw.replace(NUMBER_PREFIX, ""));

/** 从纯文本里挑出可能的对象名（标题、编号小节）。 */
export function extractCandidateNames(text: string): string[] {
  const names: string[] = [];
  const titles: string[] = [];
  const seen = new Set<string>();

  const push = (raw: string) => {
    const name = stripNumber(raw);
    if (!isCandidateName(name) || seen.has(name)) {
      return;
    }
    seen.add(name);
    names.push(name);
  };

  for (const line of text.split(/\r?\n/)) {
    const heading = line.match(HEADING);
    if (heading) {
      // 一级标题通常是文档标题（往往等于知识网络名），不作为对象类型；
      // 但如果全文只有一级标题，则退回使用它们，避免抽不出任何对象。
      if (heading[1].length === 1) {
        const name = stripNumber(heading[2]);
        if (isCandidateName(name) && !seen.has(name)) {
          seen.add(name);
          titles.push(name);
        }
        continue;
      }
      push(heading[2]);
      continue;
    }
    const section = line.match(SECTION_LINE);
    if (section) {
      push(section[3]);
    }
  }

  const chosen = names.length >= 2 ? names : [...names, ...titles.filter((title) => !seen.has(title))];
  return chosen.slice(0, 16);
}

const RELATION_PATTERNS: { regex: RegExp; reverse?: boolean }[] = [
  { regex: /包含(?<b>\S{2,12})$/ },
  { regex: /所属(?<b>\S{2,12})$/ },
  { regex: /关联(?<b>\S{2,12})$/ },
  { regex: /对应(?<b>\S{2,12})$/ },
  { regex: /产生(?<b>\S{2,12})$/ },
];

const RELATION_VERBS = ["包含", "属于", "关联", "对应", "产生", "触发", "分配给", "管理", "引用"];

const slug = (raw: string, fallback: string): string => {
  const value = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[a-z][a-z0-9_]*$/.test(value) ? value : fallback;
};

/**
 * 无 LLM 时的兜底抽取：按标题/编号小节生成对象类型，
 * 按「X包含Y」等中文短语生成关系。只用于跑通流程，质量有限。
 */
export function heuristicExtract(text: string, hint?: string): OntologyIr {
  const names = extractCandidateNames(text);
  const chosen = names.length > 0 ? names : ["业务对象"];

  const objectTypes = chosen.map((name, index) => ({
    id: slug(name, `obj_${index + 1}`),
    name,
    description: `${name}（自动抽取，待人工确认）。`,
    properties: [
      { name: "id", displayName: "ID", type: "string" as const, description: "主键ID" },
      { name: "name", displayName: "名称", type: "string" as const, description: `${name}名称` },
      { name: "description", displayName: "描述", type: "string" as const, description: "补充说明" },
    ],
    primaryKey: "id",
    displayKey: "name",
  }));

  const byName = new Map(objectTypes.map((object) => [object.name, object]));
  const relations: { id: string; name: string; source: string; target: string; description?: string }[] = [];
  const relationIds = new Set<string>();

  const pushRelation = (source: (typeof objectTypes)[number], target: (typeof objectTypes)[number], label: string) => {
    if (source.id === target.id) {
      return;
    }
    const id = slug(`${source.id}_${target.id}`, `rel_${relations.length + 1}`);
    if (relationIds.has(id)) {
      return;
    }
    relationIds.add(id);
    relations.push({
      id,
      name: label,
      source: source.id,
      target: target.id,
      description: "由文档短语推断，待人工确认。",
    });
  };

  // 1) 对象名自身带关系短语，例如标题「设备包含部件」
  for (const object of objectTypes) {
    for (const pattern of RELATION_PATTERNS) {
      const match = object.name.match(pattern.regex);
      const targetName = match?.groups?.b;
      const target = targetName ? byName.get(targetName) : undefined;
      if (!target) {
        continue;
      }
      pushRelation(object, target, `${object.name}`);
    }
  }

  // 2) 正文里的「A 包含 B」这类短语，A/B 都是已识别对象
  for (const source of objectTypes) {
    for (const target of objectTypes) {
      if (source.id === target.id) {
        continue;
      }
      for (const verb of RELATION_VERBS) {
        if (text.includes(`${source.name}${verb}${target.name}`)) {
          pushRelation(source, target, `${source.name}${verb}${target.name}`);
          break;
        }
      }
    }
  }

  return {
    networkId: slug("generated_network", "kn_generated"),
    networkName: hint?.trim() || "自动生成的知识网络",
    description: "由本体构建助手（规则模式）从文档中抽取。",
    objectTypes,
    relations,
    conceptGroups: [],
  };
}
