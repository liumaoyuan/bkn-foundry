import type { IrEvidence, OntologyIr } from "./types/ir.js";

/**
 * 证据引用：在输入材料里为对象/属性/关系/行动/风险找到出处。
 *
 * 设计原则：不做"让模型自己说明依据"（容易幻觉），而是生成草案后由服务端做
 * 确定性检索——拿名称/别名去源文本里找，命中即记录文件名、位置与原文片段。
 */

export type EvidenceSource = {
  /** 展示给用户的来源名：文件名，或「粘贴文本」。 */
  fileName: string;
  /** 更精确的位置（表格输入用工作表名）。 */
  location?: string;
  /** 可用于检索的纯文本。 */
  text: string;
};

export type EvidenceStats = {
  objects: number;
  objectsWithEvidence: number;
  properties: number;
  propertiesWithEvidence: number;
  relations: number;
  relationsWithEvidence: number;
  actions: number;
  actionsWithEvidence: number;
  risks: number;
  risksWithEvidence: number;
};

const QUOTE_CONTEXT = 36;
const MAX_QUOTE = 120;

/** 归一化：去空白/标点并转小写，避免「设备编号」与「设备 编号」匹配不上。 */
const normalize = (text: string): string =>
  text
    .toLowerCase()
    .replace(/[\s\u3000]+/g, "")
    .replace(/[，。；：、,.;:()（）【】[\]"'“”‘’!?！？/\\|·—–_-]+/g, "");

const collapse = (text: string): string =>
  text
    .replace(/[\s\u3000]+/g, " ")
    // 去掉正文里的 Markdown 标题残留（如「## 工作表：」），让引用更接近原文。
    .replace(/(^|\s)#+\s*/g, "$1")
    .trim();

const clip = (text: string): string =>
  text.length > MAX_QUOTE ? `${text.slice(0, MAX_QUOTE)}…` : text;

/**
 * 在一段源文本里查找词条，返回命中的上下文片段。
 * 使用归一化后的文本定位，再用相同偏移回原文本取片段（归一化不改变中文序，但仍做保守截断）。
 */
const findQuote = (source: EvidenceSource, term: string): IrEvidence | undefined => {
  const keyword = normalize(term);
  if (keyword.length < 2) {
    return undefined;
  }
  const normalizedText = normalize(source.text);
  const index = normalizedText.indexOf(keyword);
  if (index < 0) {
    return undefined;
  }
  // 归一化会删除字符，这里按「长度比例」粗略回推原文位置，够用且不误导。
  const ratio = source.text.length / Math.max(normalizedText.length, 1);
  const center = Math.floor(index * ratio);
  const start = Math.max(0, center - QUOTE_CONTEXT);
  const end = Math.min(source.text.length, center + keyword.length * 2 + QUOTE_CONTEXT);
  return {
    source: source.fileName,
    ...(source.location ? { location: source.location } : {}),
    quote: clip(collapse(source.text.slice(start, end))),
  };
};

/** 纯虚词/业务噪声片段，不作为检索关键词。 */
const STOPWORD_PIECE =
  /^(的|风险|管控|管理|策略|规则|要求|需要|建议|以及|并且|或者|进行|相关|对应|未|不|需|和|与|及)+$/;

/** 过于通用的词片段：命中了也不能证明该风险有原文出处，直接忽略。 */
const GENERIC_PIECES = new Set([
  "处理",
  "管理",
  "记录",
  "系统",
  "数据",
  "信息",
  "情况",
  "问题",
  "工作",
  "业务",
  "设备",
  "工单",
  "备件",
  "告警",
  "人员",
  "库存",
  "时间",
]);

/**
 * 把中文短语拆成 4–8 字的关键片段，长片段优先。
 * 用于「风险名/管控策略」这类原文中很少逐字出现的抽象描述。
 */
const keywordWindows = (term: string | undefined, limit = 20): string[] => {
  if (!term) {
    return [];
  }
  const normalized = normalize(term);
  if (normalized.length < 4) {
    return [];
  }
  const pieces: string[] = [];
  const seen = new Set<string>();
  const maxLength = Math.min(8, normalized.length);
  for (let length = maxLength; length >= 4; length -= 1) {
    for (let start = 0; start + length <= normalized.length; start += 1) {
      if (pieces.length >= limit) {
        return pieces;
      }
      const piece = normalized.slice(start, start + length);
      if (
        !/^[\u4e00-\u9fa5a-z0-9]+$/.test(piece) ||
        STOPWORD_PIECE.test(piece) ||
        GENERIC_PIECES.has(piece) ||
        seen.has(piece)
      ) {
        continue;
      }
      seen.add(piece);
      pieces.push(piece);
    }
  }
  return pieces;
};

const collect = (
  sources: EvidenceSource[],
  terms: Array<string | undefined>,
): IrEvidence[] => {  const found: IrEvidence[] = [];
  const seen = new Set<string>();
  for (const term of terms) {
    if (!term || term.trim().length < 2) {
      continue;
    }
    for (const source of sources) {
      const evidence = findQuote(source, term);
      if (!evidence) {
        continue;
      }
      const key = `${evidence.source}\u0000${evidence.location ?? ""}\u0000${evidence.quote}`;
      if (!seen.has(key)) {
        seen.add(key);
        found.push(evidence);
      }
      // 每个词条只取第一条命中的证据，避免列表过长；不同词条会带来不同来源。
      break;
    }
  }
  return found;
};

/** 给整份 IR 附加证据；返回新的 IR 与覆盖率统计（不修改入参对象）。 */
export function attachEvidence(
  ir: OntologyIr,
  sources: EvidenceSource[],
): { ir: OntologyIr; stats: EvidenceStats } {
  const usable = sources.filter((source) => source.text.trim().length > 0);
  const stats: EvidenceStats = {
    objects: 0,
    objectsWithEvidence: 0,
    properties: 0,
    propertiesWithEvidence: 0,
    relations: 0,
    relationsWithEvidence: 0,
    actions: 0,
    actionsWithEvidence: 0,
    risks: 0,
    risksWithEvidence: 0,
  };

  const objectNameById = new Map(ir.objectTypes.map((object) => [object.id, object.name]));

  const objectTypes = ir.objectTypes.map((object) => {
    stats.objects += 1;
    const properties = object.properties.map((property) => {
      stats.properties += 1;
      const evidence = collect(usable, [property.displayName, property.mappedField]);
      if (evidence.length > 0) {
        stats.propertiesWithEvidence += 1;
        return { ...property, evidence };
      }
      return property;
    });
    const evidence = collect(usable, [object.name, object.sourceTable]);
    if (evidence.length > 0) {
      stats.objectsWithEvidence += 1;
      return { ...object, properties, evidence };
    }
    return { ...object, properties };
  });

  const relations = ir.relations.map((relation) => {
    stats.relations += 1;
    const evidence = collect(usable, [
      relation.name,
      objectNameById.get(relation.source),
      objectNameById.get(relation.target),
    ]);
    if (evidence.length > 0) {
      stats.relationsWithEvidence += 1;
      return { ...relation, evidence };
    }
    return relation;
  });

  const actionTypes = (ir.actionTypes ?? []).map((action) => {
    stats.actions += 1;
    // 行动名常被模型改写，名称匹配不到时退化为「绑定对象」的出处作为佐证。
    const evidence = collect(usable, [action.name, objectNameById.get(action.boundObject)]);
    if (evidence.length > 0) {
      stats.actionsWithEvidence += 1;
      return { ...action, evidence };
    }
    return action;
  });

  const riskTypes = (ir.riskTypes ?? []).map((risk) => {
    stats.risks += 1;
    // 风险名通常是「工单超期未关闭」这类抽象短语，原文里很少逐字出现，
    // 因此除了整名匹配，再拆成 3–8 字的关键片段做模糊匹配（长片段优先）。
    const evidence = collect(usable, [
      risk.name,
      ...keywordWindows(risk.name),
      ...risk.policies.slice(0, 2),
      ...risk.policies.slice(0, 2).flatMap((policy) => keywordWindows(policy)),
    ]);
    if (evidence.length > 0) {
      stats.risksWithEvidence += 1;
      return { ...risk, evidence };
    }
    return risk;
  });

  return {
    ir: { ...ir, objectTypes, relations, actionTypes, riskTypes },
    stats,
  };
}
