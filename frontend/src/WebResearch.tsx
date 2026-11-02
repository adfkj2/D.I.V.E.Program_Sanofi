import { useEffect, useRef, useState, type ReactNode } from "react";
import { post, request, type ModelConnection, type ResearchSource, type ResearchSources, type WebConfig, type WebProvider } from "./api";
import { TypingIndicator } from "./TypingIndicator";
import "./webResearch.css";

function message(error: unknown) { return error instanceof Error ? error.message : "操作没有完成，请重试。"; }
export const webConfigured = (config?: WebConfig) => Boolean(config?.configured || config?.key_status === "CONFIGURED" || config?.key_state === "CONFIGURED");
const providerNames: Record<string, string> = { deepseek: "DeepSeek 搜索", kimi: "Kimi 搜索", glm: "智谱搜索", bocha: "博查搜索", qwen: "通义千问联网", tavily: "Tavily" };
const providerLabel = (config?: WebConfig) => config?.provider_label || providerNames[config?.provider || ""] || config?.provider || "选择联网来源";
const storageLabel = (config?: WebConfig) => config?.credential_mode === "CONNECTION" ? "复用已保存的模型连接 Key" : config?.storage === "OS_CREDENTIAL_STORE" ? "已保存 Key" : "仅本次运行有效";
function reusableConnection(connection: ModelConnection, provider?: string) {
  if (!provider || connection.provider !== provider || connection.key_status !== "CONFIGURED") return false;
  const hosts: Record<string, string> = { deepseek: "api.deepseek.com", kimi: "api.moonshot.cn", glm: "open.bigmodel.cn" };
  try { const url = new URL(connection.base_url || ""); return url.protocol === "https:" && url.hostname === hosts[provider] && (!url.port || url.port === "443") && !url.username && !url.password && !url.search && !url.hash; } catch { return false; }
}

function WebDialog({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; dialog.current?.showModal(); return () => { dialog.current?.close(); previous?.focus(); }; }, []);
  return <dialog className="upgrade-dialog web-dialog" ref={dialog} aria-label={title} onCancel={event => { event.preventDefault(); close(); }}><header><h2>{title}</h2><button type="button" className="icon-button" aria-label={`关闭${title}`} onClick={close}>✕</button></header><div className="upgrade-dialog-content">{children}</div></dialog>;
}

interface WebValidation {status:"ADDED"|"UPDATED"|"TEST_FAILED"|"SAVE_FAILED";tested:boolean;saved:boolean;elapsed_ms:number;message:string;config?:WebConfig;}

