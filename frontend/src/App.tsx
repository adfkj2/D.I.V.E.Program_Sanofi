import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type ComponentProps,
} from "react";
import Chat from "./Chat";
import { MePage, Modal, ModelSettings, ReadOnlyHistory, SettingsPage, VirtualSpace } from "./Upgrade";
import {
  connect,
  post,
  request,
  type AppState,
  type Conversation,
  type Directive,
  type Evidence,
  type Message,
  type Simulation,
} from "./api";

export type Page = "chat" | "me" | "settings" | "virtual" | "mirror" | "chosen" | "simulation" | "growth" | "privacy";
type Act = (label: string, work: () => Promise<unknown>, concurrent?: boolean) => Promise<boolean>;
type Confirmation = {
  title: string;
  body: string;
  button: string;
  danger?: boolean;
  work: () => Promise<unknown>;
};
export type Shared = {
  data: AppState;
  busy: boolean;
  act: Act;
  confirm: (value: Confirmation) => void;
  navigate: (page: Page) => void;
  refresh: () => Promise<void>;
  openModels: () => void;
};
const pages: { id: Page; title: string; icon: string; subtitle: string }[] = [
  { id: "chat", title: "聊天", icon: "chat", subtitle: "从一段真实的对话开始" },
  { id: "me", title: "我", icon: "user", subtitle: "从真实的记录，慢慢认识自己" },
  { id: "settings", title: "设置", icon: "settings", subtitle: "模型连接、数据与使用偏好" },
  { id: "virtual", title: "虚拟空间", icon: "simulation", subtitle: "让两种思考，慢慢相遇" },
  {
    id: "mirror",
    title: "看见自己",
    icon: "mirror",
    subtitle: "自我陈述、行为线索与未来选择",
  },
  {
    id: "chosen",
    title: "理想的我",
    icon: "leaf",
    subtitle: "你来选择，想成为怎样的自己",
  },
  {
    id: "simulation",
    title: "过往模拟记录",
    icon: "simulation",
    subtitle: "在另一个时空里，观察一种可能",
  },
  {
    id: "growth",
    title: "带回生活",
    icon: "growth",
    subtitle: "一次小小的、自愿的现实尝试",
  },
  {
    id: "privacy",
    title: "数据与隐私",
    icon: "privacy",
    subtitle: "你的记录，由你决定如何保留",
  },
];
const domains: Record<string, string> = {
  general: "日常思考",
  career: "工作与选择",
  relationships: "人与关系",
  learning: "学习与创造",
};
const states: Record<string, string> = {
  ACTIVE: "可使用",
  VALID: "可使用",
  COMPILED: "可使用",
  INVALIDATED: "来源已变更",
  RETIRED: "已停用",
  PROPOSED: "等待你确认",
  APPROVED: "已由你确认",
  USER_APPROVED: "已由你确认",
  REVOKED: "已撤回",
  CREATED: "尚未开始",
  RUNNING: "进行中",
  GENERATING: "正在生成",
  PAUSED: "已暂停",
  COMPLETED: "已结束",
  CANCELLED: "已停止",
  FAILED: "暂时未完成",
  HYPOTHESIS: "待验证的线索",
  SUPPORTED: "有证据支持",
  CONFLICTED: "存在不同线索",
  INSUFFICIENT: "证据仍不足",
  PENDING: "等待处理",
  APPLIED: "已纳入",
  SELF_REPORTED: "本人自报",
  DELETED: "已删除",
};
function label(value: string) {
  return states[value] || value;
}
function date(value?: string) {
  return value
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "long",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(value))
    : "";
}
function approved(directive: Directive) {
  return ["APPROVED", "USER_APPROVED", "ACTIVE"].includes(directive.state);
}
function activeVersion(state: string) {
  return ["ACTIVE", "VALID", "COMPILED"].includes(state);
}
function initialPage(): Page {
  const hash = window.location.hash.slice(1);
  if (hash === "simulate") return "simulation";
  return pages.some((p) => p.id === hash) ? (hash as Page) : "chat";
}

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    user: <><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></>,
    settings: <><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/></>,
    chat: (
      <>
        <path d="M5 5h14v10H9l-4 4V5Z" />
        <path d="M9 9h6m-6 3h4" />
      </>
    ),
    mirror: (
      <>
        <rect x="4" y="4" width="6" height="16" rx="3" />
        <rect x="14" y="4" width="6" height="16" rx="3" />
        <path d="M12 2v20" />
      </>
    ),
    leaf: (
      <>
        <path d="M19 4C9 3 4 7 5 13c1 5 7 7 11 3 3-3 3-7 3-12Z" />
        <path d="m5 20 9-10m-5 5v-4" />
      </>
    ),
    simulation: (
      <>
        <path d="M4 6h10v8H8l-4 3V6Zm14 2h2v12l-4-3h-5" />
        <path d="M8 10h2" />
      </>
    ),
    growth: (
      <>
        <path d="M5 20h14M8 20v-7m8 7v-7m-4 7V6m-3 3 3-3 3 3" />
      </>
    ),
    privacy: (
      <>
        <path d="m12 3 7 3v6c0 4-7 9-7 9s-7-5-7-9V6l7-3Z" />
        <path d="m9 12 2 2 4-4" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: (
      <>
        <path d="M4 12h15m-6-6 6 6-6 6" />
      </>
    ),
    send: (
      <>
        <path d="m4 4 17 8-17 8 3-8-3-8Z" />
        <path d="M7 12h14" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    evidence: (
      <>
        <path d="M6 3h9l4 4v14H6V3Z" />
        <path d="M14 3v5h5M9 12h7m-7 4h5" />
      </>
    ),
    pause: (
      <>
        <path d="M8 5v14m8-14v14" />
      </>
    ),
    play: <path d="m8 4 12 8-12 8V4Z" />,
    down: <path d="m6 9 6 6 6-6" />,
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.chat}
    </svg>
  );
}

