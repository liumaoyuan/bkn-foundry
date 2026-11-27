/**
 * 索引建议：根据本体草案推断每个对象需要建哪些检索索引字段。
 *
 * 平台事实（0.1.5 实测）：
 * - 索引只能建在 category=table 的资源上（dataset 会报 "Resource category must be table"）。
 * - 建索引前必须先在资源的 index_config 里配置 primary_key_fields，否则 build task 报
 *   "build task requires at least one primary_key_fields entry"。
 * - 全文索引靠 schema_definition[].features 里的 fulltext 特征；向量索引靠 vector 特征，
 *   且必须在平台的「模型管理 → 小模型」里先配置 embedding 模型。
 */

import type { IrObjectType, IrProperty, OntologyIr } from "./types/ir.js";

export type IndexSuggestion = {
  objectId: string;
  objectName: string;
  /** 对象绑定的资源名（对齐视图优先，其次来源表）。 */
  resourceName?: string;
  /** 资源 index_config.primary_key_fields 的取值（物理列名）。 */
  primaryKeyField?: string;
  /** 建议建全文索引的字段（物理列名）。 */
  fulltextFields: string[];
  /** 建议建向量索引的字段（物理列名，目前仅作建议，需平台 embedding 模型）。 */
  vectorFields: string[];
  /** 平台建索引任务要求的增量字段（日期/时间或整数列，物理列名）。 */
  incrementalField?: string;
};

export type IndexPlan = {
  suggestions: IndexSuggestion[];
  /** 汇总：对象数、全文字段数、向量字段数。 */
  summary: { objects: number; fulltextFields: number; vectorFields: number };
  /** 没有来源表、无法建索引的对象。 */
  warnings: string[];
};

/** 名称像"标题/名称/描述"的字段适合全文检索。 */
const FULLTEXT_NAME_PATTERN =
  /(name|title|label|desc|description|comment|remark|note|summary|content|phenomenon|reason|名称|名|标题|描述|说明|备注|现象|原因|内容|摘要|详情)/i;

/** 长文本类字段更适合向量检索（语义相似）。 */
const VECTOR_NAME_PATTERN =
  /(desc|description|content|summary|remark|detail|note|描述|说明|内容|摘要|详情|备注)/i;

const isLongTextProperty = (property: IrProperty): boolean => {
  if (property.type !== "string") {
    return false;
  }
  return VECTOR_NAME_PATTERN.test(property.displayName ?? property.name);
};

const isFulltextProperty = (property: IrProperty): boolean => {
  if (property.type !== "string") {
    return false;
  }
  const label = property.displayName || property.name;
  return FULLTEXT_NAME_PATTERN.test(label) || FULLTEXT_NAME_PATTERN.test(property.name);
};

/**
 * 生成索引建议。只处理有来源表（sourceTable）的对象；字段取 mappedField，缺失时退回属性名。
 */
export function buildIndexPlan(ir: OntologyIr): IndexPlan {
  const suggestions: IndexSuggestion[] = [];
  const warnings: string[] = [];
  let fulltextTotal = 0;
  let vectorTotal = 0;

  for (const objectType of ir.objectTypes) {
    if (!objectType.sourceTable) {
      warnings.push(`对象「${objectType.name}」没有来源表，跳过索引建议`);
      continue;
    }
    const columnOf = (property: IrProperty): string => property.mappedField ?? property.name;
    const primaryKeyProperty =
      objectType.properties.find((property) => property.name === objectType.primaryKey) ??
      objectType.properties[0];
    const primaryKeyField = primaryKeyProperty ? columnOf(primaryKeyProperty) : undefined;
    const fulltextFields = uniqueFields(
      objectType.properties.filter(isFulltextProperty).map(columnOf),
    );
    const vectorFields = uniqueFields(
      objectType.properties.filter(isLongTextProperty).map(columnOf),
    );
    fulltextTotal += fulltextFields.length;
    vectorTotal += vectorFields.length;
    // 平台建索引任务强制要求 incremental_fields（日期/时间或整数列），否则 400。
    const incrementalProperty =
      objectType.properties.find((property) => property.type === "datetime") ??
      objectType.properties.find((property) => property.type === "integer");
    const incrementalField = incrementalProperty ? columnOf(incrementalProperty) : undefined;
    suggestions.push({
      objectId: objectType.id,
      objectName: objectType.name,
      resourceName: objectType.sourceTable,
      ...(primaryKeyField ? { primaryKeyField } : {}),
      ...(incrementalField ? { incrementalField } : {}),
      fulltextFields,
      vectorFields,
    });
  }

  return {
    suggestions,
    summary: {
      objects: suggestions.length,
      fulltextFields: fulltextTotal,
      vectorFields: vectorTotal,
    },
    warnings,
  };
}

function uniqueFields(fields: string[]): string[] {
  return Array.from(new Set(fields.filter((field) => field.trim().length > 0)));
}