function WebConfigForm({ initial, refresh, close }: { initial?: WebConfig; refresh: () => Promise<void>; close: () => void }) {
  const [config, setConfig] = useState(initial);
  const [providers, setProviders] = useState<WebProvider[]>([]);
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [provider, setProvider] = useState(initial?.provider || "");
  const [credentialMode, setCredentialMode] = useState<"CONNECTION" | "SEPARATE">(initial?.credential_mode || "SEPARATE");
  const [connectionId, setConnectionId] = useState(initial?.connection_id || "");
  const [model, setModel] = useState(initial?.model || "");
  const [engine, setEngine] = useState(initial?.engine || "");
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [apiKey, setApiKey] = useState("");
  const [remember, setRemember] = useState(Boolean(initial?.remember));
  const [enabled, setEnabled] = useState(webConfigured(initial) ? initial?.enabled !== false : true);
  const [replacing, setReplacing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [result, setResult] = useState<WebValidation | null>(null);
  const running = useRef(false), mounted = useRef(true), revision = useRef(0);
  const feedback = useRef<HTMLElement>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; revision.current += 1; }; }, []);
  useEffect(() => {
    let current = true;
    Promise.all([request<WebProvider[]>("/web/providers"), request<ModelConnection[]>("/model-connections")]).then(([choices, savedConnections]) => {
      if (!current) return;
      if (!Array.isArray(choices) || !choices.length || !Array.isArray(savedConnections)) throw new Error("暂时未能读取联网来源，请关闭窗口后重试。");
      setProviders(choices); setConnections(savedConnections);
      const preserve = webConfigured(initial) || (initial?.revision || 0) > 0;
      const selected = choices.find(item => preserve && item.id === initial?.provider) || choices.find(item => item.id === "deepseek") || choices.find(item => item.recommended) || choices[0];
      setProvider(selected.id);
      const reusable = savedConnections.filter(item => reusableConnection(item, selected.reuse_provider));
      if (!preserve && reusable.length) { setCredentialMode("CONNECTION"); setConnectionId(reusable.find(item => item.is_default)?.id || reusable[0].id); }
      setModel(preserve ? initial?.model || selected.default_model || "" : selected.default_model || "");
      setEngine(preserve ? initial?.engine || selected.default_engine || "" : selected.default_engine || "");
    }).catch(reason => { if (current) setError(message(reason)); }).finally(() => { if (current) setLoadingOptions(false); });
    return () => { current = false; };
  }, []);
  useEffect(() => { feedback.current?.focus({ preventScroll: true }); }, [result]);
  function changed(update: () => void) { revision.current += 1; setResult(null); setInfo(""); setError(""); update(); }
  const selectedProvider = providers.find(item => item.id === provider);
  const reusable = connections.filter(item => reusableConnection(item, selectedProvider?.reuse_provider));
  const selectedConnection = reusable.find(item => item.id === connectionId);
  const configured = webConfigured(config) && config?.provider === provider && config?.credential_mode !== "CONNECTION";
  const keyMismatch = provider === "tavily" && apiKey.trim().startsWith("sk-") ? "当前选择的是 Tavily，请填写 Tavily Key；DeepSeek、Kimi 等模型 Key 请切换到对应联网来源后使用。" : provider !== "tavily" && apiKey.trim().startsWith("tvly-") ? "这看起来是 Tavily Key，请切换到 Tavily，或填写当前供应商的 Key。" : "";
  function chooseProvider(id: string) {
    changed(() => {
      const selected = providers.find(item => item.id === id);
      const saved = connections.filter(item => reusableConnection(item, selected?.reuse_provider));
      setProvider(id); setApiKey(""); setReplacing(false);
      setCredentialMode(saved.length ? "CONNECTION" : "SEPARATE");
      setConnectionId(saved.find(item => item.is_default)?.id || saved[0]?.id || "");
      setModel(selected?.default_model || ""); setEngine(selected?.default_engine || "");
    });
  }
  async function save() {
    if (running.current || !selectedProvider || keyMismatch || credentialMode === "CONNECTION" && !selectedConnection) return;
    running.current = true; const submitted = revision.current; setBusy(true); setError(""); setInfo(""); setResult(null);
    try {
      const value = await post<WebValidation>("/web/config/validate", { provider, credential_mode: credentialMode,
        ...(credentialMode === "CONNECTION" ? { connection_id: connectionId } : { ...(apiKey ? { api_key: apiKey } : {}), remember_key: remember }),
        ...(selectedProvider.requires_model ? { model } : {}), ...(engine ? { engine } : {}), enabled });
      if (!mounted.current) { if (value.saved) await refresh().catch(() => {}); return; }
      if (submitted !== revision.current) { setInfo(value.saved ? "上一次提交已保存；当前修改尚未保存，请重新测试。" : "内容已修改，请重新测试并保存。"); if (value.saved) await refresh().catch(() => {}); return; }
      setResult(value);
      if (value.saved && value.config) { setConfig(value.config); setReplacing(false); setApiKey(""); await refresh().catch(() => {}); }
    } catch (reason) { if (mounted.current && submitted === revision.current) setError(message(reason)); }
    finally { running.current = false; if (mounted.current) setBusy(false); }
  }
  return <form className="stack-form web-config-form" aria-label="联网查询配置" aria-busy={busy} onSubmit={event => { event.preventDefault(); void save(); }}>
    <p className="up-note web-config-intro">聊天模型负责人物回复，联网来源负责查资料。两者可以分开选择，普通聊天和虚拟空间共用这份联网配置。</p>
    <label className="field">联网来源<select aria-label="联网来源" value={provider} disabled={loadingOptions || !providers.length} onChange={event => chooseProvider(event.target.value)}>{!providers.length && <option value="">{loadingOptions ? "正在读取联网来源…" : "联网来源暂不可用"}</option>}{providers.map(item => <option value={item.id} key={item.id}>{item.name}{item.id === "deepseek" || item.recommended ? "（推荐）" : ""}</option>)}</select></label>
    {selectedProvider && <div className="web-provider-description"><strong>{selectedProvider.name}</strong><p>{selectedProvider.description || "只查询回答所需的公开资料，不会更换聊天模型。"}</p>{selectedProvider.docs_url && safeSourceUrl(selectedProvider.docs_url) && <a href={safeSourceUrl(selectedProvider.docs_url)!} target="_blank" rel="noopener noreferrer">查看官方说明</a>}</div>}
    {selectedProvider?.reuse_provider && <fieldset className="web-credential-options"><legend>Key 使用方式</legend><label><input type="radio" name="web-credential-mode" checked={credentialMode === "CONNECTION"} disabled={!reusable.length} onChange={() => changed(() => { setCredentialMode("CONNECTION"); setConnectionId(connectionId || reusable[0]?.id || ""); setApiKey(""); setReplacing(false); })}/><span>复用已保存的模型连接{!reusable.length && <small>还没有此供应商的可用连接，可先填写 Key。</small>}</span></label><label><input type="radio" name="web-credential-mode" checked={credentialMode === "SEPARATE"} onChange={() => changed(() => { setCredentialMode("SEPARATE"); setApiKey(""); setReplacing(false); })}/><span>单独填写此服务的 Key</span></label></fieldset>}
    {loadingOptions && <p className="up-note" role="status">正在读取联网来源与已保存连接…</p>}
    {!loadingOptions && selectedProvider && (credentialMode === "CONNECTION" && selectedProvider.reuse_provider ? <><label className="field">已保存的模型连接<select aria-label="已保存的模型连接" value={connectionId} onChange={event => changed(() => setConnectionId(event.target.value))}>{!selectedConnection && <option value="">请选择可用连接</option>}{reusable.map(item => <option value={item.id} key={item.id}>{item.label || item.model}{item.is_default ? "（当前默认）" : ""}</option>)}</select></label><p className="web-reuse-note">使用这份连接已保存的 Key 查询公开资料，不会更换你的聊天模型，也不需要另外填写 Tavily Key。</p></> : <>
      {configured && !replacing ? <section className="up-key-state" aria-label="搜索 Key 状态"><div><strong>{storageLabel(config)}</strong><span>{config?.api_key_mask || "••••••••"}</span><small>{config?.storage === "OS_CREDENTIAL_STORE" ? "保存在系统安全凭据库，原始 Key 不会回显。" : "本机服务退出后需重新填写。"}</small></div><button type="button" className="text-button" onClick={() => changed(() => setReplacing(true))}>更换搜索 Key</button></section> : <label className="field">搜索 API Key<input type="password" autoComplete="new-password" spellCheck={false} value={apiKey} onChange={event => changed(() => setApiKey(event.target.value))} placeholder={configured ? "留空保留已有 Key" : selectedProvider?.key_hint || `填入${selectedProvider?.name || "所选服务"}的 Key`} required={!configured}/></label>}
      {keyMismatch && <p className="up-error" role="alert">{keyMismatch}</p>}
      <label className="up-checkbox"><input type="checkbox" checked={remember} onChange={event => changed(() => setRemember(event.target.checked))}/>记住搜索 Key，保存在系统安全凭据库</label>
      {initial?.credential_store_available === false && <p className="up-note">系统安全存储当前不可用，可取消勾选，仅在本次运行中使用。</p>}
    </>)}
    {selectedProvider?.requires_model && <label className="field">联网模型<input value={model} onChange={event => changed(() => setModel(event.target.value))} placeholder={selectedProvider.model_hint || "填写支持联网的模型名称"} required/></label>}
    {selectedProvider?.engines?.length ? <label className="field">搜索引擎<select aria-label="搜索引擎" value={engine || selectedProvider.default_engine || (typeof selectedProvider.engines[0] === "string" ? selectedProvider.engines[0] : selectedProvider.engines[0].id)} onChange={event => changed(() => setEngine(event.target.value))}>{selectedProvider.engines.map(item => <option key={typeof item === "string" ? item : item.id} value={typeof item === "string" ? item : item.id}>{typeof item === "string" ? item : item.name}</option>)}</select></label> : null}
    <label className="up-checkbox"><input type="checkbox" checked={enabled} onChange={event => changed(() => setEnabled(event.target.checked))}/>启用按需联网查询</label>
    <p className="up-note">只向搜索服务发送必要的检索词；不发送完整聊天、本人画像或评估答案。搜索与模型调用分别计费，费用未估算。</p>
    {error && <p className="up-error" role="alert">{error}</p>}
    {info && <p className="up-result" role="status">{info}</p>}
    {result && <section ref={feedback} tabIndex={-1} className={`up-validation ${result.saved ? "is-saved" : "is-failed"}`} role={result.saved ? "status" : "alert"} aria-label="联网配置结果"><div className="up-validation-heading"><span aria-hidden="true">{result.saved ? "✓" : "!"}</span><h3>{result.status === "ADDED" ? "联网查询已添加" : result.status === "UPDATED" ? "联网查询已更新" : result.status === "SAVE_FAILED" ? "连接成功，但保存失败" : "测试失败"}</h3></div><p>{selectedProvider?.name || provider} · {((result.elapsed_ms || 0) / 1000).toFixed(2)} 秒</p><p>{result.message}</p>{result.saved ? <><small>{storageLabel(result.config)}。窗口关闭后可在聊天中使用。</small><button type="button" className="button-primary" onClick={close}>完成</button></> : <small>{credentialMode === "CONNECTION" ? "本次没有修改联网配置或原来的模型连接。" : "本次没有保存修改，填写的 Key 仍保留在当前窗口。"}</small>}</section>}
    {!result?.saved && <div className="up-form-actions"><button type="submit" className="button-primary" disabled={busy || loadingOptions || !selectedProvider || Boolean(keyMismatch) || (credentialMode === "CONNECTION" ? !selectedConnection : !apiKey && !configured)}>{busy ? "正在测试并保存…" : "测试并保存"}</button><button type="button" className="text-button" onClick={close}>稍后设置</button></div>}
    <p className="up-note">测试会发出一次简短搜索请求，只有通过后才保存。Key 不会写入聊天、浏览器永久存储或导出文件。</p>
  </form>;
}

