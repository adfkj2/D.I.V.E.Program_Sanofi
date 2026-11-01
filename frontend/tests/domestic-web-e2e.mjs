import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

// UI-only fixtures. Every mutation is intercepted and no real credential is used.
const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8768";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true);
const initial = await (await fetch(`${base}/api/v1/state`)).json();
const artifacts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../artifacts/domestic-web-e2e");
await mkdir(artifacts, { recursive: true });
await writeFile(path.join(artifacts, ".gitignore"), "data-e2e*/\n*.log\n");
const providers = [
  { id: "deepseek", name: "DeepSeek 搜索", description: "搜索与网页读取共用 DeepSeek Key，不需要切换聊天模型。", key_hint: "填入 DeepSeek API Key", docs_url: "https://api-docs.deepseek.com/guides/search/", reuse_provider: "deepseek", recommended: true },
  { id: "kimi", name: "Kimi 搜索", description: "使用 Kimi 搜索和网页读取。", key_hint: "填入 Kimi API Key", reuse_provider: "kimi" },
  { id: "glm", name: "智谱搜索", description: "使用境内搜索服务。", key_hint: "填入智谱 API Key", reuse_provider: "glm", engines: ["search_std", "search_pro"], default_engine: "search_std" },
  { id: "bocha", name: "博查搜索", description: "可查询中文网页；只有摘要时会明确标注。", key_hint: "填入博查 API Key" },
  { id: "tavily", name: "Tavily", description: "搜索与网页读取。", key_hint: "填入 Tavily API Key" },
];
const connections = [
  { id: "deepseek-saved", provider: "deepseek", model: "deepseek-flash", base_url: "https://api.deepseek.com/v1", label: "我保存的 DeepSeek", key_status: "CONFIGURED", storage: "OS_CREDENTIAL_STORE", is_default: true },
  { id: "glm-saved", provider: "glm", model: "glm-fixture", base_url: "https://open.bigmodel.cn/api/paas/v4", label: "智谱备用连接", key_status: "CONFIGURED", storage: "SESSION_ONLY" },
  { id: "kimi-missing", provider: "kimi", model: "kimi-fixture", base_url: "https://api.moonshot.cn/v1", label: "Key 已失效的 Kimi", key_status: "MISSING" },
  { id: "deepseek-proxy", provider: "deepseek", model: "proxy-fixture", base_url: "https://example.com/proxy", label: "第三方代理连接", key_status: "CONFIGURED" },
];
let config = { provider: "tavily", configured: false, enabled: false, revision: 0, credential_store_available: false };
let outcome = "TEST_FAILED", validationDelay = 200;
const writes = [], errors = [], external = [], checks = [], screenshots = [];
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
const page = await context.newPage();
page.on("pageerror", error => errors.push(error.message));
page.on("request", req => { if (!req.url().startsWith(base) && !req.url().startsWith("data:")) external.push(req.url()); });
await page.route("**/assets/**", async route => {
  const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } });
  await route.fulfill({ response });
});
const character = initial.skills.find(item => item.slug === "zhangxuefeng");
const room = { id: "domestic-web-chat", title: "信息怎么核实", skill_id: character.id, created_at: "2026-11-01T01:00:00Z" };
const sources = [
  { id: "source-body", title: "网页正文测试", url: "https://www.example.edu.cn/plan", evidence_kind: "PAGE_CONTENT", excerpt: "这段内容来自网页读取，示例不代表真实招生数据。", retrieved_at: "2026-11-01T01:00:00Z" },
  { id: "source-snippet", title: "搜索摘要测试", url: "https://www.example.edu.cn/summary", evidence_kind: "SEARCH_SNIPPET", excerpt: "搜索摘要只能定位资料，不能冒充完整正文。", retrieved_at: "2026-11-01T01:00:00Z" },
  { id: "source-grounded", title: "联网总结测试", url: "https://www.example.edu.cn/grounding", evidence_kind: "PROVIDER_GROUNDED_SUMMARY", excerpt: "供应商生成的联网总结，与原网页正文不同。", retrieved_at: "2026-11-01T01:00:00Z" },
];
function state() { return { ...structuredClone(initial), conversations: [room], upgrade: { ...initial.upgrade, web: config, connections, preferences: { setup_dismissed: true } } }; }
await page.route("**/api/v1/**", async route => {
  const req = route.request(), pathname = new URL(req.url()).pathname.replace("/api/v1", "");
  if (pathname === "/session" || pathname === "/healthz") return route.continue();
  if (pathname === "/state") return route.fulfill({ json: state() });
  if (pathname === "/web/providers") return route.fulfill({ json: providers });
  if (pathname === "/model-connections") return route.fulfill({ json: connections });
  if (pathname === "/web/config" && req.method() === "GET") return route.fulfill({ json: config });
  if (pathname === "/web/config" && req.method() === "DELETE") { config = { provider: "deepseek", configured: false, enabled: false, revision: 4 }; return route.fulfill({ json: config }); }
  if (pathname === "/web/config/validate") {
    const body = req.postDataJSON(); writes.push(body); const selected = outcome;
    await new Promise(resolve => setTimeout(resolve, validationDelay));
    const saved = selected === "ADDED" || selected === "UPDATED";
    if (saved) config = { provider: body.provider, provider_label: providers.find(item => item.id === body.provider).name, configured: true, enabled: body.enabled, credential_mode: body.credential_mode, connection_id: body.connection_id, engine: body.engine, remember: body.remember_key, storage: body.credential_mode === "CONNECTION" ? "MODEL_CONNECTION" : "SESSION_ONLY", api_key_mask: "••••••••", revision: (config.revision || 0) + 1, credential_store_available: false };
    return route.fulfill({ json: { status: selected, saved, tested: selected !== "TEST_FAILED", elapsed_ms: 200, message: saved ? "联网来源已测试并保存。" : "所选服务鉴权失败，请核对 Key。", ...(saved ? { config } : {}) } });
  }
  if (pathname.endsWith("/updates")) return route.fulfill({ json: { messages: [{ id: "human-fixture", speaker: "USER", text: "怎样核实公开信息？", created_at: "2026-11-01T01:00:00Z", sequence: 1, reply_state: "REPLIED" }, { id: "reply-fixture", speaker: "AGENT", text: "先把资料出处看准。", created_at: "2026-11-01T01:00:01Z", sequence: 2, research_id: "source-fixture", source_refs: sources.map(item => item.id) }], reset: true, revision: 1, batch: null } });
  if (pathname === "/research-runs/source-fixture/sources") return route.fulfill({ json: { research_id: "source-fixture", sources } });
  return route.fulfill({ json: {} });
});
async function go(hash) { await page.goto(`${base}/#${hash}`); await page.locator(".connection-state").waitFor({ state: "hidden" }); }
async function open() { await page.getByRole("button", { name: /^(配置|修改)联网配置$|^配置联网查询$/ }).click(); const dialog = page.getByRole("dialog", { name: "联网查询", exact: true }); await dialog.getByLabel("联网来源", { exact: true }).waitFor(); await page.waitForFunction(() => document.querySelector('.web-config-form select')?.options.length === 5); return dialog; }
async function capture(name) { const file = `${name}.png`; await page.screenshot({ path: path.join(artifacts, file), fullPage: true }); screenshots.push(file); }
async function widths(name) { for (const width of [1440, 768, 390, 360]) { await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 }); const measured = await page.evaluate(() => ({ width: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth })); assert.ok(measured.document <= width + 1 && measured.body <= width + 1, JSON.stringify(measured)); await capture(`${name}-${width}`); } }
try {
  await go("settings"); let dialog = await open();
  assert.equal(await dialog.getByLabel("联网来源", { exact: true }).inputValue(), "deepseek");
  assert.equal(await dialog.getByLabel("已保存的模型连接", { exact: true }).inputValue(), "deepseek-saved");
  assert.equal(await dialog.locator('input[type="password"]').count(), 0);
  assert.equal(writes.length, 0, "Opening must never test the saved Key automatically.");
  await widths("deepseek-reuse");
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "测试失败", exact: true }).waitFor();
  assert.deepEqual(writes[0], { provider: "deepseek", credential_mode: "CONNECTION", connection_id: "deepseek-saved", enabled: true });
  assert.equal(await dialog.getByLabel("已保存的模型连接", { exact: true }).inputValue(), "deepseek-saved");
  outcome = "ADDED";
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "联网查询已添加", exact: true }).waitFor();
  assert.ok(await dialog.getByText("复用已保存的模型连接 Key。窗口关闭后可在聊天中使用。", { exact: true }).isVisible());
  await widths("deepseek-reuse-saved");
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  dialog = await open();
  assert.equal(await dialog.getByLabel("联网来源", { exact: true }).inputValue(), "deepseek");
  assert.equal(await dialog.locator('input[type="password"]').count(), 0);
  checks.push("首次配置推荐 DeepSeek 并可复用已有连接；打开不调用服务，手动测试仅发送连接引用，失败保留选择，成功由用户关闭，重新打开不回读 Key。");

  await dialog.getByLabel("联网来源", { exact: true }).selectOption("kimi");
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "");
  assert.ok(await dialog.getByRole("radio", { name: /复用已保存的模型连接/ }).isDisabled());
  await dialog.getByLabel("搜索 API Key", { exact: true }).fill("sk-kimi-fixture-never-persisted");
  outcome = "TEST_FAILED";
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "测试失败", exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "sk-kimi-fixture-never-persisted");
  const beforeSwitch = writes.length;
  await dialog.getByLabel("联网来源", { exact: true }).selectOption("tavily");
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "");
  await dialog.getByLabel("搜索 API Key", { exact: true }).fill("sk-wrong-provider-fixture");
  assert.ok(await dialog.getByText(/当前选择的是 Tavily，请填写 Tavily Key/).isVisible());
  assert.ok(await dialog.getByRole("button", { name: "测试并保存", exact: true }).isDisabled());
  assert.equal(writes.length, beforeSwitch);
  await widths("wrong-provider-key");
  await dialog.getByLabel("联网来源", { exact: true }).selectOption("glm");
  assert.equal(await dialog.getByLabel("已保存的模型连接", { exact: true }).inputValue(), "glm-saved");
  assert.equal(await dialog.getByLabel("搜索引擎", { exact: true }).inputValue(), "search_std");
  assert.equal(await dialog.getByLabel("已保存的模型连接", { exact: true }).locator("option").count(), 1);
  await dialog.getByLabel("联网来源", { exact: true }).selectOption("bocha");
  assert.equal(await dialog.getByRole("radio").count(), 0);
  await dialog.getByLabel("搜索 API Key", { exact: true }).fill("bocha-fixture-only");
  outcome = "UPDATED";
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "联网查询已更新", exact: true }).waitFor();
  assert.equal(writes.at(-1).provider, "bocha"); assert.equal(writes.at(-1).credential_mode, "SEPARATE");
  assert.equal(writes.at(-1).connection_id, undefined);
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  checks.push("Kimi、智谱与博查按注册能力展示；失效连接不能复用，连接只匹配同供应商，切换清空 Key/结果，明显错误服务 Key 在本地阻止发送。");

  config = { provider: "tavily", configured: true, enabled: true, revision: 6, credential_mode: "SEPARATE", storage: "OS_CREDENTIAL_STORE", remember: true, api_key_mask: "••••••••" };
  await go("settings"); await page.reload(); dialog = await open();
  assert.equal(await dialog.getByLabel("联网来源", { exact: true }).inputValue(), "tavily");
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).count(), 0);
  await dialog.getByRole("button", { name: "更换搜索 Key", exact: true }).click();
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "");
  await dialog.getByRole("button", { name: "关闭联网查询", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1050 });
  await go("chat");
  await page.getByRole("button", { name: "查看依据", exact: true }).click();
  const sourceDialog = page.getByRole("dialog", { name: "本轮参考资料", exact: true });
  await sourceDialog.getByText("网页正文摘录", { exact: true }).waitFor();
  assert.ok(await sourceDialog.getByText("搜索摘要", { exact: true }).isVisible());
  assert.ok(await sourceDialog.getByText("供应商联网总结", { exact: true }).isVisible());
  await widths("source-kinds");
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  assert.ok(!storage.includes("fixture-never-persisted") && !storage.includes("bocha-fixture-only"));
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  checks.push("已有 Tavily 配置保留、原始 Key 不回读；来源区分网页正文、搜索摘要与供应商总结；四种宽度无横向溢出，Key 不写入浏览器存储。");
  await writeFile(path.join(artifacts, "report.json"), JSON.stringify({ status: "PASSED", date: new Date().toISOString(), checks, screenshots, browserErrors: errors, externalRequests: external, fixtureValidationCalls: writes.length, realModelCalls: 0, realSearchCalls: 0, limitations: ["本报告使用本地界面夹具验证配置与来源展示，不证明供应商可用性或人物拟真；真实协议由后端契约测试覆盖。"] }, null, 2));
  console.log(`PASSED ${checks.length} groups, ${screenshots.length} screenshots; no real model/search calls.`);
} catch (error) {
  await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true }).catch(() => {});
  console.error(await page.locator("dialog").allTextContents());
  throw error;
} finally { await context.close(); await browser.close(); }
