// Isolated module-level races: no browser, service, model, or real credentials.
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import ts from "typescript";

const require = createRequire(import.meta.url);
const scratch = await mkdtemp(path.join(tmpdir(), "wot-chat-delete-test-"));
const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise(done => setImmediate(done));
try {
  const memorySource = await readFile(path.join(source, "chatMemory.ts"), "utf8");
  const syncSource = await readFile(path.join(source, "chatSync.ts"), "utf8");
  const soundSource = await readFile(path.join(source, "messageSound.ts"), "utf8");
  const convert = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
  await writeFile(path.join(scratch, "memory.mjs"), convert(memorySource).replace('from "react"', `from ${JSON.stringify(pathToFileURL(require.resolve("react")).href)}`));
  await writeFile(path.join(scratch, "api.mjs"), "export class ApiError extends Error{constructor(message,status){super(message);this.status=status;}}export const calls=[];let postHandler,requestHandler;export function setup(p,r){postHandler=p;requestHandler=r;}export function post(...args){calls.push(['post',...args]);return postHandler(...args);}export function request(...args){calls.push(['request',...args]);return requestHandler(...args);}");
  await writeFile(path.join(scratch, "messageSound.mjs"), convert(soundSource));
  await writeFile(path.join(scratch, "sync.mjs"), convert(syncSource).replace('from "./api"', 'from "./api.mjs"').replace('from "./chatMemory"', 'from "./memory.mjs"').replace('from "./messageSound"', 'from "./messageSound.mjs"'));
  const memory = await import(pathToFileURL(path.join(scratch, "memory.mjs")).href);
  const sync = await import(pathToFileURL(path.join(scratch, "sync.mjs")).href);
  const api = await import(pathToFileURL(path.join(scratch, "api.mjs")).href);
  const pending = (id, conversationId) => ({ id, creationKey: `create-${id}`, conversationId, skillId: "person", payload: { text: `private ${id}`, remember: false, domain: "general" }, status: "SENDING", createdAt: "2026-11-02T00:00:00Z" });
  const latePoll = deferred(), lateSend = deferred();
  api.setup(() => lateSend.promise, () => latePoll.promise);
  memory.updateThread("conversation:deleted-a", { draft: "private draft A", messages: [{ id: "old-a", text: "private A", speaker: "AGENT", created_at: "2026-11-02T00:00:00Z", research_id: "private-source", source_refs: ["source-a"] }], pending: [pending("first", "deleted-a"), pending("queued", "deleted-a")] });
  memory.updateThread("conversation:kept-b", { draft: "keep draft B", pending: [pending("keep", "kept-b")] });
  const polling = sync.syncThread("conversation:deleted-a", "deleted-a");
  const sending = sync.submitTurn("conversation:deleted-a", pending("first", "deleted-a"), async () => {});
  const queued = sync.submitTurn("conversation:deleted-a", pending("queued", "deleted-a"), async () => {});
  await tick();
  assert.equal(api.calls.filter(call => call[0] === "post").length, 1);
  memory.chatMemory.selection = { skillId: "person-b", conversationId: "kept-b" };
  sync.purgeConversation("deleted-a");
  latePoll.resolve({ messages: [{ id: "late-a", text: "deleted late private A", speaker: "AGENT", sequence: 2, created_at: "2026-11-02T00:00:00Z" }], revision: 2, batch: null });
  lateSend.resolve({ user: { id: "late-user-a", text: "deleted accepted A", client_request_id: "first", speaker: "USER", sequence: 3, created_at: "2026-11-02T00:00:00Z" }, batch: null });
  await Promise.all([polling, sending, queued]);
  assert.equal(memory.chatMemory.threads.has("conversation:deleted-a"), false);
  assert.equal(memory.thread("conversation:deleted-a").messages.length, 0);
  assert.equal(memory.chatMemory.threads.has("conversation:deleted-a"), false, "A late read must not recreate a deleted cache.");
  assert.equal(memory.thread("conversation:kept-b").draft, "keep draft B");
  assert.equal(memory.thread("conversation:kept-b").pending[0].id, "keep");
  assert.deepEqual(memory.chatMemory.selection, { skillId: "person-b", conversationId: "kept-b" }, "Deleting A after switching to B must not change current selection.");
  assert.equal(api.calls.filter(call => call[0] === "post").length, 1, "Queued sends to the deleted room must never dispatch.");
  await sync.retryBatch("conversation:deleted-a", "deleted-a", "old-batch");
  assert.equal(api.calls.filter(call => call[0] === "post").length, 1);

  // Deleting a room created from a preview must remove its alias; a genuinely
  // new topic can create a new room while old queued work remains cancelled.
  const firstAcceptance = deferred(); let creates = 0;
  api.setup((url, payload) => {
    if (url === "/real-conversations") { creates += 1; return Promise.resolve({ id: creates === 1 ? "from-preview" : "fresh-room" }); }
    if (url.includes("from-preview")) return firstAcceptance.promise;
    return Promise.resolve({ user: { id: "new-user", text: payload.text, speaker: "USER", sequence: 1, created_at: "2026-11-02T00:00:00Z" }, batch: null });
  }, () => Promise.resolve({ messages: [], revision: 0, batch: null }));
  memory.updateThread("preview:person", { pending: [pending("preview-old", null), pending("preview-queued", null)] });
  const firstPreview = sync.submitTurn("preview:person", pending("preview-old", null), async () => {});
  const queuedPreview = sync.submitTurn("preview:person", pending("preview-queued", null), async () => {});
  await tick();
  sync.purgeConversation("from-preview");
  firstAcceptance.resolve({ user: { id: "removed-user", speaker: "USER", text: "removed", created_at: "2026-11-02T00:00:00Z" }, batch: null });
  await Promise.all([firstPreview, queuedPreview]);
  memory.updateThread("preview:person", { pending: [pending("fresh-topic", null)] });
  await sync.submitTurn("preview:person", pending("fresh-topic", null), async () => {});
  assert.equal(creates, 2, "A deleted preview alias must not redirect a new topic back to its old room.");
  assert.equal(memory.chatMemory.threads.has("conversation:from-preview"), false);
  assert.ok(memory.chatMemory.threads.has("conversation:fresh-room"));
  memory.chatMemory.selection = { skillId: "person-b", conversationId: "kept-b" };
  api.setup(() => Promise.resolve({}), () => Promise.reject(new api.ApiError("已删除的会话", 404)));
  await assert.rejects(sync.syncThread("conversation:kept-b", "kept-b"), /已删除/);
  assert.equal(memory.chatMemory.threads.has("conversation:kept-b"), false);
  assert.deepEqual(memory.chatMemory.selection, { skillId: "person-b", conversationId: null });
  console.log("PASS: deleted cache, late poll/receipt, queued sends, unrelated draft, retry and preview alias races.");
} finally {
  const resolved = path.resolve(scratch), root = path.resolve(tmpdir()) + path.sep;
  assert.ok(resolved.startsWith(root) && path.basename(resolved).startsWith("wot-chat-delete-test-"));
  await rm(resolved, { recursive: true, force: true });
}
