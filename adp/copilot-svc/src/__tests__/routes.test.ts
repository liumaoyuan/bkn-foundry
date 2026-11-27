import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { unzipSync } from "fflate";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { buildApp } from "../server.js";

const sampleText = [
  "# 设备运维",
  "",
  "## 设备",
  "## 部件",
  "## 故障",
  "## 工单",
  "",
  "设备包含部件，故障产生工单。",
].join("\n");

let settingsDir = "";

beforeAll(() => {
  // 单测把模型配置写到临时目录，避免读到开发者本机的 data/model-config.json
  settingsDir = mkdtempSync(join(tmpdir(), "copilot-settings-"));
  process.env.COPILOT_SETTINGS_PATH = join(settingsDir, "model-config.json");
  delete process.env.COPILOT_LLM_BASE_URL;
  delete process.env.COPILOT_LLM_API_KEY;
  delete process.env.COPILOT_LLM_MODEL;
});

afterAll(() => {
  delete process.env.COPILOT_SETTINGS_PATH;
  rmSync(settingsDir, { recursive: true, force: true });
});

describe("copilot routes", () => {
  it("health reports heuristic mode and settings source by default", async () => {
    const app = buildApp();
    const res = await app.inject({ method: "GET", url: "/api/copilot/health" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ ok: true, mode: "heuristic", settingsSource: "none" });
  });

  it("analyze turns pasted text into an IR", async () => {
    const app = buildApp();
    const res = await app.inject({
      method: "POST",
      url: "/api/copilot/analyze",
      payload: { text: sampleText, networkName: "设备运维网络" },
    });

    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.ok).toBe(true);
    expect(body.mode).toBe("heuristic");
    expect(body.ir.networkName).toBe("设备运维网络");
    expect(body.ir.objectTypes.length).toBeGreaterThanOrEqual(4);
    expect(body.ir.objectTypes[0].properties[0].name).toBe("id");
    expect(body.summary.objectTypes).toBe(body.ir.objectTypes.length);
  });

  it("compile/preview returns .bkn files", async () => {
    const app = buildApp();
    const analyze = await app.inject({
      method: "POST",
      url: "/api/copilot/analyze",
      payload: { text: sampleText },
    });
    const res = await app.inject({
      method: "POST",
      url: "/api/copilot/compile/preview",
      payload: { ir: analyze.json().ir },
    });

    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.ok).toBe(true);
    const paths = body.files.map((file: { path: string }) => file.path);
    expect(paths).toContain("network.bkn");
    expect(paths.some((path: string) => path.startsWith("object_types/"))).toBe(true);
    const objectFile = body.files.find((file: { path: string }) =>
      file.path.startsWith("object_types/"),
    );
    expect(objectFile.content).toContain("type: object_type");
    expect(objectFile.content).toContain("Primary Keys: id");
  });

  it("compile/download returns a zip containing the package", async () => {
    const app = buildApp();
    const analyze = await app.inject({
      method: "POST",
      url: "/api/copilot/analyze",
      payload: { text: sampleText },
    });
    const ir = analyze.json().ir;

    const res = await app.inject({
      method: "POST",
      url: "/api/copilot/compile/download",
      payload: { ir },
    });

    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toContain("application/zip");
    const files = unzipSync(new Uint8Array(res.rawPayload));
    const names = Object.keys(files);
    expect(names).toContain(`${ir.networkId}/network.bkn`);
  });

  it("settings round-trip masks the api key and drives llm mode", async () => {
    const app = buildApp();

    const empty = await app.inject({ method: "GET", url: "/api/copilot/settings" });
    expect(empty.json()).toMatchObject({ ok: true, source: "none", apiKeyMasked: "" });

    const invalid = await app.inject({
      method: "PUT",
      url: "/api/copilot/settings",
      payload: { baseUrl: "ftp://example.com", model: "m", apiKey: "sk-test-123456" },
    });
    expect(invalid.statusCode).toBe(400);

    const missingKey = await app.inject({
      method: "PUT",
      url: "/api/copilot/settings",
      payload: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat" },
    });
    expect(missingKey.statusCode).toBe(400);

    const saved = await app.inject({
      method: "PUT",
      url: "/api/copilot/settings",
      payload: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", apiKey: "sk-test-12345678" },
    });
    expect(saved.statusCode).toBe(200);
    expect(saved.json()).toMatchObject({
      ok: true,
      source: "file",
      model: "deepseek-chat",
      apiKeyMasked: "sk-t***5678",
    });

    // 保存后 key 不再需要重复输入
    const updated = await app.inject({
      method: "PUT",
      url: "/api/copilot/settings",
      payload: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-reasoner" },
    });
    expect(updated.json()).toMatchObject({ ok: true, model: "deepseek-reasoner" });

    const health = await app.inject({ method: "GET", url: "/api/copilot/health" });
    expect(health.json()).toMatchObject({ mode: "llm", settingsSource: "file" });

    const cleared = await app.inject({ method: "DELETE", url: "/api/copilot/settings" });
    expect(cleared.json()).toMatchObject({ ok: true, source: "none" });
  });

  it("chat streams a fallback reply when no model is configured", async () => {
    const app = buildApp();
    const res = await app.inject({
      method: "POST",
      url: "/api/copilot/chat",
      payload: { sessionId: "t", messages: [{ role: "user", content: "给草案加一个备件对象" }] },
    });

    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toContain("text/event-stream");
    const lines = res.body.split("\n\n").filter(Boolean);
    expect(lines.some((line) => line.includes('"type":"delta"'))).toBe(true);
    expect(lines.at(-1)).toContain('"type":"done"');
    expect(res.body).toContain("模型连接");
  });
});
