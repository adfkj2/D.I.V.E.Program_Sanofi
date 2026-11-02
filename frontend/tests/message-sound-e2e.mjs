import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

// The browser only reads static files from an isolated test server. All app
// state, publications and mutations below are local route fixtures.
const base = process.env.WOT_E2E_URL || "http://127.0.0.1:8773";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname));
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server, true);
const initial = await (await fetch(`${base}/api/v1/state`)).json();
const artifacts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../artifacts/message-sound-e2e");
await mkdir(artifacts, { recursive: true });
await writeFile(path.join(artifacts, ".gitignore"), "data-e2e*/\n*.log\n");
const browser = await chromium.launch({ headless: true, channel: process.env.WOT_E2E_CHANNEL || undefined });
const errors = [], external = [], checks = [], screenshots = [];
const people = [initial.skills.find(person => person.slug === "zhangxuefeng"), initial.skills.find(person => person.slug === "munger")];
assert.ok(people.every(Boolean));
const oldTime = new Date(Date.now() - 3600000).toISOString();
const rooms = people.map((person, index) => ({ id: `sound-room-${index}`, skill_id: person.id, title: index ? "另一段历史" : "提示音测试", created_at: index ? new Date(Date.now() - 7200000).toISOString() : oldTime }));
const logs = new Map(rooms.map((room, index) => [room.id, [
  { id: `${room.id}-old-user`, conversation_id: room.id, speaker: "USER", text: "历史里的问题", created_at: oldTime, sequence: 1, reply_state: "REPLIED" },
  { id: `${room.id}-old-agent`, conversation_id: room.id, speaker: "AGENT", text: `历史里的完整回答${index}`, created_at: oldTime, sequence: 2 },
]]));
const batches = new Map();
let revision = 1, created = 0;
const session = { id: "sound-virtual", topic: "两个人物的提示音测试", state: "PAUSED", participants: people.map((person, index) => ({ id: person.id, kind: "CHARACTER", version_id: `sound-person-${index}`, display_name: person.display_name, avatar_url: person.avatar_url })), turns: [], draft: null, published_count: 1, used_tokens: 100, max_turns: 20, max_tokens: 30000, max_web_requests: 20, used_web_requests: 0, revision: 1, created_at: oldTime, typing: false };
session.turns.push({ id: "virtual-old-turn", speaker_id: people[0].id, display_name: people[0].display_name, text: "虚拟空间的旧发言", turn_index: 0, is_generated: true, version_id: "sound-person-0", published_at: oldTime });
function state() { return { ...structuredClone(initial), conversations: [...rooms].reverse().sort((a, b) => b.created_at.localeCompare(a.created_at)), upgrade: { ...initial.upgrade, preferences: { setup_dismissed: true }, connections: [{ id: "sound-model", provider: "deepseek", model: "fixture-model", is_default: true, key_status: "CONFIGURED", storage: "SESSION_ONLY" }], virtual_sessions: [structuredClone(session)] } }; }
function add(room, speaker, text, fields = {}) { const messages = logs.get(room); const message = { id: `message-${++revision}`, conversation_id: room, speaker, text, created_at: new Date().toISOString(), sequence: messages.length + 1, reply_state: "REPLIED", ...fields }; messages.push(message); return message; }
async function instrument(context, unavailable = false) {
  await context.addInitScript(({ unavailable }) => {
    window.soundStarts = [];
    window.soundContexts = 0;
    window.soundVisibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => window.soundVisibility });
    if (unavailable) { Object.defineProperty(window, "AudioContext", { configurable: true, value: undefined }); Object.defineProperty(window, "webkitAudioContext", { configurable: true, value: undefined }); return; }
    class Param { constructor() { this.value = 0; } setValueAtTime(value) { this.value = value; } exponentialRampToValueAtTime() {} }
    class Oscillator {
      constructor(owner) { this.owner = owner; this.frequency = new Param(); this.onended = null; this.timer = null; }
      connect() {} disconnect() {}
      start(at) { window.soundStarts.push({ frequency: this.frequency.value, at }); }
      stop(at) { clearTimeout(this.timer); if (at !== undefined) this.timer = setTimeout(() => this.onended?.(), Math.max(0, at - this.owner.currentTime) * 1000); }
    }
    class Audio {
      constructor() { window.soundContexts += 1; this.state = "suspended"; this.started = performance.now(); this.destination = {}; this.onstatechange = null; window.soundAudio = this; }
      get currentTime() { return (performance.now() - this.started) / 1000; }
      resume() { this.state = "running"; this.onstatechange?.(); return Promise.resolve(); }
      close() { this.state = "closed"; return Promise.resolve(); }
      createOscillator() { return new Oscillator(this); }
      createGain() { return { gain: new Param(), connect() {}, disconnect() {} }; }
    }
    Object.defineProperty(window, "AudioContext", { configurable: true, value: Audio });
  }, { unavailable });
}
async function routes(page) {
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", req => { if (!req.url().startsWith(base) && !req.url().startsWith("data:")) external.push(req.url()); });
  await page.route("**/assets/**", async route => { const response = await route.fetch({ headers: { ...route.request().headers(), origin: "http://127.0.0.1:8765" } }); await route.fulfill({ response }); });
  await page.route("**/api/v1/**", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname.replace("/api/v1", "");
    if (["/session", "/healthz"].includes(pathname)) return route.continue();
    if (pathname === "/state") return route.fulfill({ json: state() });
    if (pathname.endsWith("/updates")) {
      const room = decodeURIComponent(pathname.split("/")[2]);
      const after = Number(url.searchParams.get("after_sequence") || 0);
      return route.fulfill({ json: { messages: (logs.get(room) || []).filter(message => message.sequence > after), batch: batches.get(room) || null, revision, reset: false } });
    }
    if (pathname === "/real-conversations" && req.method() === "POST") { const body = req.postDataJSON(); const room = { id: `sound-new-${++created}`, skill_id: body.skill_id, title: body.title, created_at: new Date().toISOString() }; rooms.push(room); logs.set(room.id, []); return route.fulfill({ json: room }); }
    if (pathname.endsWith("/turns") && req.method() === "POST") { const room = decodeURIComponent(pathname.split("/")[2]), body = req.postDataJSON(); const user = add(room, "USER", body.text, { client_request_id: req.headers()["idempotency-key"], reply_state: "WAITING" }); const batch = { id: `batch-${room}`, status: "GENERATING", typing: true, source_message_ids: [user.id] }; batches.set(room, batch); return route.fulfill({ json: { user, batch } }); }
    if (pathname === "/virtual-participants") return route.fulfill({ json: session.participants });
    if (pathname === `/virtual-sessions/${session.id}`) return route.fulfill({ json: structuredClone(session) });
    if (pathname === "/chat-presence") return route.fulfill({ json: { ok: true } });
    if (req.method() !== "GET") throw new Error(`Unexpected fixture mutation ${req.method()} ${pathname}`);
    return route.fulfill({ json: [] });
  });
}
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, reducedMotion: "reduce" });
await instrument(context);
const page = await context.newPage(); await routes(page);
const pings = () => page.evaluate(() => window.soundStarts.filter(item => item.frequency === 880).length);
async function waitPings(expected) { await page.waitForFunction(expected => window.soundStarts.filter(item => item.frequency === 880).length === expected, expected); }
async function quiet(expected, delay = 1100) { await page.waitForTimeout(delay); assert.equal(await pings(), expected); }
async function capture(name) { const file = `${name}.png`; await page.screenshot({ path: path.join(artifacts, file), fullPage: true }); screenshots.push(file); }
try {
  await page.goto(`${base}/#chat`);
  await page.getByText("历史里的完整回答0", { exact: true }).waitFor();
  assert.equal(await pings(), 0); assert.equal(await page.evaluate(() => window.soundContexts), 0, "History must not start audio before a gesture.");
  await page.getByRole("tab", { name: /最近对话/ }).click();
  await quiet(0);
  add(rooms[0].id, "USER", "本人的新消息不提示");
  add(rooms[0].id, "AGENT", "已删除的新消息不提示", { deleted: true });
  await quiet(0);
  for (const text of ["这是一组里的第一条。", "这是第二条独立消息。", "这是第三条完整接话。"]) add(rooms[0].id, "AGENT", text);
  await waitPings(3); await quiet(3);
  const starts = await page.evaluate(() => window.soundStarts.filter(item => item.frequency === 880).map(item => item.at));
  assert.ok(starts[1] - starts[0] >= 0.27 && starts[2] - starts[1] >= 0.27, "A batched poll must queue each ping separately.");
  await page.getByTestId("recent-row").filter({ hasText: rooms[1].title }).click();
  await page.getByText("历史里的完整回答1", { exact: true }).waitFor();
  await page.getByTestId("recent-row").filter({ hasText: rooms[0].title }).click();
  await quiet(3);
  for (const width of [1440, 768, 390, 360]) {
    await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.ok(await page.getByRole("button", { name: "关闭消息提示音", exact: true }).isVisible());
    assert.ok(await page.locator(".im-composer textarea").isVisible());
    await capture(`message-sound-chat-${width}`);
  }
  await page.setViewportSize({ width: 1440, height: 1050 });
  checks.push("历史载入、用户消息、已删除消息、重复轮询与切换历史不响；同一轮收到三条新人物消息逐条提示且不重放。");

  await page.getByRole("button", { name: "关闭消息提示音", exact: true }).click();
  add(rooms[0].id, "AGENT", "静音时收到的消息"); await quiet(3);
  await page.reload(); await page.getByRole("button", { name: "开启消息提示音", exact: true }).waitFor();
  assert.equal(await pings(), 0);
  assert.equal(await page.evaluate(() => localStorage.getItem("wot.message-sound.enabled")), "false");
  await page.getByRole("button", { name: "开启消息提示音", exact: true }).click();
  await quiet(0);
  add(rooms[0].id, "AGENT", "重新开启后新到达的消息"); await waitPings(1);
  checks.push("静音立即生效并记住偏好；刷新与重新开启不补播旧消息，新到消息恢复提示。");

  batches.set(rooms[0].id, { id: "pending-away", status: "GENERATING", typing: true, source_message_ids: [logs.get(rooms[0].id)[0].id] }); revision += 1;
  await page.locator(".chat-typing").waitFor();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  add(rooms[0].id, "AGENT", "离开聊天页面时仍收到人物回复"); await waitPings(2);
  batches.delete(rooms[0].id); revision += 1;
  for (const width of [1440, 768, 390, 360]) {
    await page.setViewportSize({ width, height: width > 760 ? 1050 : 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.getByRole("button", { name: "关闭消息提示音", exact: true }).waitFor();
    await page.locator(".message-sound-settings").scrollIntoViewIfNeeded();
    await capture(`message-sound-settings-${width}`);
  }
  checks.push("在其他应用页面等待中的普通聊天仍能提示；设置开关在四种屏宽可见且无横向溢出。");

  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.getByRole("button", { name: "虚拟空间", exact: true }).click();
  await page.locator(".up-session-row").filter({ hasText: session.topic }).click();
  await page.getByText("虚拟空间的旧发言", { exact: true }).waitFor(); await quiet(2);
  session.draft = { id: "unpublished-draft", speaker_id: people[0].id, text: "还没有发布的草稿", publish_at: new Date(Date.now() + 10000).toISOString() }; session.revision += 1;
  await quiet(2, 1250);
  session.turns.push({ id: "virtual-new-one", speaker_id: people[1].id, display_name: people[1].display_name, text: "现在正式发布的虚拟人物消息", turn_index: 1, is_generated: true, version_id: "sound-person-1", published_at: new Date().toISOString() }); session.revision += 1; session.draft = null;
  await waitPings(3); await quiet(3);
  checks.push("虚拟空间旧记录和未发布草稿不响，实际发布的人物发言逐条提示，状态重复读取不重放。");

  await page.evaluate(() => { window.soundVisibility = "hidden"; document.dispatchEvent(new Event("visibilitychange")); });
  session.turns.push({ id: "virtual-hidden", speaker_id: people[0].id, display_name: people[0].display_name, text: "页面隐藏时的消息", turn_index: 2, is_generated: true, version_id: "sound-person-0", published_at: new Date().toISOString() }); session.revision += 1;
  await waitPings(4);
  await page.evaluate(() => { window.soundAudio.state = "suspended"; window.soundAudio.onstatechange?.(); });
  session.turns.push({ id: "virtual-suspended", speaker_id: people[0].id, display_name: people[0].display_name, text: "浏览器挂起音频时的消息", turn_index: 3, is_generated: true, version_id: "sound-person-0", published_at: new Date().toISOString() }); session.revision += 1;
  await quiet(4, 1300);
  const missedTime = new Date().toISOString();
  await page.evaluate(() => { window.soundVisibility = "visible"; document.dispatchEvent(new Event("visibilitychange")); });
  await page.locator(".up-virtual-header h2").click();
  session.turns.push({ id: "virtual-missed-old", speaker_id: people[0].id, display_name: people[0].display_name, text: "恢复后读取到的较早发言", turn_index: 4, is_generated: true, version_id: "sound-person-0", published_at: missedTime }); session.revision += 1;
  await quiet(4);
  await page.waitForTimeout(10);
  session.turns.push({ id: "virtual-visible", speaker_id: people[0].id, display_name: people[0].display_name, text: "返回页面后真正新到达的消息", turn_index: 5, is_generated: true, version_id: "sound-person-0", published_at: new Date().toISOString() }); session.revision += 1;
  await waitPings(5);
  checks.push("后台且音频仍运行时可提示；浏览器挂起音频时不补队列，恢复后不补播旧发言，新发布消息正常提示。");

  const unsupported = await browser.newContext(); await instrument(unsupported, true);
  const unsupportedPage = await unsupported.newPage(); await routes(unsupportedPage);
  await unsupportedPage.goto(`${base}/#chat`); await unsupportedPage.locator(".im-composer textarea").waitFor();
  await unsupportedPage.getByRole("button", { name: "关闭消息提示音", exact: true }).click();
  await unsupportedPage.getByRole("button", { name: "开启消息提示音", exact: true }).click();
  add(rooms[0].id, "AGENT", "浏览器无音频支持时也可正常显示的消息");
  await unsupportedPage.getByText("浏览器无音频支持时也可正常显示的消息", { exact: true }).waitFor();
  await unsupported.close();
  checks.push("浏览器不支持音频时不抛异常、不妨碍消息继续显示。");

  const fresh = await browser.newContext(); await instrument(fresh);
  const freshPage = await fresh.newPage(); await routes(freshPage);
  await freshPage.goto(`${base}/#chat`); await freshPage.locator(".im-composer textarea").waitFor();
  await freshPage.getByRole("button", { name: "新话题", exact: true }).click();
  await freshPage.locator(".im-composer textarea").fill("给新会话的第一句话");
  await freshPage.locator(".im-composer textarea").press("Enter");
  while (!created) await freshPage.waitForTimeout(30);
  await freshPage.locator(".chat-typing").waitFor();
  assert.equal(await freshPage.evaluate(() => window.soundStarts.length), 0);
  add(rooms.at(-1).id, "AGENT", "新会话的第一条人物回复也会提示");
  await freshPage.waitForFunction(() => window.soundStarts.filter(item => item.frequency === 880).length === 1);
  await freshPage.getByText("新会话的第一条人物回复也会提示", { exact: true }).waitFor();
  await fresh.close();
  checks.push("新建会话的用户收据作为静默基线，第一条真实人物回复仍正常提示，不被误判为历史。");

  const native = await browser.newContext();
  await native.addInitScript(() => {
    const Audio = window.AudioContext;
    window.nativeSoundStarts = 0;
    window.AudioContext = class extends Audio {
      constructor(...args) { super(...args); window.nativeSoundContext = this; }
      createOscillator() { const node = super.createOscillator(), start = node.start.bind(node); node.start = (...args) => { window.nativeSoundStarts += 1; return start(...args); }; return node; }
    };
  });
  const nativePage = await native.newPage(); await routes(nativePage);
  await nativePage.goto(`${base}/#chat`); await nativePage.locator(".im-composer textarea").waitFor();
  await nativePage.getByRole("tab", { name: /最近对话/ }).click();
  await nativePage.waitForFunction(() => window.nativeSoundContext?.state === "running");
  assert.equal(await nativePage.evaluate(() => window.nativeSoundStarts), 0);
  add(rooms.at(-1).id, "AGENT", "原生浏览器音频节点验证");
  await nativePage.waitForFunction(() => window.nativeSoundStarts === 2);
  await nativePage.getByText("原生浏览器音频节点验证", { exact: true }).waitFor();
  await native.close();
  checks.push("真实浏览器AudioContext经用户点击可进入running，一条新回复创建并启动两枚本地音符节点；不访问音频外链。");
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  await writeFile(path.join(artifacts, "report.json"), JSON.stringify({ status: "PASSED", date: new Date().toISOString(), checks, screenshots, browserErrors: errors, externalRequests: external, realModelCalls: 0, realSearchCalls: 0, limitation: "消息与状态使用浏览器本地夹具；前七组用可观察音频节点验证调度与去重，最后一组使用真实浏览器AudioContext验证解锁与本地音符启动。不声称自动化测试已实际听辨音色。" }, null, 2));
  console.log(`PASS: ${checks.length} message sound groups, ${screenshots.length} screenshots, no real calls.`);
} catch (error) { await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true }).catch(() => {}); throw error; }
finally { await context.close(); await browser.close(); }
