import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8767";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true);
const artifacts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../artifacts/research-e2e");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
const page = await context.newPage(); const errors = [], external = [], screenshots = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", req => { if (!req.url().startsWith(base) && !req.url().startsWith("data:")) external.push(req.url()); });
await page.route("**/assets/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
// Real API routes; only the server's outbound Tavily client is simulated.
await page.route("**/api/v1/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
const token = (await (await context.request.get(`${base}/api/v1/session`)).json()).token;
const getConfig = async () => (await context.request.get(`${base}/api/v1/web/config`)).json();
const secret = "e2e-config-live-route-key-not-real";
try {
  await page.goto(`${base}/#settings`);
  await page.getByRole("button", { name: "配置联网查询", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  await dialog.getByLabel("联网来源", { exact: true }).selectOption("tavily");
  const input = dialog.getByLabel("搜索 API Key", { exact: true });
  await input.fill(secret);
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  assert.equal(await input.inputValue(), secret);
  await dialog.getByRole("heading", { name: "测试失败", exact: true }).waitFor();
  assert.equal(await input.inputValue(), secret); assert.equal((await getConfig()).configured, false);
  assert.ok(!(await dialog.getByLabel("联网配置结果").textContent()).includes(secret));
  await context.request.post(`${base}/api/v1/e2e/research-mode`, { headers: { "X-WOT-Token": token }, data: { mode: "SUCCESS" } });
  await dialog.getByLabel("记住搜索 Key，保存在系统安全凭据库", { exact: true }).check();
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "连接成功，但保存失败", exact: true }).waitFor();
  assert.equal(await input.inputValue(), secret); assert.equal((await getConfig()).configured, false);
  await dialog.getByLabel("记住搜索 Key，保存在系统安全凭据库", { exact: true }).uncheck();
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "联网查询已添加", exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).count(), 0);
  const config = await getConfig(); assert.equal(config.enabled, true); assert.equal(config.storage, "SESSION_ONLY"); assert.ok(!JSON.stringify(config).includes(secret));
  for (const width of [1440, 390]) { await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 }); const name = `web-config-real-route-${width}.png`; await page.screenshot({ path: path.join(artifacts, name), fullPage: true }); screenshots.push(name); }
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "修改联网配置", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).count(), 0);
  await dialog.getByRole("button", { name: "更换搜索 Key", exact: true }).click();
  assert.equal(await dialog.getByLabel("搜索 API Key", { exact: true }).inputValue(), "");
  await dialog.getByRole("button", { name: "关闭联网查询", exact: true }).click();
  await page.getByRole("button", { name: "移除联网配置", exact: true }).click();
  await page.getByRole("button", { name: "配置联网查询", exact: true }).waitFor();
  assert.equal((await getConfig()).configured, false);
  const dataDir = process.env.WOT_E2E_DATA_DIR;
  if (dataDir) { const database = await readFile(path.resolve(dataDir, "wot.db")); assert.ok(!database.includes(Buffer.from(secret))); }
  assert.ok(!JSON.stringify(await context.storageState()).includes(secret));
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  const counts = await (await context.request.post(`${base}/api/v1/e2e/research-count`, { headers: { "X-WOT-Token": token }, data: {} })).json(); assert.equal(counts.mock_search_calls, 3); assert.equal(counts.real_search_calls, 0); assert.equal(counts.real_model_calls, 0);
  await writeFile(path.join(artifacts, "config-real-route-report.json"), JSON.stringify({ status: "PASSED", checks: ["真实本机配置路由完成鉴权失败、系统凭据保存失败、仅本次运行成功与删除；失败保留Key、成功显示掩码、重新打开不回读。", "搜索Key未进入SQLite正文、浏览器存储或公开配置；供应商回显的Key未进入错误提示。"], ...counts, screenshots, browserErrors: errors, externalRequests: external, limitation: "仅Tavily出站请求使用进程内MockTransport；本机API、表单、保存和删除均走真实实现。" }, null, 2));
  console.log("Real-route web configuration browser checks PASSED");
} finally { await browser.close(); }
