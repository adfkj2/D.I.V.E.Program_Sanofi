import { useSyncExternalStore } from "react";
import type { Message, ReplyBatch } from "./api";

export type Selection = { skillId: string; conversationId: string | null };
export type Pending = {
  id: string;
  creationKey: string;
  conversationId: string | null;
  skillId: string;
  payload: { text: string; remember: boolean; domain: string };
  status: "SENDING" | "FAILED";
  createdAt: string;
  error?: string;
};
export type Thread = {
  webEnabled?: boolean;
  draft: string;
  domain: string;
  remember: boolean;
  messages: Message[];
  loaded: boolean;
  loading: boolean;
  error: string;
  pending: Pending[];
  batch: ReplyBatch | null;
  batchRetrying?: boolean;
  revision: number;
  scrollTop: number;
  hasScroll: boolean;
  nearBottom: boolean;
  forceBottom: boolean;
};
export const chatMemory: {
  selection: Selection | null;
  threads: Map<string, Thread>;
  tab: "people" | "recent";
  query: string;
  domain: string;
  mobileChat: boolean;
  knownEvidenceIds: Set<string>;
  privacyEpoch: number;
  deletedConversations: Set<string>;
} = { selection: null, threads: new Map(), tab: "people", query: "", domain: "全部", mobileChat: false, knownEvidenceIds: new Set(), privacyEpoch: 0, deletedConversations: new Set() };

let revision = 0;
const listeners = new Set<() => void>();
export function emit() { revision += 1; listeners.forEach(listener => listener()); }
export function useChatMemory() {
  useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener); }, () => revision, () => revision);
  return chatMemory;
}
export function threadKey(selection: Selection) { return selection.conversationId ? `conversation:${selection.conversationId}` : `preview:${selection.skillId}`; }
function emptyThread(): Thread { return { draft: "", domain: "general", remember: false, messages: [], loaded: false, loading: false, error: "", pending: [], batch: null, revision: 0, scrollTop: 0, hasScroll: false, nearBottom: true, forceBottom: false }; }
export function conversationDeleted(id: string | null | undefined) { return Boolean(id && chatMemory.deletedConversations.has(id)); }
function deletedKey(key: string) { return key.startsWith("conversation:") && conversationDeleted(key.slice(13)); }
export function thread(key: string): Thread {
  // A late poll, send receipt or finally handler must not recreate erased data.
  if (deletedKey(key)) return emptyThread();
  if (!chatMemory.threads.has(key)) chatMemory.threads.set(key, emptyThread());
  return chatMemory.threads.get(key)!;
}
export function updateThread(key: string, patch: Partial<Thread>) {
  if (deletedKey(key)) return;
  chatMemory.threads.set(key, { ...thread(key), ...patch }); emit();
}
export function updatePending(key: string, id: string, patch: Partial<Pending>) {
  updateThread(key, { pending: thread(key).pending.map(pending => pending.id === id ? { ...pending, ...patch } : pending) });
}
export function bindConversation(key: string, id: string, skillId: string) {
  const target = `conversation:${id}`;
  if (conversationDeleted(id)) return target;
  const view = thread(key);
  chatMemory.threads.set(target, { ...view, pending: view.pending.map(pending => ({ ...pending, conversationId: id })) });
  chatMemory.threads.delete(key);
  if (chatMemory.selection && threadKey(chatMemory.selection) === key) chatMemory.selection = { skillId, conversationId: id };
  emit(); return target;
}

export function forgetConversation(id: string) {
  const key = `conversation:${id}`;
  const cancelledIds = new Set<string>();
  chatMemory.deletedConversations.add(id);
  chatMemory.privacyEpoch += 1;
  for (const [cachedKey, cached] of chatMemory.threads) {
    for (const pending of cached.pending) if (cachedKey === key || pending.conversationId === id) cancelledIds.add(pending.id);
    if (cachedKey === key) {
      Object.assign(cached, emptyThread());
      chatMemory.threads.delete(cachedKey);
    } else cached.pending = cached.pending.filter(pending => pending.conversationId !== id);
  }
  emit();
  return cancelledIds;
}
