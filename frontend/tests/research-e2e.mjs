import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

// Browser fixtures validate UI only; no search/model credential is sent to a provider.
const here = path.dirname(fileURLToPath(import.meta.url));
const artifacts = path.resolve(here, "../artifacts/research-e2e");
const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8766";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true, "Use an isolated e2e server.");
const initial = await (await fetch(`${base}/api/v1/state`)).json();
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
const page = await context.newPage();
// The production local guard permits 8765/5173. Isolated visual fixtures use a
// spare port; preserve the production origin for crossorigin static assets.
await page.route("**/assets/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
const errors = [], external = [], checks = [], screenshots = [], writes = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", req => { if (!req.url().startsWith(base) && !req.url().startsWith("data:")) external.push(req.url()); });
let config = { enabled: false, configured: false, key_status: "MISSING", provider: "tavily", credential_store_available: false };
let outcome = "TEST_FAILED", validationCalls = 0, sourceCalls = 0, validationDelay = 250;
let phase = "SEARCHING", researchStatus = "COMPLETE", researchNotice = "";
let conversationWeb = true;
let deletedReply = false;
const character = initial.skills.find(person => person.slug === "zhangxuefeng");
assert.ok(character);
const other = initial.skills.find(person => person.slug === "buffett");
const conversation = { id: "conversation-research-e2e", title: "河南物理组志愿怎么选", skill_id: character.id, created_at: "2026-11-01T01:00:00Z", web_enabled: true };
const runId = "research-ui-fixture";
const user = { id: "message-user-e2e", speaker: "USER", text: "河南，2026 年，物化生，580 分，位次三万左右，普通本科批。我更想学工科。", created_at: "2026-11-01T01:00:00Z", sequence: 1, reply_state: "REPLIED" };
const reply = { id: "message-agent-e2e", speaker: "AGENT", text: "先把位次看准，别光盯着分数。你想学工科，那咱们得把专业组和选科要求一块看，再留出冲、稳、保的余地。", created_at: "2026-11-01T01:00:02Z", sequence: 2, research_id: runId, source_refs: ["source-official-e2e", "source-invalid-e2e"] };
const sources = [{ id: "source-official-e2e", title: "河南省普通高校招生计划与投档说明（界面测试资料）", url: "https://www.example.edu.cn/admissions/2026", publisher: "省教育考试院 · 界面夹具", applicable_year: 2026, published_at: "2026-06-20T02:00:00Z", retrieved_at: "2026-11-01T02:30:00Z", excerpt: "示例资料用于检查来源展示，不是真实报考依据。应按招生年份、科类与专业组核对投档位次，不能把往年最低分当作今年的录取保证。\n\n当前网页读取结果应保留完整段落和表格行。" }, { id: "source-invalid-e2e", title: "无效地址测试", url: "javascript:alert(1)", publisher: "地址不可点击", retrieved_at: "2026-11-01T02:30:00Z", excerpt: "这个恶意地址必须只显示文字，不能成为链接。" }];
const participants = [{ id: character.id, version_id: "zhang-voice-e2e", kind: "CHARACTER", display_name: character.display_name, avatar_url: character.avatar_url }, { id: other.id, version_id: "buffett-voice-e2e", kind: "CHARACTER", display_name: other.display_name, avatar_url: other.avatar_url }];
let virtual = { id: "virtual-research-e2e", topic: "当下的报考信息，怎样核实", state: "GENERATING", participants, turns: [{ id: "vturn-research-e2e", speaker_id: character.id, display_name: character.display_name, avatar_url: character.avatar_url, text: reply.text, is_generated: true, turn_index: 1, version_id: "zhang-voice-e2e", research_id: runId, source_refs: ["source-official-e2e"] }], used_tokens: 520, max_tokens: 30000, max_turns: 20, web_enabled: true, max_web_requests: 20, used_web_requests: 2, typing: true, typing_speaker_id: other.id, typing_display_name: other.display_name, revision: 1, research_phase: "SEARCHING" };
function state() { return { ...structuredClone(initial), conversations: [{ ...conversation, web_enabled: conversationWeb }], upgrade: { ...initial.upgrade, connections: [{ id: "model-ui-fixture", provider: "openai", model: "UI fixture only", key_status: "CONFIGURED", storage: "SESSION_ONLY", is_default: true }], preferences: { setup_dismissed: true }, web: config, virtual_sessions: [virtual] } }; }
await page.route("**/api/v1/**", async route => {
  const req = route.request(), url = new URL(req.url()), pathname = url.pathname.replace("/api/v1", "");
  if (pathname === "/session" || pathname === "/healthz") return route.continue();
  if (req.method() !== "GET") writes.push({ path: pathname, body: req.postDataJSON() });
  if (pathname === "/state") return route.fulfill({ json: state() });
  if (pathname === "/web/providers") return route.fulfill({ json: [{ id: "deepseek", name: "DeepSeek 搜索", reuse_provider: "deepseek", key_hint: "填入 DeepSeek API Key" }, { id: "tavily", name: "Tavily", key_hint: "填入 Tavily API Key" }] });
  if (pathname === "/model-connections") return route.fulfill({ json: state().upgrade.connections });
  if (pathname === "/web/config" && req.method() === "GET") return route.fulfill({ json: config });
  if (pathname === "/web/config" && req.method() === "DELETE") { config = { enabled: false, configured: false, key_status: "MISSING" }; return route.fulfill({ json: config }); }
  if (pathname === "/web/config/validate") {
    validationCalls++; const submitted = req.postDataJSON(); const selected = outcome; await new Promise(resolve => setTimeout(resolve, validationDelay));
    const saved = ["ADDED", "UPDATED"].includes(selected);
    if (saved) config = { provider: "tavily", enabled: submitted.enabled, configured: true, key_status: "CONFIGURED", api_key_mask: "••••••••", storage: "SESSION_ONLY", remember: false, credential_store_available: false };
    return route.fulfill({ json: { status: selected, tested: selected !== "TEST_FAILED", saved, success: saved, elapsed_ms: 316, message: saved ? "联网已通过测试并保存。" : selected === "SAVE_FAILED" ? "系统安全凭据库暂不可用，请选择仅本次运行。" : "鉴权失败，请检查搜索 Key。", ...(saved ? { config } : {}) } });
  }
  if (pathname.endsWith("/updates")) return route.fulfill({ json: { messages: [user, deletedReply ? { ...reply, deleted: true, text: "[关联来源已删除]", research_id: null, source_refs: [] } : reply], revision: Date.now(), reset: true, batch: { id: "batch-research-e2e", status: phase ? "GENERATING" : "COMPLETED", typing: Boolean(phase), source_message_ids: [user.id], research_id: runId, research_phase: phase, research_status: researchStatus, research_notice: researchNotice } } });
  if (pathname === `/real-conversations/${conversation.id}/web`) { conversationWeb = req.postDataJSON().enabled; return route.fulfill({ json: { ...conversation, web_enabled: conversationWeb } }); }
  if (pathname === `/research-runs/${runId}/sources`) { sourceCalls++; return route.fulfill({ json: { research_id: runId, sources } }); }
  if (pathname === "/virtual-participants") return route.fulfill({ json: participants });
  if (pathname === `/virtual-sessions/${virtual.id}/web`) { virtual = { ...virtual, web_enabled: req.postDataJSON().enabled, revision: virtual.revision + 1 }; return route.fulfill({ json: virtual }); }
  if (pathname === `/virtual-sessions/${virtual.id}/budget`) { const body = req.postDataJSON(); virtual = { ...virtual, max_turns: virtual.max_turns + body.add_messages, max_tokens: virtual.max_tokens + body.add_tokens, max_web_requests: virtual.max_web_requests + body.add_web_requests, revision: virtual.revision + 1 }; return route.fulfill({ json: virtual }); }
  if (pathname === `/virtual-sessions/${virtual.id}`) return route.fulfill({ json: virtual });
  if (pathname === "/virtual-sessions") return route.fulfill({ json: virtual });
  if (pathname === "/chat-presence") return route.fulfill({ json: { accepted: true } });
  return route.fulfill({ json: {} });
});
async function go(hash) { await page.goto(`${base}/#${hash}`); await page.locator(".connection-state").waitFor({ state: "hidden" }); }
async function capture(name) { const file = `${name}.png`; await page.screenshot({ path: path.join(artifacts, file), fullPage: true }); screenshots.push(file); }
async function noOverflow() { const metrics = await page.evaluate(() => ({ width: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth })); assert.ok(metrics.document <= metrics.width + 1 && metrics.body <= metrics.width + 1, JSON.stringify(metrics)); }
async function widths(name, check) { for (const width of [1440, 768, 390, 360]) { await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 }); await noOverflow(); if (check) await check(width); await capture(`${name}-${width}`); } }
try {
  await go("settings");
  await page.getByRole("button", { name: "配置联网查询", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  await dialog.getByLabel("联网来源", { exact: true }).selectOption("tavily");
  const keyInput = dialog.getByLabel("搜索 API Key", { exact: true });
  await keyInput.fill("e2e-search-key-never-stored");
  const firstCalls = validationCalls;
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  assert.equal(await keyInput.inputValue(), "e2e-search-key-never-stored");
  await dialog.getByRole("heading", { name: "测试失败", exact: true }).waitFor();
  assert.equal(validationCalls, firstCalls + 1); assert.equal(await keyInput.inputValue(), "e2e-search-key-never-stored");
  outcome = "SAVE_FAILED";
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "连接成功，但保存失败", exact: true }).waitFor();
  assert.equal(await keyInput.inputValue(), "e2e-search-key-never-stored");
  outcome = "TEST_FAILED"; validationDelay = 600;
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await keyInput.fill("e2e-replacement-search-key");
  await dialog.getByText("内容已修改，请重新测试并保存。", { exact: true }).waitFor();
  assert.equal(await dialog.getByRole("heading", { name: "测试失败", exact: true }).count(), 0);
  validationDelay = 250; outcome = "ADDED";
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "联网查询已添加", exact: true }).waitFor();
  assert.equal(config.enabled, true, "First successful configuration defaults to automatic research.");
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).count(), 0);
  assert.ok(await dialog.getByLabel("搜索 Key 状态", { exact: true }).getByText("仅本次运行有效", { exact: true }).isVisible());
  await widths("web-config-saved");
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "修改联网配置", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).count(), 0);
  await dialog.getByRole("button", { name: "更换搜索 Key", exact: true }).click();
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "");
  await dialog.getByRole("button", { name: "关闭联网查询", exact: true }).click();
  checks.push("搜索配置覆盖鉴权失败、保存失败、表单修改使迟到结果失效、成功后安全状态；Key等待与失败保留、重新打开不回读，成功由用户关闭。");

  await page.setViewportSize({ width: 1440, height: 1050 });
  await go("chat");
  await page.locator(`[data-testid="contact-row"][data-slug="zhangxuefeng"]`).click();
  await page.getByText("正在查资料…", { exact: true }).waitFor();
  assert.equal(sourceCalls, 0);
  await widths("chat-searching", async width => { if (width <= 760) { const input = await page.getByLabel("写下你想聊的话", { exact: true }).boundingBox(); assert.ok(input && input.y > 0 && input.y + input.height <= 844, JSON.stringify(input)); } });
  phase = "VERIFYING"; await page.getByText("正在核对资料…", { exact: true }).waitFor();
  await capture("chat-verifying-360");
  phase = "GENERATING"; await page.getByLabel("对方正在输入中...", { exact: true }).waitFor();
  phase = null; await page.getByLabel("对方正在输入中...", { exact: true }).waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "查看依据", exact: true }).click();
  await page.getByRole("dialog", { name: "本轮参考资料", exact: true }).waitFor();
  await page.getByText("资料发布", { exact: true }).first().waitFor();
  assert.equal(sourceCalls, 1);
  assert.equal(await page.locator('.web-dialog a[href^="javascript:"]').count(), 0);
  assert.equal(await page.locator('.web-dialog a[href="https://www.example.edu.cn/admissions/2026"]').count(), 1);
  await widths("chat-sources");
  deletedReply = true;
  await page.getByRole("dialog", { name: "本轮参考资料", exact: true }).waitFor({ state: "hidden" });
  assert.equal(await page.getByRole("button", { name: "查看依据", exact: true }).count(), 0);
  await page.getByLabel("联网", { exact: true }).uncheck(); assert.equal(conversationWeb, false);
  researchStatus = "NOT_CONFIGURED"; researchNotice = "联网查询尚未配置，当前回复无法核实最新资料。";
  await page.getByRole("button", { name: "配置联网查询", exact: true }).waitFor();
  await widths("chat-web-notice", async () => { const box = await page.getByLabel("写下你想聊的话", { exact: true }).boundingBox(); assert.ok(box && box.y >= 0 && box.y + box.height <= (page.viewportSize().height), JSON.stringify(box)); });
  await page.getByRole("button", { name: "配置联网查询", exact: true }).click();
  await page.getByRole("heading", { name: "联网查询", exact: true }).waitFor();
  checks.push("聊天按真实阶段展示查资料/核对/输入；来源默认收起，显式打开才读取；恶意链接不可点击；关联消息删除后来源窗口与缓存同步清除；会话联网开关及缺少配置引导可用，输入框保持可见。");

  await page.setViewportSize({ width: 1440, height: 1050 });
  await go("virtual");
  await page.locator(".up-session-row").first().click();
  await page.getByText("正在查资料…", { exact: true }).waitFor();
  await page.getByRole("button", { name: "查看依据", exact: true }).click();
  await page.getByRole("dialog", { name: "本轮参考资料", exact: true }).waitFor();
  await page.getByText("资料发布", { exact: true }).waitFor();
  await widths("virtual-sources");
  await page.getByRole("button", { name: "关闭资料", exact: true }).click();
  virtual = { ...virtual, state: "PAUSED_LIMIT", pause_reason: "WEB_REQUEST_LIMIT", research_phase: null, typing: false, used_web_requests: 20, revision: virtual.revision + 1 };
  await page.getByRole("heading", { name: "本场联网检索额度已用完", exact: true }).waitFor();
  assert.equal(await page.getByLabel("新增消息数", { exact: true }).inputValue(), "0");
  assert.equal(await page.getByLabel("新增 token", { exact: true }).inputValue(), "0");
  assert.equal(await page.getByLabel("新增检索请求", { exact: true }).inputValue(), "20");
  await widths("virtual-web-limit");
  await page.getByRole("button", { name: "追加额度", exact: true }).click();
  await page.getByRole("heading", { name: "额度已补充，点击继续接着聊", exact: true }).waitFor();
  assert.equal(virtual.state, "PAUSED_LIMIT"); assert.equal(virtual.max_web_requests, 40);
  assert.ok(writes.some(write => write.path.endsWith("/budget") && write.body.add_web_requests === 20 && write.body.add_tokens === 0 && write.body.add_messages === 0));
  await page.getByLabel("联网", { exact: true }).uncheck();
  assert.equal(virtual.web_enabled, false);
  checks.push("虚拟空间复用检索阶段与来源展示；检索额度耗尽单独暂停，默认只追加检索请求，追加不自动恢复，会话联网开关生效。");

  const storage = JSON.stringify(await context.storageState()) + await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  assert.ok(!storage.includes("e2e-search-key-never-stored") && !storage.includes("e2e-replacement-search-key"));
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  await writeFile(path.join(artifacts, "report.json"), JSON.stringify({ status: "PASSED", date: new Date().toISOString(), checks, screenshots, browserErrors: errors, externalRequests: external, realSearchCalls: 0, realModelCalls: 0, fixtureValidationCalls: validationCalls, fixtureSourceReads: sourceCalls, limitations: ["本报告使用浏览器网络夹具验证界面和交互，不证明真实资料正确性或人物拟真。", "后端联网、权限、账本与删除传播由独立单元/契约测试覆盖；未使用用户保存的 Key。"] }, null, 2));
  console.log(JSON.stringify({ status: "PASSED", checks: checks.length, screenshots: screenshots.length }));
} catch (error) { await capture("failure").catch(() => {}); await writeFile(path.join(artifacts, "report.json"), JSON.stringify({ status: "FAILED", checks, screenshots, browserErrors: errors, error: String(error) }, null, 2)); throw error; }
finally { await browser.close(); }
