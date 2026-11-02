import { ApiError, post, request, type ChatUpdates, type Conversation, type Message, type ReplyBatch } from "./api";
import { bindConversation, chatMemory, conversationDeleted, forgetConversation, thread, updatePending, updateThread, type Pending } from "./chatMemory";
import { forgetMessageSoundScope, notifyIncomingMessages } from "./messageSound";

const creations = new Map<string, Promise<Conversation>>();
const aliases = new Map<string, string>();
const syncing = new Map<string, Promise<void>>();
const cancelledTurns = new Set<string>();
let acceptanceQueue = Promise.resolve();
export const tabId = crypto.randomUUID();
export const activeBatch = (batch: ReplyBatch | null) => Boolean(batch && ["QUEUED", "GENERATING", "DELIVERING", "WAITING_FOLLOWUP"].includes(batch.status));

function sorted(messages: Message[]) {
  return messages.sort((a, b) => (a.sequence ?? Number.MAX_SAFE_INTEGER) - (b.sequence ?? Number.MAX_SAFE_INTEGER) || a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
}

export function syncThread(key: string, conversationId: string, full = false): Promise<void> {
  if (conversationDeleted(conversationId)) return Promise.resolve();
  if (syncing.has(key)) return full ? syncing.get(key)!.then(() => syncThread(key, conversationId, true)) : syncing.get(key)!;
  const initial = thread(key);
  const privacyEpoch = chatMemory.privacyEpoch;
  const after = full ? 0 : Math.max(0, ...initial.messages.map(message => message.sequence || 0));
  const operation = request<ChatUpdates>(`/real-conversations/${encodeURIComponent(conversationId)}/updates?after_sequence=${after}&after_revision=${initial.revision}`)
    .then(result => {
      if (conversationDeleted(conversationId)) return;
      const current = thread(key);
      if (privacyEpoch !== chatMemory.privacyEpoch || result.revision < current.revision) return;
      const byId = new Map((result.reset || full ? [] : current.messages).map(message => [message.id, message]));
      for (const message of result.messages) {
        byId.set(message.id, message);
        if (message.evidence_id && !message.deleted) chatMemory.knownEvidenceIds.add(message.evidence_id);
      }
      const messages = sorted([...byId.values()]);
      notifyIncomingMessages(`chat:${conversationId}`, messages, { baseline: full || result.reset || !current.loaded });
      const acceptedIds = new Set(messages.map(message => message.client_request_id));
      updateThread(key, { messages, batch: result.batch, revision: result.revision, loaded: true, loading: false, error: "", pending: current.pending.filter(pending => !acceptedIds.has(pending.id)) });
    })
    .catch(error => {
      if (error instanceof ApiError && [404, 410].includes(error.status)) purgeConversation(conversationId);
      throw error;
    })
    .finally(() => syncing.delete(key));
  syncing.set(key, operation);
  return operation;
}

export function submitTurn(originalKey: string, pending: Pending, refresh: () => Promise<void>) {
  const work = async () => {
    let targetKey = aliases.get(originalKey) || originalKey;
    let conversationId = pending.conversationId || (targetKey.startsWith("conversation:") ? targetKey.slice(13) : null);
    const cancelled = () => cancelledTurns.has(pending.id) || conversationDeleted(conversationId);
    if (cancelled()) return;
    updatePending(targetKey, pending.id, { status: "SENDING", error: undefined });
    try {
      if (!conversationId) {
        let creating = creations.get(originalKey);
        if (!creating) {
          creating = post<Conversation>("/real-conversations", { title: pending.payload.text.slice(0, 24), skill_id: pending.skillId, web_enabled: thread(targetKey).webEnabled !== false }, pending.creationKey);
          creations.set(originalKey, creating);
        }
        const conversation = await creating;
        conversationId = conversation.id;
        if (cancelled()) { creations.delete(originalKey); return; }
        targetKey = bindConversation(targetKey, conversationId, pending.skillId);
        aliases.set(originalKey, targetKey);
      }
      const privacyEpoch = chatMemory.privacyEpoch;
      const result = await post<{ user: Message; batch: ReplyBatch }>(`/real-conversations/${encodeURIComponent(conversationId)}/turns`, pending.payload, pending.id);
      if (cancelled()) return;
      if (privacyEpoch !== chatMemory.privacyEpoch) { await syncThread(targetKey, conversationId, true); void refresh().catch(() => {}); return; }
      const current = thread(targetKey);
      const byId = new Map(current.messages.map(message => [message.id, message]));
      byId.set(result.user.id, result.user);
      notifyIncomingMessages(`chat:${conversationId}`, [...byId.values()], { baseline: true });
      updateThread(targetKey, { messages: sorted([...byId.values()]), batch: result.batch, loaded: true, pending: current.pending.filter(item => item.id !== pending.id), error: "" });
      void syncThread(targetKey, conversationId).catch(() => {});
      void refresh().catch(() => {});
    } catch (error) {
      if (cancelled()) return;
      if (!conversationId) creations.delete(originalKey);
      if (conversationId) await syncThread(targetKey, conversationId, true).catch(() => {});
      if (cancelled()) return;
      const current = thread(targetKey);
      // A lost acceptance response may already have committed the human message.
      if (!current.messages.some(message => message.client_request_id === pending.id)) {
        if (!current.pending.some(item => item.id === pending.id)) updateThread(targetKey, { pending: [...current.pending, pending] });
        updatePending(targetKey, pending.id, { conversationId, status: "FAILED", error: error instanceof Error ? error.message : "消息尚未确认保存，请重试。" });
      }
      void refresh().catch(() => {});
    }
  };
  acceptanceQueue = acceptanceQueue.then(work, work);
  return acceptanceQueue;
}

export async function retryBatch(key: string, conversationId: string, batchId: string) {
  if (conversationDeleted(conversationId)) return;
  const result = await post<{ batch: ReplyBatch }>(`/reply-batches/${encodeURIComponent(batchId)}/retry`);
  if (conversationDeleted(conversationId)) return;
  updateThread(key, { batch: result.batch, error: "" });
  await syncThread(key, conversationId, true);
}

export function purgeConversation(conversationId: string) {
  forgetMessageSoundScope(`chat:${conversationId}`);
  const target = `conversation:${conversationId}`;
  if (chatMemory.selection?.conversationId === conversationId) chatMemory.selection = { skillId: chatMemory.selection.skillId, conversationId: null };
  for (const id of forgetConversation(conversationId)) cancelledTurns.add(id);
  for (const [original, resolved] of aliases) {
    if (resolved === target || original === target) { aliases.delete(original); creations.delete(original); }
  }
  creations.delete(target);
  syncing.delete(target);
  if (nextPresence?.conversation_id === conversationId) nextPresence = { ...nextPresence, conversation_id: null, visible: false, composing: false };
}

let nextPresence: { conversation_id: string | null; tab_id: string; visible: boolean; composing: boolean } | null = null;
let reportingPresence: Promise<void> | null = null;
export function presence(conversationId: string | null, visible: boolean, composing: boolean): Promise<void> {
  if (conversationDeleted(conversationId)) { conversationId = null; visible = false; composing = false; }
  nextPresence = { conversation_id: conversationId, tab_id: tabId, visible, composing };
  if (reportingPresence) return reportingPresence;
  // Serialize and coalesce reports so an old route cleanup cannot overtake the new chat.
  reportingPresence = (async () => {
    while (nextPresence) {
      const report = nextPresence;
      nextPresence = null;
      await request("/chat-presence", { method: "PUT", keepalive: true, body: JSON.stringify(report) }).catch(() => {});
    }
  })().finally(() => { reportingPresence = null; });
  return reportingPresence;
}