export function WebSettings({ config, refresh }: { config?: WebConfig; refresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false), [removing, setRemoving] = useState(false), [error, setError] = useState("");
  const configured = webConfigured(config);
  async function remove() { setRemoving(true); setError(""); try { await request("/web/config", { method: "DELETE" }); await refresh(); } catch (reason) { setError(message(reason)); } finally { setRemoving(false); } }
  return <section className="up-panel web-settings" aria-label="联网查询设置"><div className="up-section-title"><h2>联网查询</h2><span className={`web-config-badge ${configured && config?.enabled ? "enabled" : ""}`}>{configured ? config?.enabled ? "按需查询" : "已关闭" : "未配置"}</span></div><p className="up-note">聊天模型负责人物回复，联网来源负责查资料。可复用支持的模型 Key，也可单独选择搜索服务。</p><div className="web-settings-summary"><div><strong>{configured || (config?.revision || 0) > 0 ? providerLabel(config) : "选择联网来源"}</strong><p>{configured ? `${storageLabel(config)}${config?.api_key_mask ? ` · ${config.api_key_mask}` : ""}` : "境内来源优先，不必另外注册 Tavily"}</p></div><div className="up-form-actions"><button className="button-secondary" onClick={() => setOpen(true)}>{configured ? "修改联网配置" : "配置联网查询"}</button>{configured && <button className="text-button quiet-danger" disabled={removing} onClick={() => void remove()}>移除联网配置</button>}</div></div>{error && <p className="up-error" role="alert">{error}</p>}{open && <WebDialog title="联网查询" close={() => setOpen(false)}><WebConfigForm initial={config} refresh={refresh} close={() => setOpen(false)}/></WebDialog>}</section>;
}

