import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { post, request, type Conversation, type Message, type Skill } from "./api";
import type { Shared } from "./App";
import { Avatar, AvatarControls, PersonManagement, modelConfigured } from "./Upgrade";
import { bindConversation, chatMemory, emit, thread, threadKey, updatePending, updateThread, useChatMemory, type Pending, type Selection } from "./chatMemory";

const pinned = ["munger", "buffett", "zhangxuefeng"];
const domainLabels: Record<string, string> = { general: "日常", career: "工作", relationships: "关系", learning: "学习" };
function rank(skill: Skill) { const index = pinned.indexOf(skill.slug || ""); return index < 0 ? 10 + (skill.rank ?? 100) : index; }
function recentTime(value?: string) {
  if (!value) return "";
  const parsed = new Date(value); if (Number.isNaN(parsed.getTime())) return "";
  const today = new Date();
  return parsed.toDateString() === today.toDateString() ? parsed.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : parsed.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
function moment(value: string) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("zh-CN", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function conversationTime(conversation: Conversation) { return conversation.last_message_at || conversation.updated_at || conversation.created_at; }

function Glyph({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
    plus: <path d="M12 4v16M4 12h16" />,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
    back: <path d="m15 5-7 7 7 7" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    send: <><path d="m4 4 17 8-17 8 3-8-3-8Z" /><path d="M7 12h14" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10v.1" /></>,
    arrow: <><path d="M5 12h14m-5-5 5 5-5 5" /></>,
    memory: <><path d="M7 3h9l4 4v14H7V3Z" /><path d="M15 3v5h5M10 12h6m-6 4h4" /></>,
    retry: <><path d="M5 9a7 7 0 1 1 0 6M5 4v5h5" /></>,
    down: <path d="m6 9 6 6 6-6" />,
    check: <path d="m5 12 4 4L19 6" />,
    user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.more}</svg>;
}

export function Portrait({ skill, size = 44 }: { skill?: Skill; size?: number }) {
  const source = skill?.avatar_url || (skill?.slug ? `/portraits/${skill.slug}.png` : "");
  const [failed, setFailed] = useState(!source);
  useEffect(() => setFailed(!source), [source]);
  return <span className={`portrait ${failed ? "portrait-fallback" : ""}`} style={{ width: size, height: size }} aria-hidden="true">{!failed && source ? <img src={source} alt="" onError={() => setFailed(true)} /> : <span>{skill?.display_name.slice(0, 1) || "人"}</span>}</span>;
}

async function loadMessages(key: string, id: string) {
  updateThread(key, { loading: true, error: "" });
  try {
    const messages = await request<Message[]>(`/real-conversations/${encodeURIComponent(id)}/messages`);
    for (const message of messages) if (message.evidence_id && !message.deleted && !["[已删除]", "[关联来源已删除]"].includes(message.text)) chatMemory.knownEvidenceIds.add(message.evidence_id);
    updateThread(key, { messages, loaded: true, loading: false });
  }
  catch (error) { updateThread(key, { loading: false, error: error instanceof Error ? error.message : "消息暂时没有加载，请重试。" }); }
}

export default function Chat(shared: Shared) {
  const { data, act, confirm, navigate, refresh, openModels } = shared;
  const memory = useChatMemory();
  const skills = [...data.skills].filter(person => person.status === "ACTIVE").sort((a, b) => rank(a) - rank(b) || a.display_name.localeCompare(b.display_name, "zh-CN"));
  const conversations = [...data.conversations].sort((a, b) => conversationTime(b).localeCompare(conversationTime(a)) || b.id.localeCompare(a.id));
  if (!memory.selection && skills.length) memory.selection = { skillId: skills[0].id, conversationId: null };
  const selection = memory.selection;
  const activeConversation = conversations.find(c => c.id === selection?.conversationId);
  const skillId = activeConversation?.skill_id || selection?.skillId;
  const skill = data.skills.find(s => s.id === skillId);
  const key = selection ? threadKey(selection) : "empty";
  const view = thread(key);
  const [profileOpen, setProfileOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [messageMenu, setMessageMenu] = useState("");
  const viewport = useRef<HTMLDivElement>(null);
  const profile = useRef<HTMLElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const restored = useRef("");
  const previousMessageCount = useRef(0);
  const [newBelow, setNewBelow] = useState(false);
  const domains = ["全部", ...new Set(skills.map(s => s.domain).filter((domain): domain is string => Boolean(domain)))];
  const query = memory.query.trim().toLocaleLowerCase();
  const matchingSkills = skills.filter(s => (memory.domain === "全部" || s.domain === memory.domain) && `${s.display_name} ${s.description} ${(s.tags || []).join(" ")} ${s.slug || ""}`.toLocaleLowerCase().includes(query));
  const matchingConversations = conversations.filter(c => `${c.title} ${c.last_message_preview || ""} ${skills.find(s => s.id === c.skill_id)?.display_name || ""}`.toLocaleLowerCase().includes(query));
  const sending = view.pending.some(pending => pending.status === "SENDING");

  useEffect(() => {
    if (!profileOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    profile.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setProfileOpen(false); }
      if (event.key === "Tab" && window.matchMedia("(max-width: 1250px)").matches) {
        const controls = profile.current?.querySelectorAll<HTMLElement>("button, a, input, textarea, select, [tabindex='0']");
        if (!controls?.length) return;
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener("keydown", listener);
    return () => { window.removeEventListener("keydown", listener); previous?.focus(); };
  }, [profileOpen]);

  useEffect(() => {
    if (selection?.conversationId && !view.loaded && !view.loading && !view.error) void loadMessages(key, selection.conversationId);
  }, [key, selection?.conversationId, view.loaded, view.loading, view.error]);
  useEffect(() => {
    const active = new Set(data.evidence.map(evidence => evidence.id));
    const removed = new Set([...chatMemory.knownEvidenceIds].filter(id => !active.has(id)));
    if (removed.size) {
      for (const [cachedKey, cached] of chatMemory.threads) {
        const removedMessages = cached.messages.filter(message => message.evidence_id && removed.has(message.evidence_id));
        if (!removedMessages.length) continue;
        const requestIds = new Set(removedMessages.map(message => message.client_request_id));
        updateThread(cachedKey, { loaded: false, error: "", messages: cached.messages.map(message => message.speaker === "AGENT" || (message.evidence_id && removed.has(message.evidence_id)) ? { ...message, text: message.speaker === "USER" ? "[已删除]" : "[关联来源已删除]", deleted: true } : message), pending: cached.pending.filter(pending => !requestIds.has(pending.id)) });
      }
    }
    chatMemory.knownEvidenceIds = active;
  }, [data.evidence]);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    if (restored.current !== key) {
      if (selection?.conversationId && !view.loaded) return;
      element.scrollTop = view.hasScroll ? view.scrollTop : element.scrollHeight;
      restored.current = key; previousMessageCount.current = view.messages.length; setNewBelow(false);
    } else if (view.forceBottom || view.nearBottom) {
      element.scrollTop = element.scrollHeight; setNewBelow(false);
      if (view.forceBottom) updateThread(key, { forceBottom: false });
    } else if (view.messages.length > previousMessageCount.current) setNewBelow(true);
    previousMessageCount.current = view.messages.length;
  }, [key, view.loaded, view.messages, view.pending]);
  useLayoutEffect(() => {
    if (!composer.current) return;
    composer.current.style.height = "auto";
    composer.current.style.height = `${Math.min(150, Math.max(56, composer.current.scrollHeight))}px`;
  }, [key, view.draft]);

  function choose(next: Selection) {
    memory.selection = next; memory.mobileChat = true; emit(); setMessageMenu(""); setProfileOpen(false);
  }
  function selectPerson(person: Skill) {
    const latest = conversations.find(c => c.skill_id === person.id);
    choose({ skillId: person.id, conversationId: latest?.id || null });
  }
  function newTopic() {
    if (!skill) return;
    choose({ skillId: skill.id, conversationId: null });
    composer.current?.focus();
  }
  async function deliver(originalKey: string, pending: Pending) {
    let targetKey = originalKey;
    let conversationId = pending.conversationId;
    updatePending(targetKey, pending.id, { status: "SENDING", error: undefined });
    try {
      if (!conversationId) {
        const result = await post<Conversation>("/real-conversations", { title: pending.payload.text.slice(0, 24), skill_id: pending.skillId }, pending.creationKey);
        conversationId = result.id;
        targetKey = bindConversation(targetKey, conversationId, pending.skillId);
      }
      const result = await post<{ user: Message; agent: Message }>(`/real-conversations/${encodeURIComponent(conversationId)}/messages`, pending.payload, pending.id);
      const current = thread(targetKey);
      const byId = new Map(current.messages.map(message => [message.id, message]));
      byId.set(result.user.id, result.user); byId.set(result.agent.id, result.agent);
      updateThread(targetKey, { messages: [...byId.values()], loaded: true, pending: current.pending.filter(item => item.id !== pending.id), error: "" });
      await Promise.allSettled([loadMessages(targetKey, conversationId), refresh()]);
    } catch (error) {
      // The human turn may already be committed even when the model reply failed.
      if (conversationId) await loadMessages(targetKey, conversationId);
      updatePending(targetKey, pending.id, { conversationId, status: "FAILED", error: error instanceof Error ? error.message : "回复没有完成，请重试。" });
      await refresh().catch(() => {});
    }
  }
  function send(event?: FormEvent) {
    event?.preventDefault();
    if (data.upgrade && !modelConfigured(data)) { openModels(); return; }
    if (!skill || !selection || !view.draft.trim() || sending || skill.status !== "ACTIVE") return;
    const pending: Pending = { id: crypto.randomUUID(), creationKey: crypto.randomUUID(), conversationId: selection.conversationId, skillId: skill.id, payload: { text: view.draft.trim(), remember: view.remember, domain: view.domain }, status: "SENDING", createdAt: new Date().toISOString() };
    updateThread(key, { draft: "", remember: false, pending: [...view.pending, pending], forceBottom: true });
    void deliver(key, pending);
  }
  function fillTopic(text: string) { updateThread(key, { draft: text }); composer.current?.focus(); }
  function scrollBottom() { if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight; updateThread(key, { nearBottom: true, forceBottom: false }); setNewBelow(false); }
  function admit(message: Message) {
    setMessageMenu("");
    confirm({ title: "将这条话纳入长期记忆", body: "这条真人消息会成为可追溯的真实来源，用于整理自我镜像。人物的 AI 回复不会被当成你的真实经历。", button: "确认纳入", work: async () => { await post(`/real-messages/${encodeURIComponent(message.id)}/admit`); if (selection?.conversationId) await loadMessages(key, selection.conversationId); } });
  }
  function retryMessage(message: Message) {
    if (!selection?.conversationId || !skill || !message.client_request_id || !message.retry_payload || sending) return;
    const pending: Pending = { id: message.client_request_id, creationKey: crypto.randomUUID(), conversationId: selection.conversationId, skillId: skill.id, payload: message.retry_payload, status: "SENDING", createdAt: message.created_at };
    updateThread(key, { pending: [...view.pending, pending] }); void deliver(key, pending);
  }

  const renderedMessages: (Message & { pending?: Pending })[] = view.messages.map(message => ({ ...message, pending: message.speaker === "USER" ? view.pending.find(pending => pending.id === message.client_request_id) : undefined }));
  for (const pending of view.pending) {
    if (!view.messages.some(message => message.client_request_id === pending.id)) renderedMessages.push({ id: `pending:${pending.id}`, speaker: "USER", text: pending.payload.text, created_at: pending.createdAt, pending });
  }
  return <div className={`im-workspace ${memory.mobileChat ? "mobile-conversation" : "mobile-list"} ${profileOpen ? "profile-visible" : ""}`}>
    <section className="people-column" aria-label="人物与最近对话">
      <header className="people-heading"><div className="people-brand"><span className="im-logo" aria-hidden="true"><Glyph name="memory" size={20} /></span><h2>我们曾聊过</h2></div><button className="icon-button" aria-label="添加人物" title="添加人物" onClick={() => setImportOpen(true)}><Glyph name="plus" /></button></header>
      <label className="people-search"><Glyph name="search" size={17} /><span className="sr-only">搜索人物或对话</span><input value={memory.query} onChange={event => { memory.query = event.target.value; emit(); }} placeholder="搜索人物、领域或对话" />{memory.query && <button className="icon-button" aria-label="清空搜索" onClick={() => { memory.query = ""; emit(); }}><Glyph name="close" size={14} /></button>}</label>
      <div className="people-tabs" role="tablist" aria-label="列表类型"><button role="tab" aria-selected={memory.tab === "people"} className={memory.tab === "people" ? "active" : ""} onClick={() => { memory.tab = "people"; emit(); }}>人物<span>{skills.length}</span></button><button role="tab" aria-selected={memory.tab === "recent"} className={memory.tab === "recent" ? "active" : ""} onClick={() => { memory.tab = "recent"; emit(); }}>最近对话{conversations.length > 0 && <span>{conversations.length}</span>}</button></div>
      {memory.tab === "people" && <div className="domain-filters" aria-label="按领域筛选">{domains.map(domain => <button key={domain} aria-pressed={memory.domain === domain} className={memory.domain === domain ? "active" : ""} onClick={() => { memory.domain = domain; emit(); }}>{domain}</button>)}</div>}
      <div className="people-list" role="tabpanel" aria-label={memory.tab === "people" ? "人物列表" : "最近对话列表"}>
        {memory.tab === "people" ? matchingSkills.length ? matchingSkills.map(person => {
          const recent = conversations.find(c => c.skill_id === person.id);
          return <button key={person.id} data-testid="contact-row" data-builtin={person.builtin ? "true" : "false"} data-slug={person.slug} className={`contact-row ${skill?.id === person.id ? "selected" : ""}`} onClick={() => selectPerson(person)} aria-label={`与${person.display_name}聊天`}><Portrait skill={person} /><span className="contact-copy"><span className="contact-name"><strong>{person.display_name}</strong>{pinned.includes(person.slug || "") && <span className="pinned-mark" aria-label="置顶人物" />}{recent?.last_message_at && <time>{recentTime(recent.last_message_at)}</time>}</span><span className="contact-preview">{recent?.last_message_preview || person.description}</span><span className="contact-meta">{person.domain || "我的人物"}<span>AI 角色</span></span></span></button>;
        }) : <div className="list-empty"><Glyph name="search" size={25} /><p>没有找到相符的人物</p><span>试试姓名、话题或另一个领域。</span></div> : matchingConversations.length ? matchingConversations.map(conversation => {
          const person = skills.find(s => s.id === conversation.skill_id);
          return <button key={conversation.id} data-testid="recent-row" className={`contact-row recent-row ${selection?.conversationId === conversation.id ? "selected" : ""}`} onClick={() => choose({ skillId: conversation.skill_id, conversationId: conversation.id })}><Portrait skill={person} /><span className="contact-copy"><span className="contact-name"><strong>{person?.display_name || "历史人物"}</strong><time>{recentTime(conversationTime(conversation))}</time></span><span className="contact-preview">{conversation.last_message_preview || conversation.title}</span><span className="contact-meta">{conversation.title}</span></span></button>;
        }) : <div className="list-empty"><Glyph name="memory" size={27} /><p>对话会从第一句话开始</p><span>选择一位人物，聊一个你关心的问题。</span></div>}
      </div>
      <div className="people-footer"><span className="local-dot" />本地私人空间<span>{data.privacy.provider === "mock" ? "演示模式" : "外部模型"}</span></div>
    </section>

    <section className="im-chat" aria-label={skill ? `与${skill.display_name}的对话` : "聊天"}>
      <header className="im-chat-header"><button className="icon-button mobile-back" aria-label="返回人物列表" onClick={() => { memory.mobileChat = false; emit(); }}><Glyph name="back" /></button><button className="chat-person" onClick={() => setProfileOpen(!profileOpen)} aria-expanded={profileOpen} aria-label="查看人物资料"><Portrait skill={skill} size={42} /><span><strong>{skill?.display_name || "选择一位人物"}</strong><small>解释性 AI 角色<span>·</span>{data.upgrade && !modelConfigured(data) ? "待配置模型 · 离线演示" : data.privacy.provider === "mock" ? "离线演示" : "AI 生成回复"}</small></span></button><div className="chat-header-actions">{data.upgrade && !modelConfigured(data) && <button className="header-text-button" aria-label="配置模型" onClick={openModels}><Glyph name="info" size={17} />配置模型</button>}<button className="header-text-button" onClick={newTopic} disabled={!skill || (sending && !selection?.conversationId)}><Glyph name="plus" size={17} />新话题</button><button className={`icon-button ${profileOpen ? "active" : ""}`} aria-label="打开人物资料" onClick={() => setProfileOpen(!profileOpen)}><Glyph name="more" size={23} /></button></div></header>
      <div className="im-message-area" ref={viewport} data-testid="message-scroll" role="log" aria-label="消息记录" aria-live="polite" onScroll={event => {
        const element = event.currentTarget; const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 70;
        const stored = thread(key); stored.scrollTop = element.scrollTop; stored.hasScroll = true; stored.nearBottom = nearBottom;
        if (nearBottom && newBelow) setNewBelow(false);
      }}>
        {view.error && <div className="chat-inline-error" role="alert">{view.error}<button onClick={() => selection?.conversationId && void loadMessages(key, selection.conversationId)}>重新加载</button></div>}
        {view.loading && !view.loaded && <div className="loading-messages">正在读取这段对话…</div>}
        {!renderedMessages.length && !view.loading && <div className="contact-welcome"><Portrait skill={skill} size={92} /><h2>{skill?.display_name || "从一个好问题开始"}</h2><p>{skill?.description || "选一位人物，说说此刻关心的事。"}</p><span className="role-disclosure">这是基于资料的解释性 AI 角色，并非本人。</span>{data.privacy.provider === "mock" && <span className="mock-disclosure">当前使用离线演示模板，不是实际人物推理。</span>}<div className="welcome-topics">{(skill?.starter_prompts || []).slice(0, 3).map(topic => <button key={topic} onClick={() => fillTopic(topic)}>{topic}<Glyph name="arrow" size={16} /></button>)}</div><span className="preview-note">发送第一句话后才会创建对话</span></div>}
        {renderedMessages.map((message, index) => {
          const previous = renderedMessages[index - 1];
          const showTime = !previous || new Date(message.created_at).getTime() - new Date(previous.created_at).getTime() > 5 * 60 * 1000;
          const deleted = message.deleted || ["[已删除]", "[关联来源已删除]"].includes(message.text);
          const evidence = data.evidence.find(item => item.id === message.evidence_id);
          const remembered = Boolean(evidence && !["DELETED", "REJECTED"].includes(evidence.status));
          return <div className="message-group" key={message.id}>{showTime && <div className="message-time">{moment(message.created_at)}</div>}<article data-testid="message-row" data-speaker={message.speaker} className={`im-message ${message.speaker === "USER" ? "outgoing" : "incoming"} ${deleted ? "redacted-message" : ""}`}><span className="message-avatar">{message.speaker === "USER" ? <Avatar url={data.upgrade?.me.avatar_url} name={data.upgrade?.me.nickname} size={38} /> : <Portrait skill={skill} size={38} />}</span><div className="message-body"><div className="bubble-meta">{message.speaker === "USER" ? <span>我<span className="source-real">真人消息</span></span> : <span>{skill?.display_name || "人物角色"}<span className="source-ai">AI 回复</span></span>}</div><div className="bubble-and-menu"><p className="message-bubble">{deleted ? message.speaker === "USER" ? "这条消息已删除" : "关联来源已删除，此回复已隐藏" : message.text}</p>{!deleted && message.speaker === "USER" && !message.id.startsWith("pending:") && <div className="bubble-menu-wrap"><button className="message-more icon-button" aria-label="消息操作" aria-expanded={messageMenu === message.id} onClick={() => setMessageMenu(messageMenu === message.id ? "" : message.id)}><Glyph name="more" size={17} /></button>{messageMenu === message.id && <div className="bubble-menu">{remembered ? <button onClick={() => { setMessageMenu(""); navigate("mirror"); }}><Glyph name="memory" size={15} />查看真实证据</button> : <button onClick={() => admit(message)}><Glyph name="memory" size={15} />纳入长期记忆</button>}</div>}</div>}</div><div className="bubble-status">{remembered && !deleted && <span className="memory-marker"><Glyph name="memory" size={12} />已纳入记忆</span>}{message.pending?.status === "SENDING" && <span className="reply-pending">{message.reply_state === "WAITING" ? "消息已保存，等待 AI 回复" : "正在等待 AI 回复…"}</span>}{(message.pending?.status === "FAILED" || message.reply_state === "FAILED") && !deleted && <span className="reply-failed">回复未完成{(message.pending || message.retry_payload) && <button disabled={sending} onClick={() => message.pending ? void deliver(key, message.pending) : retryMessage(message)}><Glyph name="retry" size={12} />重试回复</button>}</span>}</div>{message.pending?.error && <p className="message-error" role="status">{message.pending.error}</p>}</div></article></div>;
        })}
      </div>
      {newBelow && <button className="new-messages-button" onClick={scrollBottom}>查看新回复<Glyph name="down" size={14} /></button>}
      <form className="im-composer" onSubmit={send}><div className="composer-tools"><label className="remember-this"><input type="checkbox" checked={view.remember} onChange={event => updateThread(key, { remember: event.target.checked })} /><Glyph name="memory" size={16} />记住这条</label><label className="composer-domain"><span className="sr-only">本条消息的情境</span><select aria-label="本条消息的情境" value={view.domain} onChange={event => updateThread(key, { domain: event.target.value })}>{Object.entries(domainLabels).map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select></label><span className="composer-privacy">只有你主动选择的真人发言才进入记忆</span></div><label className="sr-only" htmlFor="im-message-input">写下你想聊的话</label><textarea id="im-message-input" ref={composer} value={view.draft} onChange={event => updateThread(key, { draft: event.target.value })} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); send(); } }} placeholder={skill ? `和${skill.display_name}聊点什么…` : "请先选择一位人物"} rows={2} maxLength={10000} disabled={!skill} /><div className="im-composer-bottom"><span>{sending ? "回复会留在这段对话，你可以先看看其他人物。" : "Enter 发送，Shift + Enter 换行"}</span><button className="button-primary send-im" disabled={!skill || !view.draft.trim() || sending || skill.status !== "ACTIVE"} aria-label="发送消息">发送<Glyph name="send" size={16} /></button></div></form>
    </section>

    {profileOpen && <><button className="profile-backdrop" aria-label="关闭人物资料" onClick={() => setProfileOpen(false)} /><aside ref={profile} className="person-profile" aria-label="人物资料" role="dialog"><header><h2>人物资料</h2><button className="icon-button" aria-label="关闭人物资料" onClick={() => setProfileOpen(false)}><Glyph name="close" /></button></header><div className="profile-person"><Portrait skill={skill} size={86} /><h3>{skill?.display_name || "历史人物"}</h3><span className="profile-ai-label">解释性 AI 角色</span><p>{skill?.description}</p></div><section className="profile-section"><h3>可以从这里聊起</h3><div className="profile-topics">{(skill?.starter_prompts || []).slice(0, 3).map(topic => <button key={topic} onClick={() => { fillTopic(topic); setProfileOpen(false); }}>{topic}<Glyph name="arrow" size={15} /></button>)}</div></section><section className="profile-section"><h3>资料来源</h3><p>{skill?.source || "历史导入资料，详见原授权记录。"}</p>{skill?.source_version && <p className="profile-small">资料版本：{skill.source_version}</p>}{skill?.license && <p className="profile-small">使用说明：{skill.license}</p>}<span className="profile-disclosure">人物的发言均为 AI 生成，不代表本人观点或授权背书。</span></section><section className="profile-section"><h3>与你聊过的话题</h3>{conversations.filter(c => c.skill_id === skill?.id).length ? conversations.filter(c => c.skill_id === skill?.id).map(conversation => <button className="profile-conversation" key={conversation.id} onClick={() => choose({ skillId: conversation.skill_id, conversationId: conversation.id })}><span>{conversation.title}</span><small>{recentTime(conversationTime(conversation))}</small></button>) : <p className="profile-small">还没有对话。发送第一句话后会留在这里。</p>}<button className="profile-link" onClick={newTopic}><Glyph name="plus" size={15} />开启新话题</button></section><section className="profile-section"><h3>管理人物</h3>{skill && <><AvatarControls path={`/skills/${encodeURIComponent(skill.id)}`} act={act} /><button className="profile-link quiet-danger" onClick={() => confirm({ title: "移除这个人物", body: "人物会从可用列表移除，并停止新回复。历史聊天和你的真实记忆会保留，可以从内置目录重新添加。", button: "移除人物", danger: true, work: async () => { await request(`/skills/${encodeURIComponent(skill.id)}`, { method: "DELETE" }); setProfileOpen(false); } })}>移除人物</button></>}</section><section className="profile-section"><h3>我的记录</h3><button className="profile-link" onClick={() => navigate("mirror")}><Glyph name="memory" size={16} />查看真实证据</button><button className="profile-link" onClick={() => navigate("privacy")}><Glyph name="info" size={16} />数据与隐私</button></section></aside></>}
    {importOpen && <PersonManagement shared={shared} close={() => setImportOpen(false)} />}
  </div>;
}
