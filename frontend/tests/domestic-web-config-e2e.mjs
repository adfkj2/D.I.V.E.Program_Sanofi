import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8769";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true);
const artifacts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../artifacts/domestic-web-e2e");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
const page = await context.newPage(), errors = [], external = [], writes = [], screenshots = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", req => { if (!req.url().startsWith(base) && !req.url().startsWith("data:")) external.push(req.url()); if (req.url().endsWith("/web/config/validate")) writes.push(req.postDataJSON()); });
await page.route("**/assets/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
await page.route("**/api/v1/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
const token = (await (await context.request.get(`${base}/api/v1/session`)).json()).token;
const headers = { "X-WOT-Token": token };
async function config() { return (await context.request.get(`${base}/api/v1/web/config`)).json(); }
try {
  const countBefore = await (await context.request.post(`${base}/api/v1/e2e/domestic-count`, { headers, data: {} })).json();
  assert.equal(countBefore.mock_search_calls, 0);
  await page.goto(`${base}/#settings`);
  await page.getByRole("button", { name: "配置联网查询", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  await dialog.getByLabel("已保存的模型连接", { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("联网来源", { exact: true }).inputValue(), "deepseek");
  assert.equal(await dialog.locator('input[type="password"]').count(), 0);
  assert.equal((await (await context.request.post(`${base}/api/v1/e2e/domestic-count`, { headers, data: {} })).json()).mock_search_calls, 0);
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "测试失败", exact: true }).waitFor();
  assert.equal((await config()).configured, false);
  assert.equal(writes[0].credential_mode, "CONNECTION"); assert.equal(writes[0].provider, "deepseek");
  assert.equal(writes[0].api_key, undefined); assert.ok(writes[0].connection_id);
  assert.ok(!(await dialog.textContent()).includes("sk-domestic-model-fixture"));
  await context.request.post(`${base}/api/v1/e2e/domestic-mode`, { headers, data: { mode: "SUCCESS" } });
  await dialog.getByRole("button", { name: "测试并保存", exact: true }).click();
  await dialog.getByRole("heading", { name: "联网查询已添加", exact: true }).waitFor();
  const saved = await config(); assert.equal(saved.provider, "deepseek"); assert.equal(saved.credential_mode, "CONNECTION"); assert.equal(saved.enabled, true); assert.equal(saved.configured, true);
  assert.ok(!JSON.stringify(saved).includes("sk-domestic-model-fixture"));
  for (const width of [1440, 390]) { await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 }); const file = `deepseek-reuse-real-route-${width}.png`; await page.screenshot({ path: path.join(artifacts, file), fullPage: true }); screenshots.push(file); }
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "修改联网配置", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "联网查询", exact: true });
  assert.equal(await dialog.locator('input[type="password"]').count(), 0);
  await dialog.getByLabel("已保存的模型连接", { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("联网来源", { exact: true }).inputValue(), "deepseek");
  await dialog.getByRole("button", { name: "关闭联网查询", exact: true }).click();
  await page.getByRole("button", { name: "移除联网配置", exact: true }).click();
  await page.getByRole("button", { name: "配置联网查询", exact: true }).waitFor();
  assert.equal((await config()).configured, false);
  const modelConnections = await (await context.request.get(`${base}/api/v1/model-connections`)).json();
  assert.equal(modelConnections.find(item => item.provider === "deepseek").key_status, "CONFIGURED", "Removing lookup must not delete shared model Key.");
  assert.ok(!JSON.stringify(await context.storageState()).includes("sk-domestic-model-fixture"));
  if (process.env.WOT_E2E_DATA_DIR) { const body = await readFile(path.resolve(process.env.WOT_E2E_DATA_DIR, "wot.db")); assert.ok(!body.includes(Buffer.from("sk-domestic-model-fixture"))); }
  const counts = await (await context.request.post(`${base}/api/v1/e2e/domestic-count`, { headers, data: {} })).json();
  assert.equal(counts.mock_search_calls, 2); assert.deepEqual(counts.providers_called, ["api.deepseek.com"]);
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  await writeFile(path.join(artifacts, "real-route-report.json"), JSON.stringify({ status: "PASSED", checks: ["真实配置窗口读取境内服务清单，默认 DeepSeek 且复用连接；打开不自动测试，不传原始模型 Key。", "鉴权失败不保存；成功后保存 CONNECTION 引用，重开不回读，移除不删除共享模型 Key；浏览器及SQLite无原始 Key。"], screenshots, ...counts, browserErrors: errors, externalRequests: external, limitation: "本机配置接口与表单为真实实现；仅供应商出站请求使用进程内模拟服务，不证明真实搜索质量。" }, null, 2));
  console.log("Domestic real-route web configuration PASSED; 2 mock DeepSeek searches, no real calls.");
} finally { await context.close(); await browser.close(); }
