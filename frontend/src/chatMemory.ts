import { useSyncExternalStore } from "react";
import type { Message } from "./api";

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
  draft: string;
  domain: string;
  remember: boolean;
  messages: Message[];
  loaded: boolean;
  loading: boolean;
  error: string;
  pending: Pending[];
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
} = { selection: null, threads: new Map(), tab: "people", query: "", domain: "全部", mobileChat: false, knownEvidenceIds: new Set() };

let revision = 0;
const listeners = new Set<() => void>();
export function emit() { revision += 1; listeners.forEach(listener => listener()); }
export function useChatMemory() {
  useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener); }, () => revision, () => revision);
  return chatMemory;
}
export function threadKey(selection: Selection) { return selection.conversationId ? `conversation:${selection.conversationId}` : `preview:${selection.skillId}`; }
export function thread(key: string): Thread {
  if (!chatMemory.threads.has(key)) chatMemory.threads.set(key, { draft: "", domain: "general", remember: false, messages: [], loaded: false, loading: false, error: "", pending: [], scrollTop: 0, hasScroll: false, nearBottom: true, forceBottom: false });
  return chatMemory.threads.get(key)!;
}
export function updateThread(key: string, patch: Partial<Thread>) {
  chatMemory.threads.set(key, { ...thread(key), ...patch }); emit();
}
export function updatePending(key: string, id: string, patch: Partial<Pending>) {
  updateThread(key, { pending: thread(key).pending.map(pending => pending.id === id ? { ...pending, ...patch } : pending) });
}
export function bindConversation(key: string, id: string, skillId: string) {
  const target = `conversation:${id}`;
  const view = thread(key);
  chatMemory.threads.set(target, { ...view, pending: view.pending.map(pending => ({ ...pending, conversationId: id })) });
  chatMemory.threads.delete(key);
  if (chatMemory.selection && threadKey(chatMemory.selection) === key) chatMemory.selection = { skillId, conversationId: id };
  emit(); return target;
}
