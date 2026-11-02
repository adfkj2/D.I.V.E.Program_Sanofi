/** Local incoming-message chime. No message text or external audio is stored. */

export type SoundMessage = { id: string; speaker?: string; deleted?: boolean; created_at?: string; published_at?: string };
export type SoundOptions = { baseline?: boolean };

const PREFERENCE = "wot.message-sound.enabled";
const GAP = 0.28;
const seen = new Map<string, Set<string>>();
const listeners = new Set<() => void>();
type Voice = { scope: string; oscillators: OscillatorNode[]; gains: GainNode[] };
const voices = new Set<Voice>();
let context: AudioContext | null = null;
let nextAt = 0;
let gesture = false;
let mounts = 0;
let resuming = false;
let visibleAfter = 0;
let disposeInstallation: (() => void) | null = null;

function readPreference() {
  try { return typeof window === "undefined" || window.localStorage.getItem(PREFERENCE) !== "false"; }
  catch { return true; }
}
let enabled = readPreference();

export function isMessageSoundEnabled() { return enabled; }
export function subscribeMessageSound(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }

function stopVoice(voice: Voice) {
  voices.delete(voice);
  for (const oscillator of voice.oscillators) {
    oscillator.onended = null;
    try { oscillator.stop(); } catch { /* An already-ended node is harmless. */ }
    try { oscillator.disconnect(); } catch { /* Audio failure never interrupts chat. */ }
  }
  for (const gain of voice.gains) { try { gain.disconnect(); } catch { /* Ignore closed contexts. */ } }
}

function stopQueued() {
  for (const voice of [...voices]) stopVoice(voice);
  nextAt = 0;
}

export function setMessageSoundEnabled(value: boolean) {
  if (enabled === value) return;
  enabled = value;
  try { window.localStorage.setItem(PREFERENCE, String(value)); } catch { /* Session-only preference is still usable. */ }
  if (!value) stopQueued();
  listeners.forEach(listener => listener());
}

function unlock(event: Event) {
  if (!event.isTrusted || typeof window === "undefined" || document.visibilityState === "hidden") return;
  gesture = true;
  try {
    if (!context || context.state === "closed") {
      const Audio = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Audio) return;
      context = new Audio();
      const audio = context;
      audio.onstatechange = () => {
        // Some mobile browsers freeze the audio clock in the background. Do
        // not let already-scheduled tones replay when that clock resumes.
        if (context === audio && audio.state !== "running") stopQueued();
      };
    }
    if (context.state === "suspended" && !resuming) {
      resuming = true;
      Promise.resolve(context.resume()).catch(() => {}).finally(() => { resuming = false; });
    }
  } catch { /* Autoplay denial or unsupported audio must not block messaging. */ }
}

function visible() { return typeof document !== "undefined" && document.visibilityState !== "hidden"; }

function chime(scope: string) {
  if (!enabled || !gesture || !context || context.state !== "running") return false;
  const audio = context;
  const at = Math.max(audio.currentTime + 0.008, nextAt);
  const voice: Voice = { scope, oscillators: [], gains: [] };
  try {
    // An original, soft two-note incoming tone with short envelopes. It is
    // generated locally rather than fetching a branded notification asset.
    for (const [offset, frequency, duration, volume] of [[0, 880, 0.13, 0.085], [0.075, 1174.66, 0.15, 0.07]]) {
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      voice.oscillators.push(oscillator); voice.gains.push(gain);
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(frequency, at + offset);
      oscillator.frequency.exponentialRampToValueAtTime(frequency * 0.94, at + offset + duration);
      gain.gain.setValueAtTime(0.0001, at + offset);
      gain.gain.exponentialRampToValueAtTime(volume, at + offset + 0.006);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + offset + duration);
      oscillator.connect(gain); gain.connect(audio.destination);
      oscillator.start(at + offset); oscillator.stop(at + offset + duration + 0.01);
    }
    voices.add(voice);
    voice.oscillators[voice.oscillators.length - 1].onended = () => stopVoice(voice);
    nextAt = at + GAP;
    return true;
  } catch { stopVoice(voice); return false; }
}

/**
 * First observation of a room is a silent history baseline by default. Each
 * subsequent, previously unseen published character ID gets one distinct ping.
 * Muted, suspended or autoplay-blocked arrivals are consumed, never queued later.
 * A running background tab may play the same incoming sound as a visible tab.
 */
export function notifyIncomingMessages(scope: string, messages: readonly SoundMessage[], options: SoundOptions = {}) {
  const baseline = options.baseline ?? !seen.has(scope);
  const known = seen.get(scope) || new Set<string>();
  seen.set(scope, known);
  let scheduled = 0;
  for (const message of messages) {
    if (known.has(message.id)) continue;
    known.add(message.id);
    const published = Date.parse(message.published_at || message.created_at || "");
    if (Number.isFinite(published) && published <= visibleAfter) continue;
    if (!baseline && !message.deleted && message.speaker !== "USER" && chime(scope)) scheduled += 1;
  }
  return scheduled;
}

/** Clear a removed room's IDs and any chimes still waiting to play. */
export function forgetMessageSoundScope(scope: string) {
  seen.delete(scope);
  for (const voice of [...voices]) if (voice.scope === scope) stopVoice(voice);
  if (!voices.size) nextAt = 0;
}

/** Install once at app level; page changes do not recreate the audio context. */
export function installMessageSound() {
  if (typeof window === "undefined" || typeof document === "undefined") return () => {};
  mounts += 1;
  let released = false;
  const release = () => {
    if (released) return;
    released = true; mounts -= 1;
    if (!mounts) { disposeInstallation?.(); disposeInstallation = null; }
  };
  if (mounts > 1) return release;
  const visibility = () => {
    if (visible()) visibleAfter = Date.now();
  };
  const pageShown = (event: PageTransitionEvent) => { if (event.persisted) visibleAfter = Date.now(); };
  const storage = (event: StorageEvent) => {
    if (event.key !== PREFERENCE) return;
    const value = event.newValue !== "false";
    if (value === enabled) return;
    enabled = value;
    if (!enabled) stopQueued();
    listeners.forEach(listener => listener());
  };
  window.addEventListener("pointerdown", unlock, { capture: true });
  window.addEventListener("keydown", unlock, { capture: true });
  window.addEventListener("storage", storage);
  window.addEventListener("pagehide", stopQueued);
  window.addEventListener("pageshow", pageShown);
  document.addEventListener("visibilitychange", visibility);
  disposeInstallation = () => {
    window.removeEventListener("pointerdown", unlock, { capture: true });
    window.removeEventListener("keydown", unlock, { capture: true });
    window.removeEventListener("storage", storage);
    window.removeEventListener("pagehide", stopQueued);
    window.removeEventListener("pageshow", pageShown);
    document.removeEventListener("visibilitychange", visibility);
    stopQueued(); gesture = false; resuming = false; visibleAfter = 0;
    const old = context; context = null;
    if (old && old.state !== "closed") { try { void Promise.resolve(old.close()).catch(() => {}); } catch { /* Ignore audio teardown. */ } }
  };
  return release;
}