function Button({
  children,
  secondary = false,
  className = "",
  ...props
}: ComponentProps<"button"> & { secondary?: boolean }) {
  return (
    <button
      className={`${secondary ? "button-secondary" : "button-primary"} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-mark" aria-hidden="true">
        ∴
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
function DomainSelect({
  value,
  onChange,
  id = "domain",
}: {
  value: string;
  onChange: (value: string) => void;
  id?: string;
}) {
  return (
    <label className="field">
      适用情境
      <select id={id} aria-label="适用情境" value={value} onChange={(e) => onChange(e.target.value)}>
        {Object.entries(domains).map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>(initialPage);
  const [data, setData] = useState<AppState | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyLabel, setBusyLabel] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [modelsOpen, setModelsOpen] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const busy = Boolean(busyLabel);
  const current = pages.find((p) => p.id === page)!;

  async function load() {
    setConnecting(true);
    setError("");
    try {
      setData(await connect());
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "还没有连接到本机服务。",
      );
    } finally {
      setConnecting(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  useEffect(() => {
    if (data?.upgrade && !data.upgrade.connections.length && !data.upgrade.preferences.setup_dismissed) setModelsOpen(true);
  }, [data?.upgrade?.connections.length, data?.upgrade?.preferences.setup_dismissed]);
  useEffect(() => {
    if (!data?.upgrade) return;
    const timer = setInterval(() => { void request<AppState>("/state").then(setData).catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  }, [Boolean(data?.upgrade)]);
  useEffect(() => {
    const listener = () => setPage(initialPage());
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  useEffect(() => {
    document.title = `${current.title} · 我们曾聊过`;
  }, [current.title]);
  useEffect(() => {
    if (!confirmation) return;
    previousFocus.current = document.activeElement as HTMLElement;
    dialogRef.current?.showModal();
    cancelRef.current?.focus();
    return () => {
      dialogRef.current?.close();
      previousFocus.current?.focus();
    };
  }, [confirmation]);

  const act: Act = async (title, work, concurrent = false) => {
    if (busy && !concurrent) return false;
    if (!concurrent) setBusyLabel(title);
    setError("");
    setNotice("");
    try {
      await work();
      setData(await request<AppState>("/state"));
      setNotice(title);
      return true;
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "操作没有完成，请重试。",
      );
      return false;
    } finally {
      if (!concurrent) setBusyLabel("");
    }
  };
  function navigate(next: Page) {
    setPage(next);
    window.location.hash = next;
    setError("");
    setNotice("");
  }
  const shared: Shared | null = data
    ? { data, busy, act, confirm: setConfirmation, navigate, refresh: async () => { setData(await request<AppState>("/state")); }, openModels: () => setModelsOpen(true) }
    : null;

  return (
    <div className={`app-shell upgrade-app ${page === "chat" ? "im-app" : ""} ${page === "virtual" ? "virtual-app" : ""}`}>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar" aria-label="主导航">
        <a className="brand" href="#chat" onClick={() => navigate("chat")}>
          <span className="brand-symbol" aria-hidden="true">
            聊
          </span>
          <span>
            <strong>我们曾聊过</strong>
            <small>We once talked</small>
          </span>
        </a>
        <p className="sidebar-caption">留一处地方，慢慢认识自己。</p>
        <nav>
          {pages.filter(item => ["chat", "me", "settings"].includes(item.id) || (item.id === "virtual" && data?.upgrade?.unlock.unlocked)).map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(item.id)}
              className={`nav-item ${(page === item.id || (item.id === "me" && ["mirror","chosen","growth","simulation"].includes(page)) || (item.id === "settings" && page === "privacy")) ? "selected" : ""}`}
              title={item.title}
              aria-current={page === item.id ? "page" : undefined}
            >
              <Icon name={item.icon} />
              <span>{item.title}</span>
              {page === item.id && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className="local-status">
            <i />
            本地私人空间
          </span>
          <p>
            真实的经历，和想象的可能，
            <br />
            在这里各有位置。
          </p>
          <span className="edition">研究预览版</span>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div>
            <span className="mobile-brand">我们曾聊过</span>
            <span className="space-label">我的思考空间</span>
            <span className="header-separator">/</span>
            <span>{current.title}</span>
          </div>
          <button
            className="provider-status"
            onClick={() => setModelsOpen(true)}
          >
            <span className="status-dot" />
            {data?.privacy.provider === "mock" || !data
              ? "演示模型 · 无外部调用"
              : "外部模型已连接"}
          </button>
        </header>
        <main id="main-content" className={`main-content page-${page}`}>
          <div className="page-heading">
            <div>
              <h1>{current.title}</h1>
              <p>{current.subtitle}</p>
            </div>
            {page === "simulation" && (
              <span className="outline-badge">实验预览 · 未经人格验证</span>
            )}
          </div>
          <div className="announcements" aria-live="polite" aria-atomic="true">
            {error && (
              <div className="alert error" role="alert">
                <span>{error}</span>
                <button aria-label="关闭错误提示" onClick={() => setError("")}>
                  <Icon name="close" size={16} />
                </button>
              </div>
            )}
            {notice && (
              <div className="alert success">
                <span>{notice}</span>
                <button aria-label="关闭操作提示" onClick={() => setNotice("")}>
                  <Icon name="close" size={16} />
                </button>
              </div>
            )}
            {busy && <span className="working">正在处理，请稍候…</span>}
          </div>
          {!data ? (
            <div className="connection-state">
              <span className="connection-glyph" aria-hidden="true">
                ﹁
              </span>
              <h2>
                {connecting ? "正在打开你的空间" : "还没有连接到本机服务"}
              </h2>
              <p>
                {connecting
                  ? "稍等片刻。"
                  : "请启动“我们曾聊过”本地服务，然后重新连接。"}
              </p>
              {!connecting && (
                <Button onClick={() => void load()}>重新连接</Button>
              )}
            </div>
          ) : (
            shared && (
              <>
                {page === "chat" && <Chat {...shared} />}
                {page === "me" && <MePage {...shared} />}
                {page === "settings" && <SettingsPage {...shared} />}
                {page === "virtual" && <VirtualSpace {...shared} />}
                {page === "mirror" && <Mirror {...shared} />}
                {page === "chosen" && <Chosen {...shared} />}
                {page === "simulation" && <ReadOnlyHistory {...shared} />}
                {page === "growth" && <GrowthView {...shared} />}
                {page === "privacy" && <Privacy {...shared} />}
              </>
            )
          )}
        </main>
        <footer className="site-footer">
          <span>在另一个时空里，另一个我，与你聊了很久。</span>
          <span>真实证据与 AI 模拟始终分开</span>
        </footer>
      </div>
      {modelsOpen && shared && <Modal title="模型配置" close={() => { setModelsOpen(false); void post("/preferences", {setup_dismissed:true}).then(shared.refresh); }}><ModelSettings {...shared} firstRun={!data?.upgrade?.connections.length} onDone={() => setModelsOpen(false)} /></Modal>}
      {confirmation && (
        <dialog
          ref={dialogRef}
          className="confirm-dialog"
          onCancel={(e) => {
            if (busy) e.preventDefault();
            else setConfirmation(null);
          }}
          aria-labelledby="confirm-title"
          aria-describedby="confirm-description"
        >
          <div className="dialog-inner">
            <h2 id="confirm-title">{confirmation.title}</h2>
            <p id="confirm-description">{confirmation.body}</p>
            <div className="dialog-actions">
              <Button
                ref={cancelRef}
                secondary
                onClick={() => setConfirmation(null)}
                disabled={busy}
              >
                暂不操作
              </Button>
              <button
                className={`${confirmation.danger ? "button-danger" : "button-primary"}`}
                disabled={busy}
                onClick={async () => {
                  const ok = await act(confirmation.title, confirmation.work);
                  if (ok) setConfirmation(null);
                }}
              >
                {busy ? "处理中…" : confirmation.button}
              </button>
            </div>
          </div>
        </dialog>
      )}
    </div>
  );
}

function EvidenceCard({
  evidence,
  busy,
  confirm,
}: {
  evidence: Evidence;
  busy: boolean;
  confirm: Shared["confirm"];
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(evidence.text);
  const originNames: Record<string, string> = {
    REAL_USER_MESSAGE: "真人发言",
    USER_SELF_REPORT: "现实行动自报",
    USER_CORRECTION: "本人更正",
    AUTHORIZED_IMPORT: "已授权导入",
  };
  const claimNames: Record<string, string> = {
    SELF_REPORT: "自我陈述",
    OBSERVATION: "行为自报",
    ASPIRATION: "未来愿望",
    HYPOTHESIS: "待审阅线索",
  };
  return (
    <article className="evidence-card">
      <header>
        <span className="real-badge message-badge">
          {originNames[evidence.origin] || evidence.origin}
        </span>
        <time>{date(evidence.created_at)}</time>
      </header>
      <blockquote>{evidence.text}</blockquote>
      <div className="evidence-meta">
        <span>{domains[evidence.domain] || evidence.domain}</span>
        <span>{claimNames[evidence.claim_kind] || evidence.claim_kind}</span>
      </div>
      <footer>
        <button
          className="text-button"
          onClick={() => setEditing(!editing)}
          aria-expanded={editing}
        >
          更正
        </button>
        <button
          className="text-button quiet-danger"
          disabled={busy}
          onClick={() =>
            confirm({
              title: "删除这条真实证据",
              body: "原始证据会被删除，依赖它的人格版本会失效，相关模拟将无法继续。删除后不能从旧版本恢复正文。",
              button: "删除证据",
              danger: true,
              work: () =>
                request(`/data/evidence/${encodeURIComponent(evidence.id)}`, {
                  method: "DELETE",
                }),
            })
          }
        >
          删除
        </button>
      </footer>
      {editing && (
        <form
          className="correction-form"
          onSubmit={(e) => {
            e.preventDefault();
            confirm({
              title: "保存本人更正",
              body: "更正会成为新的真实来源，依赖旧证据的人格版本会失效。历史推断不会被悄悄改写。",
              button: "确认更正",
              work: () =>
                post(
                  `/evidence/${encodeURIComponent(evidence.id)}/corrections`,
                  { text, domain: evidence.domain },
                ),
            });
          }}
        >
          <label className="field">
            更正后的陈述
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              required
              rows={3}
              maxLength={10000}
            />
          </label>
          <Button secondary disabled={busy || !text.trim()}>
            保存更正
          </Button>
        </form>
      )}
    </article>
  );
}

function Mirror({ data, busy, act, confirm, navigate }: Shared) {
  const [filter, setFilter] = useState("all");
  const [focusEvidence, setFocusEvidence] = useState<string[]>([]);
  const evidence = data.evidence.filter(
    (e) => e.status !== "DELETED" && (filter === "all" || e.domain === filter),
  );
  const tracks = [
    {
      key: "PERCEIVED" as const,
      title: "我如何描述自己",
      caption: "自我陈述",
      empty: "把你对自己的描述留在真聊中。",
    },
    {
      key: "OBSERVED" as const,
      title: "行为里看见的我",
      caption: "条件性的行为线索",
      empty: "来自真实来源的行为线索，会逐渐出现在这里。",
    },
  ];
  return (
    <>
      <div className="mirror-intro">
        <p>
          这三种视角可以不同。每个判断都应该留有证据，也留有你说“不”的空间。
        </p>
        <label className="inline-select">
          情境
          <select aria-label="筛选情境" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">全部情境</option>
            {Object.entries(domains).map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mirror-triptych">
        {tracks.map((track) => {
          const traits = (data.mirror[track.key] || []).filter(
            (t) => filter === "all" || t.domain === filter,
          );
          return (
            <section
              className={`mirror-track track-${track.key.toLowerCase()}`}
              key={track.key}
            >
              <header>
                <span className="track-caption">{track.caption}</span>
                <h2>{track.title}</h2>
              </header>
              {!traits.length ? (
                <div className="track-empty">
                  <span aria-hidden="true">〈 〉</span>
                  <p>{track.empty}</p>
                </div>
              ) : (
                <div className="trait-list">
                  {traits.map((trait) => (
                    <article key={trait.id} className="trait">
                      <span className="trait-status">
                        {label(trait.status)}
                      </span>
                      <h3>{trait.proposition}</h3>
                      <p className="subtle">
                        {domains[trait.domain] || trait.domain}
                      </p>
                      <button
                        className="text-button"
                        onClick={() => {
                          setFocusEvidence([
                            ...trait.supports,
                            ...trait.counters,
                          ]);
                          document
                            .getElementById("evidence-library")
                            ?.scrollIntoView({ behavior: "instant" });
                        }}
                      >
                        支持 {trait.supports.length} 条 / 反例{" "}
                        {trait.counters.length} 条<Icon name="down" size={14} />
                      </button>
                    </article>
                  ))}
                </div>
              )}
              <p className="track-footnote">
                {track.key === "PERCEIVED"
                  ? "“我认为自己怎样”是你的陈述，保留情境与时间。"
                  : "行为自报仍是线索，系统解释始终可以被质疑。"}
              </p>
              <Button
                secondary
                disabled={busy || !traits.length}
                onClick={() =>
                  void act("人格版本已保存", () =>
                    post(`/personas/${track.key}/compile`),
                  )
                }
              >
                保存当前版本
              </Button>
            </section>
          );
        })}
        <section className="mirror-track track-chosen">
          <header>
            <span className="track-caption">由你主动选择</span>
            <h2>我选择成为的我</h2>
          </header>
          {data.directives.filter(
            (d) => approved(d) && (filter === "all" || d.domain === filter),
          ).length ? (
            <div className="trait-list">
              {data.directives
                .filter(
                  (d) =>
                    approved(d) && (filter === "all" || d.domain === filter),
                )
                .map((d) => (
                  <article key={d.id} className="trait">
                    <span className="trait-status">本人已确认的方向</span>
                    <h3>{d.objective}</h3>
                    <p className="subtle">{domains[d.domain] || d.domain}</p>
                    {d.limits && <p>保留的边界：{d.limits}</p>}
                  </article>
                ))}
            </div>
          ) : (
            <div className="track-empty">
              <span aria-hidden="true">〈 〉</span>
              <p>希望如何回应生活，只有你可以决定。</p>
            </div>
          )}
          <p className="track-footnote">
            希望成为的样子，不会被写成过去已经做到。
          </p>
          <Button secondary onClick={() => navigate("chosen")}>
            选择我的方向
          </Button>
        </section>
      </div>
      <section id="evidence-library" className="evidence-library">
        <div className="section-heading">
          <div>
            <h2>回到真实来源</h2>
            <p>你批准保留的原话与现实行动自报。</p>
          </div>
          {focusEvidence.length > 0 && (
            <button
              className="text-button"
              onClick={() => setFocusEvidence([])}
            >
              查看全部证据
            </button>
          )}
        </div>
        {!evidence.length ? (
          <Empty
            title="还没有纳入真实证据"
            action={
              <Button secondary onClick={() => navigate("chat")}>
                开始真聊
              </Button>
            }
          >
            在真聊中，主动选择“纳入记忆”。模拟对话不会出现在这里。
          </Empty>
        ) : (
          <div className="evidence-grid">
            {evidence
              .filter(
                (e) => !focusEvidence.length || focusEvidence.includes(e.id),
              )
              .map((e) => (
                <EvidenceCard
                  key={e.id}
                  evidence={e}
                  busy={busy}
                  confirm={confirm}
                />
              ))}
          </div>
        )}
      </section>
      <section className="version-history">
        <h2>留下的版本</h2>
        {data.versions.length ? (
          <ul>
            {data.versions.map((v) => (
              <li key={v.id}>
                <span>
                  {v.track === "CHOSEN"
                    ? "理想的我"
                    : v.track === "OBSERVED"
                      ? "行为线索"
                      : "自我陈述"}
                </span>
                <time>{date(v.created_at)}</time>
                <span
                  className={`version-state ${activeVersion(v.state) ? "" : "invalid"}`}
                >
                  {label(v.state)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="subtle">
            当你保存一个人格版本，它的来源和状态会留在这里。
          </p>
        )}
      </section>
    </>
  );
}

function Chosen({ data, busy, act, confirm, navigate }: Shared) {
  const [objective, setObjective] = useState("");
  const [domain, setDomain] = useState("general");
  const [limits, setLimits] = useState("");
  return (
    <div className="two-column-page">
      <section className="form-paper">
        <h2>给未来的自己，一点方向。</h2>
        <p className="section-copy">
          不需要成为另一种“标准答案”。写下一个你认可的选择方式，以及你不想放弃的边界。
        </p>
        <form
          className="stack-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (
              await act("目标提案已保存，等待你再次确认", () =>
                post("/personas/chosen/proposals", {
                  objective,
                  domain,
                  limits,
                }),
              )
            ) {
              setObjective("");
              setLimits("");
            }
          }}
        >
          <label className="field">
            我希望……
            <textarea
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              required
              minLength={2}
              maxLength={2000}
              rows={4}
              placeholder="例如：面对重要选择，减少没有新增信息的反复犹豫。"
            />
          </label>
          <DomainSelect
            value={domain}
            onChange={setDomain}
            id="chosen-domain"
          />
          <label className="field">
            同时，我想保留的边界
            <textarea
              value={limits}
              onChange={(e) => setLimits(e.target.value)}
              maxLength={2000}
              rows={3}
              placeholder="例如：仍然核查关键风险，不为了果断而仓促决定。"
            />
          </label>
          <p className="form-note">
            先保存提案，再由你明确确认。AI 不会替你批准目标。
          </p>
          <Button disabled={busy || !objective.trim()}>保存目标提案</Button>
        </form>
      </section>
      <section className="chosen-list">
        <div className="section-heading">
          <div>
            <h2>我选择的方向</h2>
            <p>可以确认，也可以随时撤回。</p>
          </div>
        </div>
        {!data.directives.length ? (
          <Empty title="方向由你来写">
            你没有必须弥补的分数。一个具体、愿意尝试的变化就足够。
          </Empty>
        ) : (
          data.directives.map((d) => (
            <article
              className={`directive ${approved(d) ? "directive-approved" : ""}`}
              key={d.id}
            >
              <header>
                <span>{label(d.state)}</span>
                <span>{domains[d.domain] || d.domain}</span>
              </header>
              <h3>{d.objective}</h3>
              {d.limits && <p>我的边界：{d.limits}</p>}
              <div className="directive-actions">
                {d.state === "PROPOSED" && (
                  <Button
                    disabled={busy}
                    onClick={() =>
                      confirm({
                        title: "确认这是我选择的方向",
                        body: `“${d.objective}”将作为你明确选择的未来目标。这不会改写你过去的行为，也不代表你已经做到。`,
                        button: "这是我的选择",
                        work: () =>
                          post(
                            `/personas/chosen/proposals/${encodeURIComponent(d.id)}/approve`,
                            { revision: d.revision },
                          ),
                      })
                    }
                  >
                    我确认这个方向
                  </Button>
                )}
                {approved(d) && (
                  <>
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() =>
                        confirm({
                          title: "撤回这个方向",
                          body: "依赖该目标的理想人格版本会停用。你不需要说明原因，也可以之后重新选择。",
                          button: "撤回方向",
                          work: () =>
                            post(
                              `/personas/chosen/proposals/${encodeURIComponent(d.id)}/revoke`,
                              { revision: d.revision },
                            ),
                        })
                      }
                    >
                      撤回
                    </button>
                  </>
                )}
              </div>
            </article>
          ))
        )}
        {data.directives.some(approved) && (
          <div className="compile-chosen">
            <p>把你已确认的方向保存为一个固定版本，用于接下来的模拟。</p>
            <Button
              disabled={busy}
              onClick={() =>
                void act("理想人格版本已保存", () =>
                  post("/personas/CHOSEN/compile"),
                )
              }
            >
              保存理想人格版本
            </Button>
            <button
              className="text-button"
              onClick={() => navigate("simulation")}
            >
              前往模拟空间
              <Icon name="arrow" size={16} />
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

function Simulations({ data, busy, act, confirm, navigate }: Shared) {
  const [selected, setSelected] = useState(data.simulations[0]?.id || "");
  const [topic, setTopic] = useState("");
  const [version, setVersion] = useState(
    data.versions.find((v) => activeVersion(v.state))?.id || "",
  );
  const [skill, setSkill] = useState(data.skills[0]?.id || "");
  const [turnCap, setTurnCap] = useState(6);
  const [allowPrivate, setAllowPrivate] = useState(false);
  const [feedbackTurn, setFeedbackTurn] = useState("");
  const [feedbackText, setFeedbackText] = useState("");
  const nextKeys = useRef<Record<string, { index: number; key: string }>>({});
  const versions = data.versions.filter(
    (v) => activeVersion(v.state) && ["OBSERVED", "CHOSEN"].includes(v.track),
  );
  const session = data.simulations.find((s) => s.id === selected);
  useEffect(() => {
    if (!versions.some((v) => v.id === version))
      setVersion(versions[0]?.id || "");
  }, [data.versions, version]);
  return (
    <>
      <div className="simulation-notice">
        <Icon name="simulation" size={23} />
        <p>
          <strong>这是一场 AI 模拟。</strong>
          {data.privacy.provider === "mock"
            ? "当前使用离线演示固定模板，展示独立角色的交替流程，并非实际人格推理。"
            : "分身与人物分别生成发言，均不代表真人正在对话。"}
          分身发言先由你检查，只有你明确允许，另一角色才会阅读并回应。这里的剧情不会进入你的真实记忆。
        </p>
      </div>
      <div className="studio-layout">
        <aside className="studio-setup">
          <h2>开启另一种可能</h2>
          {!versions.length ? (
            <div className="setup-empty">
              <p>
                先在“看见自己”保存行为画像版本，或在“理想的我”确认目标并保存版本。
              </p>
              <Button secondary onClick={() => navigate("chosen")}>
                选择我的方向
              </Button>
            </div>
          ) : (
            <form
              className="stack-form"
              onSubmit={async (e) => {
                e.preventDefault();
                await act("模拟空间已创建", async () => {
                  const result = await post<Simulation>("/simulations", {
                    topic,
                    persona_version_id: version,
                    skill_id: skill,
                    max_turns: turnCap,
                    allow_private_context: allowPrivate,
                  });
                  setSelected(result.id);
                });
              }}
            >
              <label className="field">
                这次想观察什么？
                <textarea
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  rows={3}
                  required
                  maxLength={2000}
                  placeholder="例如：面对一个不确定的工作机会，我会怎样讨论？"
                />
              </label>
              <label className="field">
                哪一个我
                <select
                  aria-label="哪一个我"
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                >
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.track === "CHOSEN" ? "理想的我" : "行为线索中的我"} ·{" "}
                      {date(v.created_at)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                与谁交谈
                <select
                  aria-label="与谁交谈"
                  value={skill}
                  onChange={(e) => setSkill(e.target.value)}
                >
                  {data.skills.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.display_name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                最多发言条数
                <input
                  type="number"
                  min={2}
                  max={20}
                  value={turnCap}
                  onChange={(e) => setTurnCap(Number(e.target.value))}
                  required
                />
              </label>
              <label className="checkbox-label private-grant">
                <input
                  type="checkbox"
                  checked={allowPrivate}
                  onChange={(e) => setAllowPrivate(e.target.checked)}
                />
                允许我的分身按主题使用最多三项私人证据。
              </label>
              <p className="form-note">
                外部人物只能看公开模拟发言。理想目标会作为分身的输入；若连接外部模型，这些经授权字段会发送给模型供应商。
              </p>
              <p className="form-note">
                每次只生成一条。你可以随时暂停，版本在本次模拟中保持固定。
              </p>
              <Button disabled={busy || !version || !topic.trim()}>
                创建实验预览
              </Button>
            </form>
          )}
          {data.simulations.length > 0 && (
            <div className="past-simulations">
              <h3>以前的可能</h3>
              {data.simulations.map((s) => (
                <button
                  key={s.id}
                  className={selected === s.id ? "selected" : ""}
                  onClick={() => setSelected(s.id)}
                >
                  <span>{s.topic}</span>
                  <small>
                    {label(s.state)} / {s.turns.length} 条发言
                  </small>
                </button>
              ))}
            </div>
          )}
        </aside>
        <section className="simulation-stage" aria-label="AI 模拟对话时间线">
          {!session ? (
            <div className="stage-empty">
              <div className="stage-avatars" aria-hidden="true">
                <span>我</span>
                <i />
                <span>他</span>
              </div>
              <h2>坐下来，看看另一个我。</h2>
              <p>
                选一个真实关心的话题，
                <br />
                让两个视角展开一场有边界的交流。
              </p>
              <span className="outline-badge">所有发言均为 AI 模拟</span>
            </div>
          ) : (
            <>
              <div className="stage-heading">
                <div>
                  <span className="outline-badge">AI 模拟 · 实验预览</span>
                  <h2>{session.topic}</h2>
                </div>
                <span className="session-state">
                  {label(session.state)}
                  <small>
                    {session.turns.length} / {session.max_turns} 条
                  </small>
                </span>
              </div>
              <div className="session-usage"><span>{data.privacy.provider === "mock" ? "离线演示用量" : "模型用量"}：{session.used_tokens ?? 0} / {session.max_tokens ?? 6000}</span><span>{data.privacy.provider === "mock" ? "本场费用 0 元" : "达到用量上限后停止"}</span></div>
              {session.failure && <p className="session-failure">{session.failure}</p>}
              <div className="simulation-transcript">
                {!session.turns.length && (
                  <p className="stage-invitation">
                    舞台已准备好。点击“生成下一条”开始。
                  </p>
                )}
                {session.turns.map((turn) => {
                  const isPersona =
                    [
                      "PERSONA",
                      "SELF",
                      "persona",
                      "user_persona",
                      "PERSONA_AGENT",
                    ].includes(turn.speaker) || turn.turn_index % 2 === 0;
                  return (
                    <article
                      key={turn.id}
                      className={`simulation-turn ${isPersona ? "persona-turn" : "external-turn"}`}
                    >
                      <header>
                        <span className="turn-avatar">
                          {isPersona ? "我" : "问"}
                        </span>
                        <strong>{isPersona ? "我的分身" : "人物角色"}</strong>
                        <span className="message-badge ai-badge">AI 模拟</span>
                      </header>
                      <p>{turn.text}</p>
                      <div className="turn-feedback">
                        <span>{isPersona ? "这一句，像你吗？" : "这句提问，你怎么看？"}</span>
                        {[
                          ["LIKE_ME", isPersona ? "像我" : "有共鸣"],
                          ["NOT_LIKE_ME", isPersona ? "不像我" : "有分歧"],
                          ["UNCERTAIN", "不确定"],
                          ...(isPersona ? [["WANT_TO_BE", "我想成为这样"]] : []),
                        ].map(([verdict, text]) => (
                          <button
                            key={verdict}
                            disabled={busy}
                            className={
                              session.feedback?.some(
                                (f) =>
                                  f.turn_id === turn.id &&
                                  f.verdict === verdict,
                              )
                                ? "chosen-feedback"
                                : ""
                            }
                            onClick={() =>
                              void act(verdict === "WANT_TO_BE" ? "已记录目标意向；请到“理想的我”另行提出并确认目标" : "反馈已保存，仅用于模拟审阅", () =>
                                post(
                                  `/simulation-turns/${encodeURIComponent(turn.id)}/feedback`,
                                  {
                                    verdict,
                                    comment: "",
                                    intent_scope: verdict === "WANT_TO_BE" ? "CHOSEN_GOAL_AMENDMENT" : isPersona ? "SIM_STYLE_CORRECTION" : "SIM_VALUE_DISAGREEMENT",
                                  },
                                ),
                              )
                            }
                          >
                            {text}
                          </button>
                        ))}
                        <button
                          onClick={() => {
                            setFeedbackTurn(
                              feedbackTurn === turn.id ? "" : turn.id,
                            );
                            setFeedbackText("");
                          }}
                          aria-expanded={feedbackTurn === turn.id}
                        >
                          补充说明
                        </button>
                      </div>
                      {feedbackTurn === turn.id && (
                        <form
                          className="feedback-form"
                          onSubmit={async (e) => {
                            e.preventDefault();
                            if (
                              await act(
                                "反馈已保存，不会自动改写真实资料",
                                () =>
                                  post(
                                    `/simulation-turns/${encodeURIComponent(turn.id)}/feedback`,
                                    {
                                      verdict: "NOT_LIKE_ME",
                                      comment: feedbackText,
                                      intent_scope: "SIM_STYLE_CORRECTION",
                                    },
                                  ),
                              )
                            )
                              setFeedbackTurn("");
                          }}
                        >
                          <label className="field">
                            哪里与你不同？
                            <textarea
                              rows={2}
                              value={feedbackText}
                              onChange={(e) => setFeedbackText(e.target.value)}
                              required
                              maxLength={2000}
                            />
                          </label>
                          <Button secondary disabled={busy}>
                            保存模拟反馈
                          </Button>
                        </form>
                      )}
                    </article>
                  );
                })}
              </div>
              <div className="stage-controls">
                {["CREATED", "RUNNING", "ACTIVE", "GENERATING"].includes(session.state) && (
                  <>
                    <Button
                      disabled={busy || session.state === "GENERATING" || session.turns.length >= session.max_turns}
                      onClick={() => void act("模拟状态已更新", () => {
                        if (nextKeys.current[session.id]?.index !== session.turns.length) {
                          nextKeys.current[session.id] = { index: session.turns.length, key: crypto.randomUUID() };
                        }
                        return post(
                          `/simulations/${encodeURIComponent(session.id)}/next`,
                          session.turns.length % 2 === 1 ? { disclosure_approved: true } : {},
                          nextKeys.current[session.id].key,
                        );
                      })}
                    >
                      <Icon name="play" size={16} />
                      {session.turns.length % 2 === 1 ? "允许人物阅读上一句并回应" : "生成下一条"}
                    </Button>
                    <Button
                      secondary
                      onClick={() =>
                        void act("模拟已暂停", () =>
                          post(
                            `/simulations/${encodeURIComponent(session.id)}/pause`,
                          ),
                          true,
                        )
                      }
                    >
                      <Icon name="pause" size={16} />
                      暂停
                    </Button>
                  </>
                )}
                {["PAUSED", "FAILED"].includes(session.state) && (
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void act("模拟已恢复，可逐条继续", () =>
                        post(
                          `/simulations/${encodeURIComponent(session.id)}/resume`,
                        ),
                      )
                    }
                  >
                    恢复模拟
                  </Button>
                )}
                {!["COMPLETED", "CANCELLED", "INVALIDATED"].includes(
                  session.state,
                ) && (
                  <button
                    className="text-button quiet-danger"
                    onClick={() =>
                      void act("模拟已停止，已生成的内容仍可回放", () =>
                          post(
                            `/simulations/${encodeURIComponent(session.id)}/cancel`,
                          ),
                          true,
                      )
                    }
                  >
                    停止
                  </button>
                )}
                <span className="controls-note">不自动连聊</span>
              </div>
              <p className="stage-footnote">
                对话版本已冻结。若想修改目标，请回到“理想的我”，确认新版本后开启另一场模拟。
              </p>
            </>
          )}
        </section>
      </div>
    </>
  );
}

function GrowthView({ data, busy, act, confirm, navigate }: Shared) {
  const approvedDirectives = data.directives.filter(approved);
  const [directive, setDirective] = useState(approvedDirectives[0]?.id || "");
  const [title, setTitle] = useState("");
  const [trigger, setTrigger] = useState("");
  const [action, setAction] = useState("");
  const [consent, setConsent] = useState(false);
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  return (
    <div className="two-column-page">
      <section className="form-paper">
        <h2>把一种可能，带回日常。</h2>
        <p className="section-copy">
          不必打卡，也不必证明自己。只为你已确认的方向，安排一次可以退出的小尝试。
        </p>
        {!approvedDirectives.length ? (
          <Empty
            title="先选择一个自己的方向"
            action={
              <Button secondary onClick={() => navigate("chosen")}>
                前往理想的我
              </Button>
            }
          >
            只有你已经确认的目标，才能成为现实行动的依据。
          </Empty>
        ) : (
          <form
            className="stack-form"
            onSubmit={async (e) => {
              e.preventDefault();
              if (
                await act("自愿行动已保存", () =>
                  post("/growth/goals", {
                    directive_id: directive,
                    title,
                    trigger,
                    action,
                    user_approved: consent,
                  }),
                )
              ) {
                setTitle("");
                setTrigger("");
                setAction("");
                setConsent(false);
              }
            }}
          >
            <label className="field">
              对应的方向
              <select
                aria-label="对应的方向"
                value={directive}
                onChange={(e) => setDirective(e.target.value)}
                required
              >
                {approvedDirectives.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.objective}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              给这次尝试起个名字
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={200}
                required
                placeholder="例如：给选择留一个截止时间"
              />
            </label>
            <label className="field">
              如果我遇到……
              <textarea
                value={trigger}
                onChange={(e) => setTrigger(e.target.value)}
                rows={2}
                maxLength={2000}
                required
                placeholder="一个具体、可辨认的情境。"
              />
            </label>
            <label className="field">
              我愿意试着……
              <textarea
                value={action}
                onChange={(e) => setAction(e.target.value)}
                rows={3}
                maxLength={2000}
                required
                placeholder="一个足够小、可以随时停止的动作。"
              />
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                required
              />
              这是我自愿选择的尝试，我可以随时撤回。
            </label>
            <Button disabled={busy || !consent}>确认这次小尝试</Button>
          </form>
        )}
      </section>
      <section className="growth-list">
        <div className="section-heading">
          <div>
            <h2>正在生活里发生</h2>
            <p>做了、没做、改变主意，都可以如实留下。</p>
          </div>
        </div>
        {!data.growth.length ? (
          <Empty title="留一个空白给现实">
            从一个很小的行动开始。这里没有连续天数，也没有完成排行榜。
          </Empty>
        ) : (
          data.growth.map((item) => (
            <article className="growth-entry" key={item.id}>
              <header>
                <span
                  className={`growth-state ${item.state === "REVOKED" ? "muted" : ""}`}
                >
                  {label(item.state)}
                </span>
              </header>
              <h3>{item.title}</h3>
              <dl>
                <dt>如果</dt>
                <dd>{item.trigger}</dd>
                <dt>那么</dt>
                <dd>{item.action}</dd>
              </dl>
              {item.outcome && (
                <div className="self-report">
                  <span>我对实际结果的自报</span>
                  <p>{item.outcome}</p>
                </div>
              )}
              {!["REVOKED", "CANCELLED"].includes(item.state) && (
                <>
                  <form
                    className="stack-form checkin-form"
                    onSubmit={async (e) => {
                      e.preventDefault();
                      if (
                        await act("现实结果已作为本人自报保存", () =>
                          post(
                            `/growth/actions/${encodeURIComponent(item.id)}/checkins`,
                            { outcome: outcomes[item.id] || "" },
                          ),
                        )
                      )
                        setOutcomes({ ...outcomes, [item.id]: "" });
                    }}
                  >
                    <label className="field">
                      这次实际上发生了什么？
                      <textarea
                        rows={2}
                        value={outcomes[item.id] || ""}
                        onChange={(e) =>
                          setOutcomes({
                            ...outcomes,
                            [item.id]: e.target.value,
                          })
                        }
                        maxLength={4000}
                        required
                        placeholder="也可以写下没做、退出或新的想法。"
                      />
                    </label>
                    <Button
                      secondary
                      disabled={busy || !outcomes[item.id]?.trim()}
                    >
                      记录现实结果
                    </Button>
                  </form>
                  <button
                    className="text-button quiet-danger"
                    disabled={busy}
                    onClick={() =>
                      confirm({
                        title: "撤回这次尝试",
                        body: "这项行动将停止。撤回不是失败，你不需要解释原因。",
                        button: "撤回行动",
                        work: () =>
                          post(
                            `/growth/actions/${encodeURIComponent(item.id)}/revoke`,
                          ),
                      })
                    }
                  >
                    我想撤回这次尝试
                  </button>
                </>
              )}
            </article>
          ))
        )}
      </section>
    </div>
  );
}

function Privacy({ data, busy, act, confirm, navigate }: Shared) {
  async function exportData() {
    const result = await post<unknown>("/data/export");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(result, null, 2)], {
        type: "application/json;charset=utf-8",
      }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `我们曾聊过-个人数据-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="privacy-page">
      <section className="privacy-opening">
        <Icon name="privacy" size={36} />
        <h2>这些话，仍然属于你。</h2>
        <p>你决定什么成为记忆，什么只留在当下，什么可以被删除。</p>
      </section>
      <div className="privacy-sections">
        <section>
          <div className="section-heading">
            <h2>模型与外部发送</h2>
            <span className="real-badge message-badge">
              {data.privacy.provider === "mock"
                ? "当前为演示模型"
                : "已连接外部模型"}
            </span>
          </div>
          <p>
            {data.privacy.provider === "mock"
              ? "当前使用本地确定性演示回复，不调用外部模型。它用于体验流程和验证数据边界，不能证明人格模拟效果。"
              : "模型推理会把经授权的必要字段发送给你选择的供应商。本地保存并不等于推理不出本机。"}
          </p>
          <dl className="privacy-facts">
            <div>
              <dt>实际外部模型调用</dt>
              <dd>{data.privacy.external_calls} 次</dd>
            </div>
            <div>
              <dt>遥测</dt>
              <dd>{data.privacy.telemetry ? "已开启" : "默认关闭"}</dd>
            </div>
          </dl>
        </section>
        <section>
          <h2>真实与模拟的边界</h2>
          <ul className="privacy-boundaries">
            <li>
              <strong>真实证据</strong>
              <p>仅保存你主动纳入的真人发言、更正和现实行动自报。</p>
            </li>
            <li>
              <strong>我选择的目标</strong>
              <p>需要你明确确认，且不会被写成过去已经发生的事实。</p>
            </li>
            <li>
              <strong>模拟对话与反馈</strong>
              <p>独立保存，不会自动进入真实记忆。反馈先留作审阅。</p>
            </li>
            <li>
              <strong>人格版本</strong>
              <p>保留来源关系。删除底层证据后，相关版本立即失效。</p>
            </li>
          </ul>
        </section>
        <section>
          <h2>查看、导出与删除</h2>
          <p>
            导出文件可能包含你的私人记录，请选择你信任的位置保存。删除具体证据时，系统会一并停用依赖它的版本。
          </p>
          <div className="privacy-actions">
            <Button
              secondary
              disabled={busy}
              onClick={() =>
                confirm({
                  title: "导出我的个人数据",
                  body: `将导出本地对话、${data.evidence.length} 条证据、你选择的方向和带有 AI 模拟标记的虚拟对话。导出文件包含私人内容，请妥善保存。`,
                  button: "下载个人数据",
                  work: exportData,
                })
              }
            >
              导出个人数据
            </Button>
            <button className="text-button" onClick={() => navigate("mirror")}>
              管理与删除真实证据
              <Icon name="arrow" size={16} />
            </button>
          </div>
        </section>
        <section className="research-note">
          <h2>关于这个预览</h2>
          <p>
            这是《我们曾聊过》的本地研究预览。当前的自我镜像是可追溯、可反驳的整理线索，尚未经过独立人格忠实度验证。没有“人格准确率”，也没有已被证明的成长收益。
          </p>
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              void act("已重新读取本地记录", () => request("/state"))
            }
          >
            重新读取本地记录
          </button>
        </section>
      </div>
    </div>
  );
}
