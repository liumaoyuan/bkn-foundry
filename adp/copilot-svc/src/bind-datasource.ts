import type { ExtractedTable } from "./extract.js";
import type { IrObjectType, IrProperty, IrPropertyType, IrRelation, OntologyIr } from "./types/ir.js";

export type BindingStats = {
  tables: number;
  objects: number;
  boundObjects: number;
  properties: number;
  mappedProperties: number;
  /** 成功推断出主键的对象数。 */
  inferredPrimaryKeys: number;
  /** 成功推断出键映射的关系数。 */
  inferredRelations: number;
};

export type BindingObjectDetail = {
  objectId: string;
  objectName: string;
  table: string;
  primaryKey?: { property: string; column: string };
  properties: Array<{ property: string; column: string; type: IrPropertyType }>;
};

export type BindingRelationDetail = {
  relationId: string;
  relationName: string;
  sourceTable?: string;
  targetTable?: string;
  mapping: Array<{ sourceProperty: string; targetProperty: string }>;
};

export type BindingDetails = {
  objects: BindingObjectDetail[];
  relations: BindingRelationDetail[];
};

export type BindingResult = {
  ir: OntologyIr;
  stats: BindingStats;
  details: BindingDetails;
  warnings: string[];
};

/** 表名/字段名里常见的修饰词，匹配前先去掉。 */
const NOISE_WORDS = ["数据表", "数据", "信息", "清单", "列表", "台账", "记录", "明细", "汇总", "表"];

const PK_NAME_PATTERN = /(^|_)(id|code|no|key)$/i;
const PK_CJK_PATTERN = /(编号|编码|代码|序号|号码)$/;

