import type { FastifyInstance } from "fastify";
import { zipSync } from "fflate";

import { composeAnalyzeSource } from "../analyze-source.js";
import { inferIrBindings } from "../bind-datasource.js";
import { buildBindingKit } from "../datasource-kit.js";
import { buildAttachPlan, type AttachTarget } from "../vega-plan.js";
import { buildIndexPlan } from "../index-plan.js";
import {
  createAlignmentViews,
  introspectSchema,
  normalizeIntrospectTarget,
  testIntrospectConnection,
} from "../introspect.js";
import { attachEvidence, type EvidenceSource } from "../evidence.js";
import { dedupeRelations, dedupeWarning } from "../relations.js";
import {
  extractDocument,
  extractTables,
  type ExtractedTable,
  type ExtractHealth,
} from "../extract.js";
import { applyIrOps } from "../edit.js";
import { heuristicExtract } from "../heuristic.js";
import { compileIrToBkn } from "../ir-to-bkn.js";
import { compileIrToKnPayload } from "../ir-to-kn.js";
import {
  buildSchemaBrief,
  deriveActionsAndRisks,
  mergeSchemas,
  parseSqlSchema,
  schemaStats,
  schemaToIr,
  schemaToTables,
  type Schema,
} from "../schema.js";
import {
  chatWithDraftStream,
  extractIrFromSchema,
  extractIrWithLlm,
  isLlmConfigured,
  testLlmConnection,
} from "../llm.js";
import {
  clearStoredSettings,
  isHttpUrl,
  maskApiKey,
  readEnvSettings,
  readStoredSettings,
  resolveSettings,
  writeStoredSettings,
} from "../settings.js";
import type { OntologyIr } from "../types/ir.js";
import { normalizeIr } from "../types/ir.js";

type ChatBody = {
  sessionId?: string;
  messages: { role: string; content: string }[];
  draft?: unknown;
};

type AnalyzeJsonBody = {
  text?: string;
  networkName?: string;
};

type SettingsBody = {
  baseUrl?: string;
  model?: string;
  apiKey?: string;
};

const textEncoder = new TextEncoder();
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function toFilesObject(files: { path: string; content: string }[]): Record<string, Uint8Array> {
  return Object.fromEntries(files.map((file) => [file.path, textEncoder.encode(file.content)]));
}

function bindingWarnings(stats: { objects: number; boundObjects: number }): string[] {
  if (stats.objects > 0 && stats.boundObjects === 0) {
    return ["没有匹配到数据表，导入平台后需要手动绑定数据来源"];
  }
  return [];
}

