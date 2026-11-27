import type { IrRelation } from "./types/ir.js";

/**
 * 关系去重与同义归一。
 *
 * 模型会把同一对对象之间的关系换个动词重复输出（如「发生故障」「出现故障」，
 * 或同一对对象两个方向都写一遍），这些重复项会让人工核对成本翻倍。
 * 这里用确定性的规则做去重：同一对端点 + 同一动词组，只保留第一条。
 */

/** 同义动词组；每组第一个作为展示/归并基准。 */
const VERB_GROUPS: string[][] = [
  ["生成", "触发", "产生", "导致", "引起", "生成出"],
  ["消耗", "使用", "领用", "占用", "使用掉"],
  ["指派", "派给", "分配", "委派", "指定"],
  ["属于", "归属", "隶属", "归入"],
  ["包含", "含有", "包括", "组成", "构成"],
  ["下单", "下达订单", "提交订单", "订货", "订购"],
  ["处理", "办理", "处置", "应对"],
  ["供应", "提供", "供货"],
  ["关联", "相关", "涉及", "对应"],
  ["发生", "出现", "出现于"],
  ["部署", "部署于", "布置于", "驻扎于"],
];

/** 去掉空白/标点，便于比较中文短语。 */
const normalize = (text: string): string =>
  text
    .toLowerCase()
    .replace(/[\s\u3000]+/g, "")
    .replace(/[，。；：、,.;:()（）【】[\]"'“”‘’!?！？/\\|·—–_-]+/g, "");

type VerbMatch = {
  groupIndex: number;
  /** 动词之后的宾语部分（如「工单」）。 */
  rest: string;
};

/** 找出关系名开头的同义动词，返回其分组与剩余部分。 */
const matchVerb = (name: string): VerbMatch | undefined => {
  const normalized = normalize(name);
  for (let index = 0; index < VERB_GROUPS.length; index += 1) {
    for (const verb of VERB_GROUPS[index]) {
      if (normalized.startsWith(verb)) {
        return { groupIndex: index, rest: normalized.slice(verb.length) };
      }
    }
  }
  return undefined;
};

/** 关系去重键：端点相同 → 同义动词视为同一关系。 */
const relationKey = (relation: IrRelation): string => {
  const verb = matchVerb(relation.name);
  const suffix = verb ? `verb:${verb.groupIndex}:${verb.rest}` : `name:${normalize(relation.name)}`;
  return `${relation.source}\u0000${relation.target}\u0000${suffix}`;
};

export type DedupeRelationsResult = {
  relations: IrRelation[];
  /** 被合并掉的关系条数。 */
  removed: number;
  /** 被合并掉的同义关系名，用于提示。 */
  mergedNames: string[];
};

/**
 * 去掉同一对端点上的重复关系。
 * - 端点相同 + 动词同组（或名称相同）→ 视为重复；
 * - 保留最早出现的一条，不改名字；
 * - 自环关系（source === target）保留（可能是合法的层级关系），但同样去重。
 */
export function dedupeRelations(relations: IrRelation[]): DedupeRelationsResult {
  const seen = new Set<string>();
  const kept: IrRelation[] = [];
  const mergedNames: string[] = [];

  for (const relation of relations) {
    const key = relationKey(relation);
    if (seen.has(key)) {
      mergedNames.push(relation.name);
      continue;
    }
    seen.add(key);
    kept.push(relation);
  }

  return { relations: kept, removed: relations.length - kept.length, mergedNames };
}

/** 生成给前端展示的去重提示文案；没有重复时返回 undefined。 */
export function dedupeWarning(result: DedupeRelationsResult): string | undefined {
  if (result.removed === 0) {
    return undefined;
  }
  const names = [...new Set(result.mergedNames)].slice(0, 5).join("、");
  const suffix = names ? `（${names}${result.mergedNames.length > 5 ? " 等" : ""}）` : "";
  return `已合并 ${result.removed} 条重复或同义关系${suffix}`;
}