export function ResearchIndicator({ phase }: { phase?: string | null }) {
  const text = phase === "SEARCHING" ? "正在查资料…" : phase === "VERIFYING" ? "正在核对资料…" : "";
  return text ? <span className="chat-typing web-research-phase" role="status" aria-label={text}><span>{text}</span><span className="typing-dots" aria-hidden="true"><i/><i/><i/></span></span> : <TypingIndicator/>;
}

export function ResearchNotice({ status, notice, configure, retry }: { status?: string | null; notice?: string | null; configure: () => void; retry?: () => void }) {
  const actionable: Record<string, { fallback: string; action?: string }> = {
    NOT_CONFIGURED: { fallback: "联网查询尚未配置，当前回复无法核实最新资料。", action: "配置联网查询" },
    DISABLED: { fallback: "联网查询已关闭，当前回复无法核实最新资料。", action: "启用联网查询" },
    NO_RESULTS: { fallback: "没有读到能支持结论的公开资料，可补充条件后继续。", action: "调整联网来源" },
    SNIPPETS_ONLY: { fallback: "本轮只有搜索摘要，关键数字还需要网页正文核对。", action: "更换联网来源" },
    FAILED: { fallback: "本次资料查询未完成。" },
    UNAVAILABLE: { fallback: "联网服务暂时不可用。" },
    TIMEOUT: { fallback: "本次资料查询超时。" },
  };
  const state = actionable[status || ""];
  // Successful research may carry provenance guidance for the sources drawer;
  // it is not a connection failure and must not show a repair prompt in chat.
  if (!state) return null;
  return <div className="web-research-notice" role="status"><span>{notice || state.fallback}</span>{state.action ? <button className="text-button" onClick={configure}>{state.action}</button> : retry ? <button className="text-button" onClick={retry}>重试回复</button> : <button className="text-button" onClick={configure}>检查联网配置</button>}</div>;
}