/** 读取界面传入的网络名称（可选）。 */
function readNetworkName(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** 数据接入包的 SQL 方言：只有明确的 postgres 才用双引号，其余按 MySQL 反引号处理。 */
function readDialect(value: unknown): "mysql" | "postgres" {
  return typeof value === "string" && value.trim().toLowerCase() === "postgres" ? "postgres" : "mysql";
}

/** 由 Schema 生成草案的统一链路：结构 brief → 模型/规则 → 归一化 → 去重 → 绑定 → 证据。 */
async function analyzeSchemaSource(
  schema: Schema,
  options: { fallbackName: string; extraTables?: ExtractedTable[]; extraEvidence?: EvidenceSource[] } = {
    fallbackName: "数据库知识网络",
  },
): Promise<{
  mode: "llm" | "heuristic";
  llmError?: string;
  ir: ReturnType<typeof attachEvidence>["ir"];
  warnings: string[];
  schemaStats: ReturnType<typeof schemaStats>;
  openQuestions: string[];
  binding: ReturnType<typeof inferIrBindings>["stats"];
  bindingDetails: ReturnType<typeof inferIrBindings>["details"];
  evidence: ReturnType<typeof attachEvidence>["stats"];
  summary: { objectTypes: number; relations: number; conceptGroups: number };
}> {
  const brief = buildSchemaBrief(schema);
  const openQuestions: string[] = [];
  let mode: "llm" | "heuristic" = "heuristic";
  let llmError: string | undefined;
  let raw: unknown = null;

  if (isLlmConfigured()) {
    const result = await extractIrFromSchema(brief);
    if ("raw" in result) {
      raw = result.raw;
      mode = "llm";
    } else {
      llmError = result.error;
    }
  }

  const fallback = schemaToIr(schema, options.fallbackName);
  if (raw === null) {
    raw = fallback;
    openQuestions.push(...fallback.questions);
  } else if (raw && typeof raw === "object") {
    const fromLlm = (raw as { questions?: unknown }).questions;
    if (Array.isArray(fromLlm)) {
      for (const item of fromLlm.slice(0, 5)) {
        const text = String(item ?? "").trim();
        if (text) openQuestions.push(text);
      }
    }
  }

  const normalized = normalizeIr(raw);
  if (!normalized.ok) {
    throw new Error(`模型返回的本体草案不合法：${normalized.errors.join("；")}`);
  }

  const deduped = dedupeRelations(normalized.ir.relations);
  // 数据库结构模式：没有业务描述时，从 status/level 这类结构语义补出行动与风险。
  const derived = deriveActionsAndRisks(schema, normalized.ir.objectTypes);
  const dedupedIr = {
    ...normalized.ir,
    relations: deduped.relations,
    actionTypes:
      normalized.ir.actionTypes && normalized.ir.actionTypes.length > 0
        ? normalized.ir.actionTypes
        : derived.actionTypes,
    riskTypes:
      normalized.ir.riskTypes && normalized.ir.riskTypes.length > 0
        ? normalized.ir.riskTypes
        : derived.riskTypes,
  };
  const binding = inferIrBindings(dedupedIr, [...(options.extraTables ?? []), ...schemaToTables(schema)]);
  const grounded = attachEvidence(binding.ir, [
    { fileName: "数据库结构", text: brief },
    ...(options.extraEvidence ?? []),
  ]);

  return {
    mode,
    llmError,
    ir: grounded.ir,
    warnings: [
      ...normalized.warnings,
      ...(dedupeWarning(deduped) ? [dedupeWarning(deduped) as string] : []),
      ...bindingWarnings(binding.stats),
      ...binding.warnings,
      ...(grounded.stats.objects > 0 && grounded.stats.objectsWithEvidence === 0
        ? ["没有在输入材料中找到对象名称的原文出处，证据引用为空"]
        : []),
    ],
    schemaStats: schemaStats(schema),
    openQuestions,
    binding: binding.stats,
    bindingDetails: binding.details,
    evidence: grounded.stats,
    summary: {
      objectTypes: grounded.ir.objectTypes.length,
      relations: grounded.ir.relations.length,
      conceptGroups: (grounded.ir.conceptGroups ?? []).length,
    },
  };
}
function splitForStreaming(text: string): string[] {
  const chunks: string[] = [];
  let index = 0;
  while (index < text.length) {
    chunks.push(text.slice(index, index + 12));
    index += 12;
  }
  return chunks.length > 0 ? chunks : [""];
}

function settingsSummary() {
  const { source, settings } = resolveSettings();
  return {
    ok: true,
    source,
    baseUrl: settings?.baseUrl ?? "",
    model: settings?.model ?? "",
    apiKeyMasked: settings ? maskApiKey(settings.apiKey) : "",
  };
}

export async function copilotRoutes(app: FastifyInstance) {
  app.get("/health", async () => {
    const { source } = resolveSettings();
    return {
      ok: true,
      mode: isLlmConfigured() ? "llm" : "heuristic",
      settingsSource: source,
    };
  });

  // ---- 模型连接配置（只在本机服务端读写，key 不下发浏览器） ----
  app.get("/settings", async () => settingsSummary());

  app.put("/settings", async (request, reply) => {
    const body = (request.body ?? {}) as SettingsBody;
    const baseUrl = (body.baseUrl ?? "").trim();
    const model = (body.model ?? "").trim();
    const apiKeyInput = (body.apiKey ?? "").trim();

    if (!baseUrl || !model) {
      reply.code(400);
      return { ok: false, errors: ["请填写接口地址与模型名称"] };
    }
    if (!isHttpUrl(baseUrl)) {
      reply.code(400);
      return { ok: false, errors: ["接口地址必须是 http(s) URL"] };
    }

    const fallbackKey =
      readStoredSettings()?.apiKey ?? readEnvSettings()?.apiKey ?? "";
    const apiKey = apiKeyInput || fallbackKey;
    if (!apiKey) {
      reply.code(400);
      return { ok: false, errors: ["请填写 API Key"] };
    }

    writeStoredSettings({ baseUrl, model, apiKey });
    return settingsSummary();
  });

  app.delete("/settings", async () => {
    clearStoredSettings();
    return settingsSummary();
  });

  app.post("/settings/test", async (request, reply) => {
    const body = (request.body ?? {}) as SettingsBody;
    const result = await testLlmConnection({
      baseUrl: body.baseUrl?.trim(),
      model: body.model?.trim(),
      apiKey: body.apiKey?.trim(),
    });
    if (!result.ok) {
      reply.code(400);
      return { ok: false, error: result.error };
    }
    return { ok: true, latencyMs: result.latencyMs };
  });

  app.post("/chat", async (request, reply) => {
    const body = (request.body ?? {}) as ChatBody;
    const rawMessages = Array.isArray(body.messages) ? body.messages : [];
    const messages = rawMessages
      .filter((item) => item && typeof item.content === "string")
      .map((item) => ({
        role: item.role === "assistant" ? "assistant" : "user",
        content: item.content,
      }));

    const draftResult = body.draft ? normalizeIr(body.draft) : null;
    const draft = draftResult?.ok ? draftResult.ir : undefined;

    reply.raw.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    const writeEvent = (event: Record<string, unknown>) => {
      reply.raw.write(`data: ${JSON.stringify(event)}\n\n`);
    };

    if (!isLlmConfigured()) {
      const fallback =
        "还没有配置模型连接。点抽屉右上角的设置按钮填写接口地址、模型名和 API Key 后，就可以和我对话修改草案了。";
      for (const text of splitForStreaming(fallback)) {
        writeEvent({ type: "delta", text });
        await sleep(10);
      }
      writeEvent({ type: "done", warnings: [] });
      reply.raw.end();
      return;
    }

    writeEvent({ type: "status", stage: "thinking", text: "正在阅读当前草案并理解你的要求…" });

    let firstToken = true;
    const llmResult = await chatWithDraftStream(messages, draft, (text) => {
      if (firstToken) {
        firstToken = false;
        writeEvent({ type: "status", stage: "replying", text: "正在说明修改方案…" });
      }
      writeEvent({ type: "delta", text });
    });

    if (!llmResult.ok) {
      const fallback = `模型调用失败（${llmResult.error}），请检查「模型连接」设置后重试。`;
      for (const text of splitForStreaming(fallback)) {
        writeEvent({ type: "delta", text });
        await sleep(10);
      }
      writeEvent({ type: "done", warnings: [] });
      reply.raw.end();
      return;
    }

    // ops 模式：模型只给原子操作，服务端确定性执行，避免「嘴上删了、草案没变」。
    let applied: string[] = [];
    let warnings: string[] = [];
    let nextIr: OntologyIr | undefined;
    const hasOps = Array.isArray(llmResult.ops) && llmResult.ops.length > 0;

    if (hasOps) {
      writeEvent({ type: "status", stage: "applying", text: "正在把修改写入草案…" });
      const base: OntologyIr = draft ?? {
        networkId: "kn_generated",
        networkName: "新建知识网络",
        description: "",
        objectTypes: [],
        relations: [],
        conceptGroups: [],
      };
      const result = applyIrOps(base, llmResult.ops);
      applied = result.applied;
      warnings = result.warnings;
      if (applied.length > 0) {
        const deduped = dedupeRelations(result.ir.relations);
        nextIr = deduped.removed > 0 ? { ...result.ir, relations: deduped.relations } : result.ir;
        if (deduped.removed > 0) {
          warnings = [...warnings, dedupeWarning(deduped) as string];
        }
      } else {
        warnings = ["模型没有给出可执行的修改，草案保持不变", ...warnings];
      }
    } else if (llmResult.ir) {
      const normalized = normalizeIr(llmResult.ir);
      if (normalized.ok) {
        nextIr = normalized.ir;
        warnings = normalized.warnings;
      } else {
        warnings = ["模型返回的草案格式不合法，已忽略"];
      }
    }

    writeEvent({ type: "done", ir: nextIr, applied, warnings });
    reply.raw.end();
  });

  app.post("/analyze", async (request, reply) => {
    const reportPieces: string[] = [];
    const dataPieces: string[] = [];
    const tables: ExtractedTable[] = [];
    const health: ExtractHealth[] = [];
    const evidenceSources: EvidenceSource[] = [];
    const plainPieces: string[] = [];
    const fileNames: string[] = [];
    const sqlSchemas: Schema[] = [];
    const fileErrors: string[] = [];
    let networkName = "";
    let pastedText = "";

    if (request.isMultipart()) {
      for await (const part of request.parts()) {
        if (part.type === "file") {
          const buffer = await part.toBuffer();
          if (/\.(sql|ddl)$/i.test(part.filename)) {
            try {
              // 数据库结构文件（DDL / mysqldump --no-data / pg_dump -s）：单独解析成 Schema，
              // 供「无业务描述、只有数据库」的场景生成本体。
              const parsed = parseSqlSchema(buffer.toString("utf8"));
              if (parsed.tables.length > 0) sqlSchemas.push(parsed);
            } catch {
              // 解析失败时不影响其他文件，后续按普通文本处理。
            }
          }
          try {
            const extracted = await extractDocument(part.filename, buffer);
            fileNames.push(extracted.fileName);
            if (extracted.health && extracted.health.length > 0) health.push(...extracted.health);
            const sheetTables = await extractTables(part.filename, buffer);
            tables.push(...sheetTables);
            evidenceSources.push({ fileName: extracted.fileName, text: extracted.text });
            for (const table of sheetTables) {
              evidenceSources.push({
                fileName: extracted.fileName,
                location: table.name,
                text: table.headers.join(" "),
              });
            }
            for (const table of sheetTables) {
              if (table.health && table.health.length > 0) health.push(...table.health);
            }
            const block = `## 文件：${extracted.fileName}\n${extracted.text}`;
            if (part.fieldname === "reportFiles") {
              reportPieces.push(block);
            } else if (part.fieldname === "dataFiles") {
              dataPieces.push(block);
            } else {
              plainPieces.push(block);
            }
          } catch (error) {
            // 单个文件类型不支持时只跳过该文件并给出提示，其余文件继续解析。
            fileErrors.push(error instanceof Error ? error.message : String(error));
          }
        } else if (part.fieldname === "text") {
          pastedText += String(part.value ?? "");
        } else if (part.fieldname === "networkName") {
          networkName = String(part.value ?? "");
        }
      }
    } else {
      const body = (request.body ?? {}) as AnalyzeJsonBody;
      pastedText = body.text ?? "";
      networkName = body.networkName ?? "";
    }

    const mergedSchema = mergeSchemas(sqlSchemas);
    const schemaMode = mergedSchema.tables.length > 0;
    const schemaBrief = schemaMode ? buildSchemaBrief(mergedSchema) : "";
    const openQuestions: string[] = [];

    const sourceText = schemaMode
      ? [schemaBrief, ...(pastedText.trim() ? [`## 补充说明\n${pastedText}`] : [])].join("\n\n")
      : composeAnalyzeSource({
          report: reportPieces,
          data: dataPieces,
          plain: plainPieces,
          pasted: pastedText,
        });
    if (!sourceText) {
      reply.code(400);
      return { ok: false, errors: fileErrors.length > 0 ? fileErrors : ["没有可分析的内容"] };
    }

    let mode: "llm" | "heuristic" = "heuristic";
    let raw: unknown = null;
    let llmError: string | undefined;

    if (schemaMode && isLlmConfigured()) {
      const result = await extractIrFromSchema(sourceText);
      if ("raw" in result) {
        raw = result.raw;
        mode = "llm";
      } else {
        llmError = result.error;
      }
    } else if (isLlmConfigured() && !schemaMode) {
      const result = await extractIrWithLlm(sourceText);
      if ("raw" in result) {
        raw = result.raw;
        mode = "llm";
      } else {
        llmError = result.error;
      }
    }

    const schemaFallback = schemaMode
      ? schemaToIr(mergedSchema, networkName || fileNames[0] || "数据库知识网络")
      : null;

    if (raw === null && schemaFallback) {
      raw = schemaFallback;
      openQuestions.push(...schemaFallback.questions);
    } else if (raw === null) {
      raw = heuristicExtract(sourceText, networkName || fileNames[0]);
    } else if (raw && typeof raw === "object") {
      const fromLlm = (raw as { questions?: unknown }).questions;
      if (Array.isArray(fromLlm)) {
        for (const item of fromLlm.slice(0, 5)) {
          const text = String(item ?? "").trim();
          if (text) openQuestions.push(text);
        }
      }
    }

    const normalized = normalizeIr(raw);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors, mode };
    }

    const deduped = dedupeRelations(normalized.ir.relations);
    const dedupedIr = { ...normalized.ir, relations: deduped.relations };

    if (pastedText.trim()) {
      evidenceSources.push({ fileName: "粘贴文本", text: pastedText });
    }

    const binding = inferIrBindings(
      dedupedIr,
      schemaMode ? [...tables, ...schemaToTables(mergedSchema)] : tables,
    );
    const grounded = attachEvidence(binding.ir, evidenceSources);

    return {
      ok: true,
      mode,
      llmError,
      files: fileNames,
      characters: sourceText.length,
      warnings: [
        ...normalized.warnings,
        ...(dedupeWarning(deduped) ? [dedupeWarning(deduped) as string] : []),
        ...health.map((item) => item.message),
        ...bindingWarnings(binding.stats),
        ...binding.warnings,
        ...(grounded.stats.objects > 0 && grounded.stats.objectsWithEvidence === 0
          ? ["没有在输入材料中找到对象名称的原文出处，证据引用为空"]
          : []),
      ],
      ir: grounded.ir,
      tables: tables.map((table) => ({
        name: table.name,
        headers: table.headers.slice(0, 40),
        rows: table.rows,
        headerRowCount: table.headerRowCount,
      })),
      health,
      schema: schemaMode ? schemaStats(mergedSchema) : undefined,
      openQuestions,
      binding: binding.stats,
      bindingDetails: binding.details,
      evidence: grounded.stats,
      summary: {
        objectTypes: grounded.ir.objectTypes.length,
        relations: grounded.ir.relations.length,
        conceptGroups: (grounded.ir.conceptGroups ?? []).length,
      },
    };
  });

  app.post("/introspect/test", async (request, reply) => {
    const normalized = normalizeIntrospectTarget(request.body);
    if (!normalized.ok) {
      reply.code(400);
      return { ok: false, errors: normalized.errors };
    }
    const result = await testIntrospectConnection(normalized.target);
    if (!result.ok) {
      reply.code(400);
      return { ok: false, errors: [result.error] };
    }
    return { ok: true, serverVersion: result.serverVersion, tables: result.tables };
  });

  app.post("/introspect", async (request, reply) => {
    const body = (request.body ?? {}) as Record<string, unknown>;
    const normalized = normalizeIntrospectTarget(body);
    if (!normalized.ok) {
      reply.code(400);
      return { ok: false, errors: normalized.errors };
    }

    const introspected = await introspectSchema(normalized.target);
    if (!introspected.ok) {
      reply.code(400);
      return { ok: false, errors: [introspected.error] };
    }
    if (introspected.result.schema.tables.length === 0) {
      reply.code(400);
      return { ok: false, errors: ["连接成功，但没有读到任何表；请确认库名/schema 是否正确"] };
    }

    try {
      const analysis = await analyzeSchemaSource(introspected.result.schema, {
        fallbackName: readNetworkName(body.networkName) || `${normalized.target.database} 知识网络`,
      });
      return {
        ok: true,
        mode: analysis.mode,
        llmError: analysis.llmError,
        ir: analysis.ir,
        warnings: analysis.warnings,
        schema: analysis.schemaStats,
        openQuestions: analysis.openQuestions,
        binding: analysis.binding,
        bindingDetails: analysis.bindingDetails,
        evidence: analysis.evidence,
        summary: analysis.summary,
        scannedTables: introspected.result.scannedTables,
        serverVersion: introspected.result.serverVersion,
      };
    } catch (error) {
      reply.code(422);
      return { ok: false, errors: [error instanceof Error ? error.message : String(error)] };
    }
  });
  app.post("/introspect/views", async (request, reply) => {
    const body = (request.body ?? {}) as Record<string, unknown>;
    const normalized = normalizeIntrospectTarget(body);
    if (!normalized.ok) {
      reply.code(400);
      return { ok: false, errors: normalized.errors };
    }
    const rawViews = Array.isArray(body.views) ? body.views : [];
    const views = rawViews
      .map((item) => {
        const view = (item ?? {}) as Record<string, unknown>;
        const columns = Array.isArray(view.columns) ? view.columns : [];
        return {
          viewName: typeof view.viewName === "string" ? view.viewName.trim() : "",
          tableName: typeof view.tableName === "string" ? view.tableName.trim() : "",
          columns: columns
            .map((column) => {
              const pair = (column ?? {}) as Record<string, unknown>;
              const name = typeof pair.column === "string" ? pair.column.trim() : "";
              // 没有来源列的属性（如模型补的 id）用 NULL 占位，保证视图列与属性一一对应。
              return {
                ...(name ? { column: name } : {}),
                property: typeof pair.property === "string" ? pair.property.trim() : "",
              };
            })
            .filter((column) => Boolean(column.property)),
        };
      })
      .filter((view) => view.viewName && view.tableName && view.columns.length > 0);
    if (views.length === 0) {
      reply.code(400);
      return { ok: false, errors: ["没有可创建的对齐视图（需要对象名/表名/列映射）"] };
    }
    try {
      const result = await createAlignmentViews(normalized.target, views);
      return { ok: true, created: result.created, failed: result.failed };
    } catch (error) {
      reply.code(422);
      return { ok: false, errors: [error instanceof Error ? error.message : String(error)] };
    }
  });

  app.post("/compile/preview", async (request, reply) => {
    const body = (request.body ?? {}) as { ir?: unknown };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }
    const files = compileIrToBkn(normalized.ir);
    return {
      ok: true,
      networkId: normalized.ir.networkId,
      warnings: normalized.warnings,
      files,
    };
  });

  app.post("/compile/kn-payload", async (request, reply) => {
    const body = (request.body ?? {}) as {
      ir?: unknown;
      resourceBindings?: Record<string, { id: string; name?: string }>;
    };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }
    return {
      ok: true,
      networkId: normalized.ir.networkId,
      warnings: normalized.warnings,
      payload: compileIrToKnPayload(normalized.ir, { resourceBindings: body.resourceBindings,
        includeExtraTypes: process.env.COPILOT_KN_INCLUDE_EXTRA_TYPES !== "0",
      }),
    };
  });
  app.post("/compile/attach-plan", async (request, reply) => {
    const body = (request.body ?? {}) as {
      ir?: unknown;
      target?: Partial<AttachTarget>;
      catalogName?: string;
    };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }
    const target = body.target;
    if (
      !target ||
      (target.dialect !== "mysql" && target.dialect !== "postgres") ||
      !target.host ||
      !target.database ||
      !target.user ||
      !target.password
    ) {
      reply.code(400);
      return {
        ok: false,
        errors: ["需要提供 target：dialect(mysql|postgres)、host、database、user、password"],
      };
    }
    const plan = buildAttachPlan(normalized.ir, target as AttachTarget, {
      catalogName: body.catalogName,
      networkName: normalized.ir.networkName,
    });
    return { ok: true, networkId: normalized.ir.networkId, plan };
  });
  app.post("/compile/index-plan", async (request, reply) => {
    const body = (request.body ?? {}) as { ir?: unknown };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }
    const plan = buildIndexPlan(normalized.ir);
    return { ok: true, networkId: normalized.ir.networkId, plan };
  });
  app.post("/compile/binding-kit", async (request, reply) => {
    const body = (request.body ?? {}) as { ir?: unknown; dialect?: unknown };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }
    const kit = buildBindingKit(normalized.ir, { dialect: readDialect(body.dialect) });
    return { ok: true, warnings: [...normalized.warnings, ...kit.warnings], files: kit.files, stats: kit.stats };
  });

  app.post("/compile/binding-kit/download", async (request, reply) => {
    const body = (request.body ?? {}) as { ir?: unknown; dialect?: unknown };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }

    const kit = buildBindingKit(normalized.ir, { dialect: readDialect(body.dialect) });
    const folderName = `${normalized.ir.networkId}-binding-kit`;
    const zipped = zipSync(
      Object.fromEntries(
        kit.files.map((file) => [`${folderName}/${file.name}`, textEncoder.encode(file.content)]),
      ),
    );

    reply
      .header("Content-Type", "application/zip")
      .header("Content-Disposition", `attachment; filename="${folderName}.zip"`);
    return reply.send(Buffer.from(zipped));
  });

  app.post("/compile/download", async (request, reply) => {
    const body = (request.body ?? {}) as { ir?: unknown };
    const normalized = normalizeIr(body.ir);
    if (!normalized.ok) {
      reply.code(422);
      return { ok: false, errors: normalized.errors };
    }

    const files = compileIrToBkn(normalized.ir);
    const folderName = normalized.ir.networkId;
    const zipped = zipSync(
      Object.fromEntries(
        Object.entries(toFilesObject(files)).map(([path, content]) => [
          `${folderName}/${path}`,
          content,
        ]),
      ),
    );

    reply
      .header("Content-Type", "application/zip")
      .header("Content-Disposition", `attachment; filename="${folderName}.zip"`);
    return reply.send(Buffer.from(zipped));
  });
}
