// Deterministic Web Audio contracts. No browser service, real key, or network.
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";

const scratch = await mkdtemp(path.join(tmpdir(), "wot-message-sound-test-"));
const handlers = () => {
  const callbacks = new Map();
  return {
    addEventListener(type, fn) { if (!callbacks.has(type)) callbacks.set(type, new Set()); callbacks.get(type).add(fn); },
    removeEventListener(type, fn) { callbacks.get(type)?.delete(fn); },
    fire(type, extra = {}) { for (const fn of [...(callbacks.get(type) || [])]) fn({ type, ...extra }); },
    count(type) { return callbacks.get(type)?.size || 0; },
  };
};
const values = new Map();
let failStorage = false, failAudio = false, failResume = false;
class Param {
  values = [];
  setValueAtTime(value, at) { this.values.push({ value, at }); }
  exponentialRampToValueAtTime(value, at) { this.values.push({ value, at }); }
}
class Oscillator {
  frequency = new Param();
  starts = []; stops = []; disconnected = false; onended = null;
  connect() {}
  disconnect() { this.disconnected = true; }
  start(at) { this.starts.push(at); }
  stop(at) { this.stops.push(at); }
}
class Gain { gain = new Param(); disconnected = false; connect() {} disconnect() { this.disconnected = true; } }
class Audio {
  static instances = [];
  state = "suspended"; currentTime = 10; destination = {}; oscillators = []; gains = []; closes = 0;
  constructor() { if (failAudio) throw new Error("audio unavailable"); Audio.instances.push(this); }
  resume() { if (failResume) return Promise.reject(new Error("autoplay denied")); this.state = "running"; return Promise.resolve(); }
  close() { this.closes += 1; this.state = "closed"; return Promise.resolve(); }
  createOscillator() { const value = new Oscillator(); this.oscillators.push(value); return value; }
  createGain() { const value = new Gain(); this.gains.push(value); return value; }
}
const fakeWindow = { ...handlers(), AudioContext: Audio, localStorage: {
  getItem(key) { if (failStorage) throw new Error("storage denied"); return values.get(key) ?? null; },
  setItem(key, value) { if (failStorage) throw new Error("storage denied"); values.set(key, value); },
} };
const fakeDocument = { ...handlers(), visibilityState: "visible" };
const originals = { window: globalThis.window, document: globalThis.document };
globalThis.window = fakeWindow; globalThis.document = fakeDocument;
const tick = () => new Promise(done => setImmediate(done));
try {
  const source = await readFile(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src/messageSound.ts"), "utf8");
  await writeFile(path.join(scratch, "sound.mjs"), ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText);
  const sound = await import(pathToFileURL(path.join(scratch, "sound.mjs")).href);
  const cleanupA = sound.installMessageSound(), cleanupB = sound.installMessageSound();
  assert.equal(sound.isMessageSoundEnabled(), true);
  assert.equal(fakeWindow.count("pointerdown"), 1, "Multiple consumers share one gesture listener.");
  assert.equal(sound.notifyIncomingMessages("chat:history", [{ id: "history", speaker: "AGENT" }]), 0);
  sound.notifyIncomingMessages("chat:waiting", []);
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "blocked", speaker: "AGENT" }]), 0);
  assert.equal(Audio.instances.length, 0, "Autoplay is not attempted before a trusted gesture.");
  fakeWindow.fire("pointerdown", { isTrusted: false });
  assert.equal(Audio.instances.length, 0);
  fakeWindow.fire("pointerdown", { isTrusted: true }); await tick();
  const audio = Audio.instances[0];
  assert.equal(audio.state, "running");
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "blocked", speaker: "AGENT" }]), 0, "Blocked arrivals never replay after unlock.");
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "one", speaker: "AGENT" }, { id: "two", speaker: "AGENT" }, { id: "three", speaker: "AGENT" }]), 3);
  assert.equal(audio.oscillators.length, 6, "Each message gets a complete two-note chime.");
  const starts = audio.oscillators.filter((_, index) => index % 2 === 0).map(node => node.starts[0]);
  assert.ok(starts[1] - starts[0] >= 0.279 && starts[2] - starts[1] >= 0.279, "Burst messages are separately audible.");
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "one", speaker: "AGENT" }, { id: "own", speaker: "USER" }, { id: "deleted", speaker: "AGENT", deleted: true }]), 0);
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "resync", speaker: "AGENT" }], { baseline: true }), 0);
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "resync", speaker: "AGENT" }]), 0);
  let notified = 0;
  const unsubscribe = sound.subscribeMessageSound(() => { notified += 1; });
  sound.setMessageSoundEnabled(false);
  assert.ok(audio.oscillators.every(node => node.disconnected), "Muting cancels already scheduled tones.");
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "muted", speaker: "AGENT" }]), 0);
  sound.setMessageSoundEnabled(true);
  assert.equal(sound.notifyIncomingMessages("chat:waiting", [{ id: "muted", speaker: "AGENT" }]), 0);
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "virtual-history" }]), 0);
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "virtual-new" }]), 1, "Virtual published turns may omit chat speaker labels.");
  fakeDocument.visibilityState = "hidden"; fakeDocument.fire("visibilitychange");
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "hidden" }]), 1, "A running background tab still sounds for new messages.");
  audio.state = "suspended"; audio.onstatechange?.();
  assert.ok(audio.oscillators.every(node => node.disconnected), "Browser suspension cancels scheduled tones rather than replaying them later.");
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "suspended" }]), 0);
  audio.state = "running"; audio.onstatechange?.();
  const restoredAt = Date.now();
  fakeDocument.visibilityState = "visible"; fakeDocument.fire("visibilitychange");
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "hidden" }, { id: "suspended" }, { id: "missed-hidden", published_at: new Date(restoredAt - 1000).toISOString() }]), 0);
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "after-return", published_at: new Date(Date.now() + 1000).toISOString() }]), 1);
  sound.forgetMessageSoundScope("virtual:new");
  assert.ok(audio.oscillators.every(node => node.disconnected), "Deleting a room stops its queued chimes.");
  assert.equal(sound.notifyIncomingMessages("virtual:new", [{ id: "virtual-history" }]), 0);
  fakeWindow.fire("storage", { key: "wot.message-sound.enabled", newValue: "false" });
  assert.equal(sound.isMessageSoundEnabled(), false);
  assert.equal(notified, 3);
  fakeWindow.fire("storage", { key: "unrelated", newValue: "true" });
  assert.equal(sound.isMessageSoundEnabled(), false);
  failStorage = true; sound.setMessageSoundEnabled(true); failStorage = false;
  assert.equal(sound.isMessageSoundEnabled(), true, "Denied storage does not break session controls.");
  assert.deepEqual([...values.keys()], ["wot.message-sound.enabled"], "Only the harmless sound preference is persisted.");
  unsubscribe();
  cleanupA(); assert.equal(audio.closes, 0);
  cleanupB(); cleanupB(); assert.equal(audio.closes, 1);
  assert.equal(fakeWindow.count("pointerdown"), 0); assert.equal(fakeWindow.count("storage"), 0);
  assert.equal(fakeDocument.count("visibilitychange"), 0);

  const cleanupC = sound.installMessageSound();
  failResume = true; fakeWindow.fire("keydown", { isTrusted: true }); await tick();
  assert.equal(sound.notifyIncomingMessages("denied", [{ id: "denied" }], { baseline: false }), 0);
  failResume = false; fakeWindow.fire("keydown", { isTrusted: true }); await tick();
  assert.equal(sound.notifyIncomingMessages("denied", [{ id: "denied" }]), 0);
  assert.equal(sound.notifyIncomingMessages("denied", [{ id: "recovered" }]), 1);
  cleanupC();
  const cleanupD = sound.installMessageSound();
  failAudio = true; fakeWindow.fire("pointerdown", { isTrusted: true });
  assert.equal(sound.notifyIncomingMessages("unsupported", [{ id: "unsupported" }], { baseline: false }), 0);
  cleanupD(); failAudio = false;
  console.log("PASS: gesture/autoplay, per-message burst tones, history/retry dedupe, mute/background/suspension/no-backlog, deletion, preference storage and shared cleanup.");
} finally {
  globalThis.window = originals.window; globalThis.document = originals.document;
  const target = path.resolve(scratch), root = path.resolve(tmpdir()) + path.sep;
  assert.ok(target.startsWith(root) && path.basename(target).startsWith("wot-message-sound-test-"));
  await rm(target, { recursive: true, force: true });
}
