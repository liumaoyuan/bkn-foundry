import type { IrObjectType, IrPropertyType, IrRelation, OntologyIr } from "./types/ir.js";

/** 把 IR 转成平台「导入知识网络」接口的 JSON 结构（POST /bkn-backend/v1/knowledge-networks）。 */

const PROPERTY_TYPE_MAP: Record<IrPropertyType, string> = {
  string: "string",
  integer: "integer",
  number: "float",
  boolean: "boolean",
  datetime: "datetime",
};

export type KnPayloadActionType = {
  id: string;
  name: string;
  comment: string;
  tags: string[];
  action_type: string;
  object_type_id: string;
  condition?: {
    object_type_id: string;
    field: string;
    operation: string;
    value?: string;
  };
  parameters: never[];
  schedule: { type: string; expression: string };
  action_source: { type: string };
};

export type KnPayloadRiskType = {
  id: string;
  name: string;
  comment: string;
  tags: string[];
};

export type KnImportPayload = {
  id: string;
  name: string;
  branch: string;
  comment: string;
  tags: string[];
  object_types: KnPayloadObjectType[];
  relation_types: KnPayloadRelationType[];
  concept_groups: KnPayloadConceptGroup[];
  action_types?: KnPayloadActionType[];
  risk_types?: KnPayloadRiskType[];
};

export type KnPayloadObjectType = {
  id: string;
  name: string;
  comment: string;
  tags: string[];
  data_properties: KnPayloadDataProperty[];
  logic_properties: never[];
  primary_keys: string[];
  display_key: string;
  incremental_key: string;
  /** 自动接入时绑定到平台资源（对象级数据来源）。 */
  data_source?: { type: "resource"; id: string; name?: string };
};

export type KnPayloadDataProperty = {
  name: string;
  display_name: string;
  type: string;
  comment: string;
  /**
   * 平台字段级绑定：mapped_field 必须是对象 `{ name }`（字符串形态会 400 参数绑定失败）。
   * name 为资源里的物理列名。
   */
  mapped_field?: { name: string };
};

export type KnPayloadSimpleProperty = {
  name: string;
};

export type KnPayloadMappingRule = {
  source_property: KnPayloadSimpleProperty;
  target_property: KnPayloadSimpleProperty;
};

export type KnPayloadRelationType = {
  id: string;
  name: string;
  comment: string;
  tags: string[];
  source_object_type_id: string;
  target_object_type_id: string;
  type: "direct";
  mapping_rules: KnPayloadMappingRule[];
};

export type KnPayloadConceptGroup = {
  id: string;
  name: string;
  comment: string;
  object_type_ids: string[];
};

function toObjectType(
  objectType: IrObjectType,
  includeMappedField: boolean,
  resourceBindings: Record<string, { id: string; name?: string }> = {},
): KnPayloadObjectType {
  const properties = objectType.properties ?? [];
  const primaryKey = objectType.primaryKey ?? "id";
  return {
    id: objectType.id,
    name: objectType.name,
    comment: objectType.description ?? "",
    tags: objectType.tags ?? [],
    data_properties: properties.map((property) => ({
      name: property.name,
      display_name: property.displayName ?? property.name,
      type: PROPERTY_TYPE_MAP[property.type] ?? "string",
      comment: property.description ?? "",
      ...(includeMappedField && property.mappedField ? { mapped_field: { name: property.mappedField } } : {}),
    })),
    logic_properties: [],
    primary_keys: [primaryKey],
    display_key: objectType.displayKey ?? "name",
    incremental_key: "",
  };
}

function toRelationType(
  relation: IrRelation,
  objectTypes: IrObjectType[],
): KnPayloadRelationType {
  const sourcePrimary = objectTypes.find((item) => item.id === relation.source)?.primaryKey ?? "id";
  const targetPrimary = objectTypes.find((item) => item.id === relation.target)?.primaryKey ?? "id";
  // 数据绑定推断出的外键映射优先；没有推断结果时退回「两侧主键」映射，
  // 因为 0.1.5 平台要求 direct 关系必须有非空 mapping_rules。
  const inferred = relation.mapping ?? [];
  const mappingRules: KnPayloadMappingRule[] =
    inferred.length > 0
      ? inferred.map((item) => ({
          source_property: { name: item.sourceProperty },
          target_property: { name: item.targetProperty },
        }))
      : [
          {
            source_property: { name: sourcePrimary },
            target_property: { name: targetPrimary },
          },
        ];
  return {
    id: relation.id,
    name: relation.name,
    comment: relation.description ?? "",
    tags: [],
    source_object_type_id: relation.source,
    target_object_type_id: relation.target,
    type: "direct",
    mapping_rules: mappingRules,
  };
}

export type KnPayloadOptions = {
  /** 是否携带行动/风险类型；旧版本平台（0.1.5）如不兼容可用 COPILOT_KN_INCLUDE_EXTRA_TYPES=0 关闭。 */
  includeExtraTypes?: boolean;
  /**
   * 是否在 data_properties 里带 mapped_field。
   * 官方 0.2.1 平台导入接口不识别该字段（会 400 参数绑定失败），默认关闭；
   * 0.1.5/0.2.1 都接受对象形态 { name }，因此默认开启；设置 COPILOT_KN_INCLUDE_MAPPED_FIELD=0 可关闭。
   */
  includeMappedField?: boolean;
  /** 对象 id → 平台资源（自动接入时由前端创建后回传）。 */
  resourceBindings?: Record<string, { id: string; name?: string }>;
};

/** 生成可直接 POST 给 bkn-backend 的知识网络导入载荷。 */
export function compileIrToKnPayload(ir: OntologyIr, options: KnPayloadOptions = {}): KnImportPayload {
  const includeExtraTypes = options.includeExtraTypes ?? true;
  const includeMappedField = options.includeMappedField ?? true;
  const resourceBindings = options.resourceBindings ?? {};
  return {
    id: ir.networkId,
    name: ir.networkName,
    branch: "main",
    comment: ir.description ?? "",
    tags: [],
    object_types: ir.objectTypes.map((objectType) => {
      const payload = toObjectType(objectType, includeMappedField);
      const resource = resourceBindings[objectType.id];
      if (resource?.id) {
        payload.data_source = { type: "resource", id: resource.id, ...(resource.name ? { name: resource.name } : {}) };
      }
      return payload;
    }),
    relation_types: ir.relations.map((relation) => toRelationType(relation, ir.objectTypes)),
    concept_groups: (ir.conceptGroups ?? []).map((group) => ({
      id: group.id,
      name: group.name,
      comment: group.description ?? "",
      object_type_ids: group.objectTypes,
    })),
    ...(includeExtraTypes
      ? {
          action_types: (ir.actionTypes ?? []).map((action) => ({
            id: action.id,
            name: action.name,
            comment: action.description ?? "",
            tags: [],
            action_type: action.actionType,
            object_type_id: action.boundObject,
            ...(action.trigger
              ? {
                  condition: {
                    object_type_id: action.boundObject,
                    field: action.trigger.field,
                    operation: action.trigger.operation,
                    ...(action.trigger.value ? { value: action.trigger.value } : {}),
                  },
                }
              : {}),
            parameters: [],
            schedule: { type: "", expression: "" },
            action_source: { type: "" },
          })),
          risk_types: (ir.riskTypes ?? []).map((risk) => ({
            id: risk.id,
            name: risk.name,
            comment: risk.description ?? "",
            tags: [],
          })),
        }
      : {}),
  };
}