function safeSourceUrl(value: string) { try { const url = new URL(value); if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null; return url.href; } catch { return null; } }
function dateLabel(value?: string | null) { if (!value) return "未标注"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function SourceItem({ source }: { source: ResearchSource }) {
  const url = safeSourceUrl(source.url), years = source.applicable_years?.join("、") || source.applicable_year;
  const kind = source.evidence_kind?.toUpperCase() || "";
  const kindLabel = kind.includes("SUMMARY") ? "供应商联网总结" : ["SEARCH_EXCERPT", "SEARCH_SNIPPET", "SNIPPET"].includes(kind) ? "搜索摘要" : ["PAGE_CONTENT", "RAW_CONTENT", "WEB_PAGE", "FETCHED_PAGE"].includes(kind) ? "网页正文摘录" : "资料摘录";
  return <article className="web-source-item"><h3>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{source.title || "来源页面"}<span aria-hidden="true"> ↗</span></a> : source.title || "来源页面"}</h3><p className="web-source-publisher">{source.publisher || source.institution || (url ? new URL(url).hostname : "来源地址不可用")}</p><dl><div><dt>资料发布</dt><dd>{dateLabel(source.published_at)}</dd></div><div><dt>本次查询</dt><dd>{dateLabel(source.retrieved_at)}</dd></div>{years && <div><dt>适用年份</dt><dd>{years}</dd></div>}</dl>{source.excerpt && <div className="web-source-excerpt"><span>{kindLabel}</span><p>{source.excerpt}</p></div>}</article>;
}

export function ResearchSourcesButton({ researchId, sourceRefs, privacyKey }: { researchId?: string | null; sourceRefs?: string[]; privacyKey?: number | string }) {
  const [open, setOpen] = useState(false), [data, setData] = useState<ResearchSources | null>(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  useEffect(() => { generation.current += 1; setOpen(false); setData(null); setError(""); }, [researchId, privacyKey]);
  useEffect(() => { if (!open || !researchId) return; const current = ++generation.current; setLoading(true); setError(""); void request<ResearchSources>(`/research-runs/${encodeURIComponent(researchId)}/sources`).then(value => { if (current === generation.current) setData(value); }).catch(reason => { if (current === generation.current) setError(message(reason)); }).finally(() => { if (current === generation.current) setLoading(false); }); return () => { generation.current += 1; }; }, [open, researchId]);
  if (!researchId || !sourceRefs?.length) return null;
  function close() { setOpen(false); setData(null); setError(""); generation.current += 1; }
  const sources = data?.sources.filter(source => sourceRefs?.includes(source.id));
  return <><button type="button" className="web-source-button" aria-haspopup="dialog" onClick={() => setOpen(true)}><svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M4 2h8l4 4v12H4zM11 2v5h5M7 10h6M7 13h6"/></svg>查看依据</button>{open && <WebDialog title="本轮参考资料" close={close}><p className="up-note">以下是这轮回复参考的公开资料。资料年份与查询时间分别保留，便于核对。</p>{loading && <p role="status">正在读取来源…</p>}{error && <div className="up-error" role="alert">{error}<button type="button" className="text-button" onClick={() => { close(); }}>关闭</button></div>}{!loading && !error && !sources?.length && <p className="up-note">关联资料已不可用，可能已随相关记录一同清除。</p>}{sources?.map(source => <SourceItem source={source} key={source.id}/>)}<button type="button" className="button-secondary web-source-done" onClick={close}>关闭资料</button></WebDialog>}</>;
}
