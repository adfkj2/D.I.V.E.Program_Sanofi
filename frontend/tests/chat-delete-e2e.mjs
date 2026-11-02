import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8771";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true);
const initial = await (await fetch(`${base}/api/v1/state`)).json();
const artifacts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../artifacts/domestic-web-e2e");
await mkdir(artifacts, { recursive: true });
const people = [initial.skills.find(person => person.slug === "zhangxuefeng"), initial.skills.find(person => person.slug === "munger")];
assert.ok(people.every(Boolean));
const rooms = people.map((person, index) => ({ id: `delete-review-${index}`, skill_id: person.id, title: index ? "保留的会话B" : "待删除的会话A", created_at: index ? "2026-11-01T01:00:00Z" : "2026-11-02T01:00:00Z" }));
let deleted = false, staleSidebar = false, externalDeleted = false, releaseDelete, requestDelete, stateReads = 0;
const deleteStarted = new Promise(resolve => { requestDelete = resolve; });
const deleteRelease = new Promise(resolve => { releaseDelete = resolve; });
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
const page = await context.newPage(), errors = [], external = [], writes = [], screenshots = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", request => { if (!request.url().startsWith(base) && !request.url().startsWith("data:")) external.push(request.url()); });
await page.route("**/assets/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
await page.route("**/api/v1/**", async route => {
  const req = route.request(), url = new URL(req.url()), pathname = url.pathname.replace("/api/v1", "");
  if (["/session", "/healthz"].includes(pathname)) return route.continue();
  if (pathname === "/state") { stateReads += 1; return route.fulfill({ json: { ...initial, conversations: deleted && !staleSidebar ? [rooms[1]] : rooms, upgrade: { ...initial.upgrade, preferences: { setup_dismissed: true } } } }); }
  if (pathname === `/real-conversations/${rooms[0].id}` && req.method() === "DELETE") { writes.push(pathname); requestDelete(); await deleteRelease; deleted = true; return route.fulfill({ json: { id: rooms[0].id, deleted: true } }); }
  if (pathname.endsWith("/updates")) {
    const room = pathname.includes(rooms[0].id) ? rooms[0] : rooms[1];
    if (room.id === rooms[1].id && externalDeleted) return route.fulfill({ status: 410, json: { detail: "这段会话已被删除" } });
    return route.fulfill({ json: { messages: [{ id: `${room.id}-message`, speaker: "AGENT", text: room.id === rooms[0].id ? "仅属于A的私密测试消息" : "仅属于B的保留测试消息", created_at: room.created_at, sequence: 1 }], revision: 1, reset: true, batch: null } });
  }
  if (pathname === "/chat-presence") return route.fulfill({ json: { ok: true } });
  if (req.method() !== "GET") throw new Error(`Unexpected mutation: ${req.method()} ${pathname}`);
  return route.fulfill({ json: [] });
});
const composer = () => page.locator(".im-composer textarea");
const recent = index => page.getByTestId("recent-row").filter({ hasText: rooms[index].title });
async function capture(name) { const file = `${name}.png`; await page.screenshot({ path: path.join(artifacts, file), fullPage: true }); screenshots.push(file); }
try {
  await page.goto(`${base}/#chat`);
  await page.getByRole("tab", { name: /最近对话/ }).click();
  await recent(0).click(); await page.getByText("仅属于A的私密测试消息", { exact: true }).waitFor();
  await composer().fill("A的未发送草稿");
  await recent(1).click(); await page.getByText("仅属于B的保留测试消息", { exact: true }).waitFor();
  await composer().fill("B的草稿应当保留");
  await page.getByRole("button", { name: `${rooms[0].title}的更多操作`, exact: true }).click();
  await page.getByRole("menuitem", { name: "删除会话", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "删除这段会话", exact: true });
  await dialog.getByRole("button", { name: "删除会话", exact: true }).click();
  await deleteStarted;
  // Delete A while B is current; delayed completion must not select A or wipe B.
  releaseDelete();
  await dialog.waitFor({ state: "hidden" });
  assert.equal(await composer().inputValue(), "B的草稿应当保留");
  assert.equal(await recent(0).count(), 0);
  assert.ok(await page.getByText("仅属于B的保留测试消息", { exact: true }).isVisible());
  await capture("chat-delete-keeps-b-1440");
  await page.getByRole("tab", { name: /^人物/ }).click();
  await page.getByRole("button", { name: `与${people[0].display_name}聊天`, exact: true }).click();
  assert.equal(await composer().inputValue(), "");
  assert.equal(await page.getByText("仅属于A的私密测试消息", { exact: true }).count(), 0);
  await composer().fill("A的新话题草稿");
  staleSidebar = true; const before = stateReads;
  while (stateReads === before) await page.waitForTimeout(100);
  await page.getByRole("tab", { name: /最近对话/ }).click();
  assert.equal(await recent(0).count(), 0, "A stale sidebar snapshot cannot restore a deleted room.");
  await recent(1).click();
  assert.equal(await composer().inputValue(), "B的草稿应当保留");
  externalDeleted = true;
  await page.getByText("仅属于B的保留测试消息", { exact: true }).waitFor({ state: "hidden" });
  assert.equal(await composer().inputValue(), "");
  assert.equal(await recent(1).count(), 0);
  await page.getByRole("tab", { name: /^人物/ }).click();
  await page.getByRole("button", { name: `与${people[0].display_name}聊天`, exact: true }).click();
  assert.equal(await composer().inputValue(), "A的新话题草稿", "Deleting B in another window must not clear A's new-topic draft.");
  await page.setViewportSize({ width: 390, height: 844 });
  await capture("chat-delete-external-390");
  assert.equal(writes.length, 1); assert.deepEqual(errors, []); assert.deepEqual(external, []);
  await writeFile(path.join(artifacts, "chat-delete-report.json"), JSON.stringify({ status: "PASSED", checks: ["延迟删除非当前会话A不会改变当前B或清空B草稿，删除A后新话题无旧草稿/消息。", "迟到的侧栏快照不恢复已删除会话；另一窗口删除B后410触发缓存清除，其他新话题草稿保留。"], screenshots, browserErrors: errors, externalRequests: external, realModelCalls: 0, realSearchCalls: 0, limitation: "删除、轮询和状态为本机浏览器夹具；不触发真实模型、搜索或私人数据修改。" }, null, 2));
  console.log("PASS: chat deletion and external deletion browser races; no real calls.");
} finally { releaseDelete(); await context.close(); await browser.close(); }