const normalize = (raw: string): string => {
  let value = raw
    .toLowerCase()
    .replace(/[\s_\-]+/g, "")
    .replace(/[()[\]【】（）"'"'，。；：、]/g, "");
  for (const word of NOISE_WORDS) {
    value = value.split(word).join("");
  }
  return value;
};

const isCjk = (text: string): boolean => /[\u4e00-\u9fa5]/.test(text);

const nameMatches = (left: string, right: string): boolean => {
  const a = normalize(left);
  const b = normalize(right);
  if (!a || !b) return false;
  if (a === b) return true;
  if (a.length < 2 || b.length < 2) return false;
  return a.includes(b) || b.includes(a);
};

const propertyNames = (property: IrProperty): string[] =>
  [property.displayName, property.name].filter((name): name is string => Boolean(name && name.trim()));

const tableNameScore = (object: IrObjectType, table: ExtractedTable): number => {
  const objectNorm = normalize(object.name);
  const tableNorm = normalize(table.name);
  if (!objectNorm || !tableNorm) return 0;
  if (objectNorm === tableNorm) return 3;
  if (objectNorm.includes(tableNorm) || tableNorm.includes(objectNorm)) return 2;
  return 0;
};

const columnOverlap = (object: IrObjectType, table: ExtractedTable): number => {
  if (object.properties.length === 0 || table.headers.length === 0) return 0;
  const matched = object.properties.filter((property) =>
    table.headers.some((header) => propertyNames(property).some((name) => nameMatches(name, header))),
  );
  return matched.length / object.properties.length;
};

const pickTable = (object: IrObjectType, tables: ExtractedTable[]): ExtractedTable | null => {
  let best: { table: ExtractedTable; score: number } | null = null;
  for (const table of tables) {
    const nameScore = tableNameScore(object, table);
    const overlap = columnOverlap(object, table);
    // 表名匹配优先；只靠字段重合时必须重合一半以上，避免「借一个同名列」误绑到不相关的表。
    if (nameScore === 0 && overlap < 0.5) continue;
    const score = nameScore * 3 + overlap * 2;
    if (!best || score > best.score) {
      best = { table, score };
    }
  }
  return best?.table ?? null;
};

const pickColumn = (property: IrProperty, table: ExtractedTable): string | undefined => {
  for (const header of table.headers) {
    if (propertyNames(property).some((name) => nameMatches(name, header))) {
      return header;
    }
  }
  return undefined;
};

/** 单列表的样例值。 */
const columnValues = (table: ExtractedTable, column: string): string[] => {
  const index = table.headers.indexOf(column);
  if (index < 0) return [];
  return table.samples.map((row) => row[index] ?? "").filter((value) => value !== "");
};

const DATE_PATTERN = /^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$/;
const INT_PATTERN = /^-?\d+$/;
const FLOAT_PATTERN = /^-?\d+(\.\d+)?$/;
const BOOL_VALUES = new Set(["true", "false", "是", "否", "y", "n", "yes", "no"]);

/** 按样例值推断字段类型；没有样例时返回 undefined。 */
export function inferColumnType(values: string[]): IrPropertyType | undefined {
  const sample = values.filter((value) => value !== "").slice(0, 50);
  if (sample.length === 0) return undefined;
  if (sample.every((value) => INT_PATTERN.test(value))) return "integer";
  if (sample.every((value) => FLOAT_PATTERN.test(value))) return "number";
  if (sample.every((value) => BOOL_VALUES.has(value.toLowerCase()))) return "boolean";
  if (sample.every((value) => DATE_PATTERN.test(value))) return "datetime";
  return "string";
}

/** 值在样例里是否唯一（用于判定主键）。 */
const isUniqueColumn = (table: ExtractedTable, column: string): boolean => {
  const values = columnValues(table, column);
  if (values.length < 2) return false;
  return new Set(values).size === values.length;
};

/**
 * 推断表的主键列：
 * 1. 优先「<表名单数>_id」这类列；
 * 2. 其次 `*_id` / `*_code` / `*编号` 等且样例唯一；
 * 3. 再退回任意样例唯一的列；
 * 4. 都没有则返回 undefined。
 */
export function inferPrimaryKey(table: ExtractedTable): string | undefined {
  if (table.headers.length === 0) return undefined;
  const tableNorm = normalize(table.name);
  const scored = table.headers.map((header) => {
    const headerNorm = normalize(header);
    let score = 0;
    if (tableNorm && headerNorm.startsWith(tableNorm) && PK_NAME_PATTERN.test(header)) score += 4;
    if (PK_NAME_PATTERN.test(header) || PK_CJK_PATTERN.test(header)) score += 3;
    if (isUniqueColumn(table, header)) score += 2;
    return { header, score };
  });
  scored.sort((a, b) => b.score - a.score);
  const best = scored.find((item) => item.score >= 2);
  return best?.header;
}

/** 子表列的值有多大比例落在父表主键值里（用于识别没按命名规范命名的外键）。 */
const valueOverlap = (childValues: string[], parentValues: string[]): number => {
  if (childValues.length === 0 || parentValues.length === 0) return 0;
  const parentSet = new Set(parentValues);
  const hit = childValues.filter((value) => parentSet.has(value)).length;
  return hit / childValues.length;
};

export const foreignKeyTargets = (
  table: ExtractedTable,
  tables: ExtractedTable[],
  primaryKeys: Map<string, string | undefined>,
): Array<{ column: string; parentTable: ExtractedTable; parentColumn: string }> => {
  const result: Array<{ column: string; parentTable: ExtractedTable; parentColumn: string }> = [];
  for (const header of table.headers) {
    if (!(PK_NAME_PATTERN.test(header) || PK_CJK_PATTERN.test(header))) continue;
    if (header === primaryKeys.get(table.name)) continue;
    const childValues = columnValues(table, header);
    for (const parent of tables) {
      if (parent.name === table.name) continue;
      const parentPk = primaryKeys.get(parent.name);
      if (!parentPk) continue;
      const parentNorm = normalize(parent.name);
      const headerNorm = normalize(header);
      const nameMatched =
        headerNorm === normalize(parentPk) ||
        (parentNorm.length >= 2 && headerNorm.includes(parentNorm)) ||
        (parentNorm.length >= 2 && parentNorm.includes(headerNorm.replace(/(id|code|no|编号|编码|代码)$/i, "")));
      const valueMatched = valueOverlap(childValues, columnValues(parent, parentPk)) >= 0.6;
      if (nameMatched || valueMatched) {
        result.push({ column: header, parentTable: parent, parentColumn: parentPk });
        break;
      }
    }
  }
  return result;
};

const propertyForColumn = (object: IrObjectType, column: string): IrProperty | undefined =>
  object.properties.find((property) => property.mappedField === column || // 已绑定
    propertyNames(property).some((name) => nameMatches(name, column)));

/** 列名转属性名：能转成 ASCII slug 就返回它；中文等无法转的返回空串，由调用方决定兜底名。 */
const columnToPropertyName = (column: string): string =>
  column
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");

/** 在对象里取一个不冲突的属性名。 */
const uniquePropertyName = (object: IrObjectType, base: string): string => {
  const taken = new Set(object.properties.map((property) => property.name));
  if (!taken.has(base)) return base;
  let index = 2;
  while (taken.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
};

/**
 * 给 IR 的每个对象类型推断「来源表」「映射字段」「主键」，并给关系推断键映射。
 * 纯规则实现：表名/字段名/样例值匹配，不调用模型，保证可重复。
 */
export function inferIrBindings(ir: OntologyIr, tables: ExtractedTable[]): BindingResult {
  const usable = tables.filter((table) => table.headers.length > 0);
  const emptyStats: BindingStats = {
    tables: tables.length,
    objects: ir.objectTypes.length,
    boundObjects: 0,
    properties: ir.objectTypes.reduce((sum, object) => sum + object.properties.length, 0),
    mappedProperties: 0,
    inferredPrimaryKeys: 0,
    inferredRelations: 0,
  };
  if (usable.length === 0) {
    return {
      ir,
      stats: emptyStats,
      details: { objects: [], relations: [] },
      warnings: tables.length > 0 ? ["上传的表格没有表头，无法推断数据绑定"] : [],
    };
  }

  const primaryKeys = new Map<string, string | undefined>();
  for (const table of usable) {
    primaryKeys.set(table.name, inferPrimaryKey(table));
  }

  let boundObjects = 0;
  let mappedProperties = 0;
  let properties = 0;
  let inferredPrimaryKeys = 0;
  const warnings: string[] = [];
  const objectDetails: BindingObjectDetail[] = [];

  const objectTypes = ir.objectTypes.map((object) => {
    properties += object.properties.length;
    const table = pickTable(object, usable);
    if (!table) {
      return object;
    }
    const tablePk = primaryKeys.get(table.name);
    const detail: BindingObjectDetail = {
      objectId: object.id,
      objectName: object.name,
      table: table.name,
      properties: [],
    };
    const nextProperties: IrProperty[] = [];
    for (const property of object.properties) {
      const column = pickColumn(property, table);
      if (!column) {
        nextProperties.push(property);
        continue;
      }
      mappedProperties += 1;
      const inferred = inferColumnType(columnValues(table, column));
      const nextType = property.type === "string" && inferred ? inferred : property.type;
      const nextProperty = { ...property, mappedField: column, type: nextType };
      nextProperties.push(nextProperty);
      detail.properties.push({ property: nextProperty.name, column, type: nextType });
    }

    let primaryKey = object.primaryKey;
    if (tablePk) {
      const pkProperty = nextProperties.find((property) => property.mappedField === tablePk);
      if (pkProperty) {
        primaryKey = pkProperty.name;
        inferredPrimaryKeys += 1;
        detail.primaryKey = { property: pkProperty.name, column: tablePk };
      } else {
        // 主键列没有被模型映射到属性上：补一个属性，避免导入后无法绑定。
        const inferred = inferColumnType(columnValues(table, tablePk));
        const name = columnToPropertyName(tablePk) || "id";
        const exists = nextProperties.some((property) => property.name === name);
        if (!exists) {
          const pkPropertyNew: IrProperty = {
            name,
            displayName: tablePk,
            type: inferred ?? "string",
            mappedField: tablePk,
          };
          nextProperties.unshift(pkPropertyNew);
          detail.properties.unshift({ property: pkPropertyNew.name, column: tablePk, type: pkPropertyNew.type });
          primaryKey = pkPropertyNew.name;
          inferredPrimaryKeys += 1;
          detail.primaryKey = { property: pkPropertyNew.name, column: tablePk };
        }
      }
    } else {
      warnings.push(`对象「${object.name}」的来源表「${table.name}」没有推断出主键`);
    }

    boundObjects += 1;
    objectDetails.push(detail);
    return { ...object, sourceTable: table.name, primaryKey, properties: nextProperties };
  });

  const objectById = new Map(objectTypes.map((object) => [object.id, object]));
  const detailsByObjectId = new Map(objectDetails.map((item) => [item.objectId, item]));

  /** 找到对象上承载某列的属性；没有就补一个（外键列常被模型只当成关系而漏建属性）。 */
  const ensureColumnProperty = (
    object: IrObjectType | undefined,
    table: ExtractedTable,
    column: string,
    fallbackName?: string,
  ): IrProperty | undefined => {
    if (!object) return undefined;
    const existing = propertyForColumn(object, column);
    if (existing) return existing;
    const name = columnToPropertyName(column) || fallbackName || "ref";
    const uniqueName = uniquePropertyName(object, name);
    const nameTaken = object.properties.find((property) => property.name === uniqueName);
    if (nameTaken) return nameTaken;
    const inferred = inferColumnType(columnValues(table, column));
    const property: IrProperty = {
      name: uniqueName,
      displayName: column,
      type: inferred ?? "string",
      mappedField: column,
    };
    object.properties.push(property);
    properties += 1;
    mappedProperties += 1;
    detailsByObjectId.get(object.id)?.properties.push({ property: name, column, type: property.type });
    return property;
  };

  let inferredRelations = 0;
  const relationDetails: BindingRelationDetail[] = [];
  const relations: IrRelation[] = ir.relations.map((relation) => {
    const sourceObject = objectById.get(relation.source);
    const targetObject = objectById.get(relation.target);
    const sourceTable = usable.find((table) => table.name === sourceObject?.sourceTable);
    const targetTable = usable.find((table) => table.name === targetObject?.sourceTable);
    const detail: BindingRelationDetail = {
      relationId: relation.id,
      relationName: relation.name,
      sourceTable: sourceTable?.name,
      targetTable: targetTable?.name,
      mapping: [],
    };
    if (!sourceObject || !targetObject || !sourceTable || !targetTable) {
      relationDetails.push(detail);
      return relation;
    }

    const sourcePkColumn = primaryKeys.get(sourceTable.name);
    const targetPkColumn = primaryKeys.get(targetTable.name);
    // 外键可能在目标表（多端）或源表上，两种都试。
    const targetFk = targetPkColumn
      ? foreignKeyTargets(targetTable, usable, primaryKeys).find((fk) => fk.parentTable.name === sourceTable.name)
      : undefined;
    const sourceFk = sourcePkColumn
      ? foreignKeyTargets(sourceTable, usable, primaryKeys).find((fk) => fk.parentTable.name === targetTable.name)
      : undefined;

    let mapping: Array<{ sourceProperty: string; targetProperty: string }> = [];
    if (targetFk && sourcePkColumn) {
      // 外键列常常没有被模型建成属性（模型只把它当关系），这里按需补一个属性承载它。
      const sourceProperty = ensureColumnProperty(sourceObject, sourceTable, sourcePkColumn);
      const targetProperty = ensureColumnProperty(targetObject, targetTable, targetFk.column, sourceProperty?.name);
      if (sourceProperty && targetProperty) {
        mapping = [{ sourceProperty: sourceProperty.name, targetProperty: targetProperty.name }];
      }
    } else if (sourceFk && targetPkColumn) {
      const targetProperty = ensureColumnProperty(targetObject, targetTable, targetPkColumn);
      const sourceProperty = ensureColumnProperty(sourceObject, sourceTable, sourceFk.column, targetProperty?.name);
      if (sourceProperty && targetProperty) {
        mapping = [{ sourceProperty: sourceProperty.name, targetProperty: targetProperty.name }];
      }
    } else if (sourcePkColumn && targetPkColumn) {
      const sourceProperty = ensureColumnProperty(sourceObject, sourceTable, sourcePkColumn);
      const targetProperty = ensureColumnProperty(targetObject, targetTable, targetPkColumn);
      if (sourceProperty && targetProperty) {
        mapping = [{ sourceProperty: sourceProperty.name, targetProperty: targetProperty.name }];
      }
    }

    if (mapping.length > 0) {
      inferredRelations += 1;
      detail.mapping = mapping;
      relationDetails.push(detail);
      return { ...relation, mapping };
    }
    relationDetails.push(detail);
    return relation;
  });

  return {
    ir: { ...ir, objectTypes, relations },
    stats: {
      tables: tables.length,
      objects: ir.objectTypes.length,
      boundObjects,
      properties,
      mappedProperties,
      inferredPrimaryKeys,
      inferredRelations,
    },
    details: { objects: objectDetails, relations: relationDetails },
    warnings,
  };
}

/** 供测试与调试：判断一个名字是否会被当成同一字段。 */
export const matchNames = nameMatches;
export const isCjkName = isCjk;
