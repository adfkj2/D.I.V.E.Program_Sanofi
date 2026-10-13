"use strict";
const $ = (id) => document.getElementById(id);
let project =
    new URL(location.href).searchParams.get("project") || "my-research",
  state = null,
  brief = null,
  since = null,
  lastConsult = null,
  contextVersion = 0,
  refreshVersion = 0,
  answerVersion = 0,
  lastTrace = null,
  accessToken = "",
  cutoff = null,
  recordRequestId = null,
  recordObservedAt = null,
  recordDateDisplay = "";
const category = {
  constraint: "约束",
  decision: "决定",
  result: "实验结果",
  preference: "偏好",
  note: "事实",
};
const disposition = {
  confirmed: "已确认",
  proposed: "仅提议",
  rejected: "未采用",
};
const statusNames = {
  ACTIVE: "当前版本",
  REINFORCED: "已再次确认",
  SUPERSEDED: "历史版本",
  ARCHIVED: "已归档",
};
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function showMessage(text, error = false) {
  $("message").textContent = text;
  $("message").className = error ? "error" : "";
  $("message").hidden = false;
}
async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(accessToken ? { Authorization: "Bearer " + accessToken } : {}),
  };
  const response = await fetch(path, { ...options, headers });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
function params(extra = {}) {
  return new URLSearchParams({ project, ...extra }).toString();
}
function date(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "未记录时间";
}
function localDate(value = new Date()) {
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function recordDate(memory) {
  const value = memory.valid_from || memory.observed_at;
  if (
    memory.structured_content.source_sha256 &&
    memory.structured_content.session?.startsWith("repo-casebook-")
  ) {
    return (
      new Date(value).toLocaleDateString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "short",
        day: "numeric",
      }) + " · 日期精度"
    );
  }
  return date(value);
}
function switchTab(name) {
  document.querySelectorAll(".page").forEach((p) => (p.hidden = p.id !== name));
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.tab === name);
    b.setAttribute("aria-current", b.dataset.tab === name ? "page" : "false");
  });
}
async function refresh() {
  const version = ++refreshVersion;
  const context = contextVersion;
  const query = params({
    ...(cutoff ? { as_of: cutoff } : {}),
    ...(since ? { since } : {}),
  });
  const [ready, snapshot, resume] = await Promise.all([
    api("/api/readiness"),
    api("/api/snapshot?" + params(cutoff ? { as_of: cutoff } : {})),
    api("/api/brief?" + query),
  ]);
  if (version !== refreshVersion || context !== contextVersion) return;
  state = snapshot;
  brief = resume;
  $("project").value = project;
  const url = new URL(location.href);
  url.searchParams.set("project", project);
  history.replaceState(null, "", url);
  $("project-label").textContent = project;
  $("mode").textContent =
    ready.mode === "live_model" ? "在线模型已配置" : "本地记录核查";
  $("reader-description").textContent =
    ready.mode === "live_model"
      ? "回答器仅接收本次选中的证据；引用和失败状态可以检查。"
      : "当前未配置回答模型。检索照常运行，只展示真实证据，不生成模型回答。";
  renderBrief();
  renderResume();
  renderShortcuts();
  renderTimeline();
  setAnswerMode();
}
function invalidate() {
  contextVersion += 1;
  brief = null;
  lastConsult = null;
  lastTrace = null;
  $("trace-result").hidden = true;
  $("trace-empty").hidden = false;
  $("lookup-result").replaceChildren();
  $("consult-result").hidden = true;
  $("resume-panel").hidden = true;
  $("copy-handoff").disabled = true;
  $("download-handoff").disabled = true;
}
function action(label, handler) {
  const b = node("button", label);
  b.type = "button";
  b.addEventListener("click", () => run(handler));
  return b;
}
function recordCard(memory, history = false) {
  const d = memory.structured_content;
  const card = node("article", undefined, "record");
  const head = node("div", undefined, "record-head");
  head.append(node("h3", d.key || memory.kind));
  head.append(
    node(
      "span",
      history
        ? statusNames[memory.status] || memory.status
        : disposition[d.disposition] || "文本提取",
      "tag " + (d.disposition || ""),
    ),
  );
  card.append(head);
  card.append(node("p", d.value || memory.content, "record-value"));
  if (d.reason) card.append(node("p", d.reason, "record-reason"));
  const footer = node("div", undefined, "record-footer");
  footer.append(
    node(
      "time",
      (category[d.category] || memory.evidence_state) +
        " · " +
        recordDate(memory),
    ),
  );
  const actions = node("div", undefined, "record-actions");
  actions.append(action("查看来源 ↗", () => showSource(memory.id)));
  if (
    d.schema === "project-record-v1" &&
    ["ACTIVE", "REINFORCED"].includes(memory.status)
  )
    actions.append(action("更新", () => openRecord(memory)));
  actions.append(action("遗忘", () => openForget(memory)));
  footer.append(actions);
  card.append(footer);
  return card;
}
function renderBrief() {
  const confirmed = state.memories.filter(
    (m) =>
      !state.conflicts.includes(m.structured_content.key) &&
      (!m.structured_content.disposition ||
        m.structured_content.disposition === "confirmed"),
  );
  const pending = state.memories.filter((m) =>
    ["proposed", "rejected"].includes(m.structured_content.disposition),
  );
  $("confirmed").replaceChildren(...confirmed.map((m) => recordCard(m)));
  $("pending").replaceChildren(...pending.map((m) => recordCard(m)));
  if (!pending.length)
    $("pending").append(node("p", "暂无待确认事项", "form-note"));
  $("confirmed-count").textContent = confirmed.length + " 条记录";
  $("empty").hidden = state.memories.length > 0;
  $("conflicts").hidden = !state.conflicts.length;
  $("conflicts").textContent =
    "存在尚未解决的冲突：" +
    state.conflicts.join("、") +
    "。系统不会替你选定其中一个值。";
  $("time-label").textContent = cutoff
    ? "历史时点 · " + date(cutoff)
    : "当前有效状态";
}

function briefItem(memory) {
  const data = memory.structured_content;
  const item = node("article", undefined, "resume-item");
  item.append(node("h3", data.key), node("p", data.value));
  if (data.reason) item.append(node("small", data.reason));
  item.append(action("核对来源 ↗", () => showSource(memory.id)));
  return item;
}

function renderResume() {
  const hasRecords =
    brief.sections.some((section) => section.items.length) ||
    brief.conflicts.length;
  $("resume-panel").hidden = !hasRecords;
  $("copy-handoff").disabled = !hasRecords;
  $("download-handoff").disabled = !hasRecords;
  $("resume-summary").textContent = brief.summary;
  const sections = $("resume-sections");
  sections.replaceChildren();
  for (const section of brief.sections.filter((part) => part.items.length)) {
    const card = node("section", undefined, "resume-card " + section.id);
    card.append(node("h2", section.title));
    card.append(...section.items.slice(0, 2).map(briefItem));
    if (section.items.length > 2) {
      const rest = node("details", undefined, "resume-more");
      rest.append(
        node("summary", "展开其余 " + (section.items.length - 2) + " 条"),
      );
      rest.append(...section.items.slice(2).map(briefItem));
      card.append(rest);
    }
    sections.append(card);
  }
  const conflicts = $("resume-conflicts");
  conflicts.replaceChildren();
  conflicts.hidden = !brief.conflicts.length;
  if (brief.conflicts.length) {
    conflicts.append(
      node("h2", "先核对这些冲突"),
      node("p", "以下记录同时有效，简报不会替你选择其中一个值。"),
    );
    for (const group of brief.conflicts) {
      const card = node("section", undefined, "resume-card conflict");
      card.append(node("h3", group.key), ...group.items.map(briefItem));
      conflicts.append(card);
    }
  }
  const changes = $("recent-changes");
  changes.replaceChildren();
  if (!since)
    changes.append(
      node(
        "p",
        "选择上次工作时间，核对之后新增和明确替换的记录。",
        "form-note",
      ),
    );
  else if (!brief.changes.length)
    changes.append(
      node(
        "p",
        "该区间没有新增或替换记录。已删除的来源不会在这里恢复。",
        "form-note",
      ),
    );
  else
    for (const change of brief.changes) {
      const item = node("article", undefined, "change-item");
      const after = change.after.structured_content;
      item.append(
        node(
          "span",
          change.kind === "updated" ? "明确更新" : "新增记录",
          "tag",
        ),
      );
      item.append(node("h3", after.key));
      if (change.before)
        item.append(
          node(
            "p",
            "此前 · " + change.before.structured_content.value,
            "change-before",
          ),
        );
      item.append(node("p", "此后 · " + after.value, "change-after"));
      item.append(action("查看这次记录 ↗", () => showSource(change.after.id)));
      changes.append(item);
    }
  const curated = state.timeline.some((m) =>
    m.structured_content.session?.startsWith("repo-casebook-"),
  );
  $("casebook-banner").hidden = !curated;
  if (curated)
    $("casebook-banner").querySelector("p").textContent =
      "仓库记录人工整理的选摘案例，截至 2026-10-12。日期按北京时间日末归一化，不代表实际时分秒；未录入的信息不在此简报中。原文可核查，用户价值尚待验证。";
}

function renderShortcuts() {
  const keys = state.memories
    .filter((m) => m.structured_content.schema === "project-record-v1")
    .map((m) => m.structured_content.key);
  const questions = [
    "继续项目",
    "当前约束",
    "哪些方案未采用",
    ...[...new Set(keys)].slice(0, 4),
  ];
  $("question-shortcuts").replaceChildren(
    ...questions.map((question) => {
      const button = action(question, async () => {
        $("question").value = question;
        $("answer-mode").value = "local";
        setAnswerMode();
        $("ask-form").requestSubmit(
          $("ask-form").querySelector('button[type="submit"]'),
        );
      });
      button.className = "question-chip";
      return button;
    }),
  );
}

function setAnswerMode() {
  answerVersion += 1;
  const local = $("answer-mode").value === "local";
  $("ask-form").querySelector('button[type="submit"]').textContent = local
    ? "核查记录 →"
    : "查找证据 →";
  $("reader-description").textContent = local
    ? "核查当前状态、约束、未采用方案，或输入一条记录名称。结果直接来自明确记录，不调用模型。"
    : "检索与模型模式：无密钥时只展示证据；引用可核查不代表结论已被自动证实。";
  $("consult-result").hidden = true;
  $("trace-result").hidden = true;
  $("trace-empty").hidden = false;
}

function renderConsult() {
  $("trace-empty").hidden = true;
  $("trace-result").hidden = true;
  const box = $("consult-result");
  box.hidden = false;
  box.replaceChildren();
  box.className = "consult-card " + lastConsult.status.toLowerCase();
  box.append(node("span", "本地记录核查 · 无模型调用", "small-title"));
  const labels = {
    ANSWER: "依据已整理",
    ABSTAIN: "暂时不能确定",
    UNSUPPORTED: "这个问题需要进一步查证",
  };
  box.append(node("h2", labels[lastConsult.status] || lastConsult.status));
  box.append(
    node("p", lastConsult.answer || lastConsult.reason, "consult-answer"),
  );
  const citations = node("div", undefined, "consult-citations");
  for (const cite of lastConsult.citations || [])
    citations.append(
      action("核对 “" + cite.quote + "” ↗", () => showSource(cite.memory_id)),
    );
  box.append(citations);
  if (lastConsult.status === "UNSUPPORTED")
    box.append(
      node(
        "p",
        "可以选择上面的项目任务，或输入已记录的名称；也可切换检索与模型模式查找相关片段。",
        "form-note",
      ),
    );
}

function downloadText(text, filename, type) {
  const link = document.createElement("a");
  const url = URL.createObjectURL(new Blob([text], { type }));
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderTimeline() {
  const rows = [...state.timeline].sort((a, b) =>
    (b.valid_from || "").localeCompare(a.valid_from || ""),
  );
  $("timeline-list").replaceChildren(
    ...rows.map((m) => {
      const item = node("div", undefined, "timeline-item");
      item.append(node("time", recordDate(m)));
      const body = node("div", undefined, "timeline-body");
      body.append(recordCard(m, true));
      if (m.supersedes_id)
        body.append(node("p", "此版本替换了此前的明确记录。", "form-note"));
      item.append(body);
      return item;
    }),
  );
  if (!rows.length)
    $("timeline-list").append(
      node("p", "记录一条信息后，版本历史会出现在这里。", "form-note"),
    );
}
async function showSource(id) {
  const context = contextVersion;
  const result = await api(
    "/api/sources/" + encodeURIComponent(id) + "?" + params(),
  );
  if (context !== contextVersion) return;
  const box = $("source-content");
  box.replaceChildren();
  const source = result.memory.structured_content;
  if (source.schema === "project-record-v1") {
    box.append(node("h3", source.key), node("p", source.value));
    box.append(
      node(
        "p",
        disposition[source.disposition] + " · " + category[source.category],
        "form-note",
      ),
    );
  } else box.append(node("p", result.memory.content));
  if (source.source_excerpt) {
    const section = node("section", undefined, "source-excerpt");
    section.append(
      node("span", "随记录保存的来源摘录 · 人工整理", "small-title"),
    );
    section.append(node("h3", source.source_title || "来源文档"));
    section.append(node("blockquote", source.source_excerpt));
    section.append(node("p", source.source_path, "source-path"));
    if (source.source_sha256) {
      const details = node("details", undefined, "technical");
      details.append(
        node("summary", "来源文件冻结哈希"),
        node("code", source.source_sha256),
      );
      section.append(details);
    }
    section.append(
      node(
        "p",
        "此处展示载入时的摘录快照，不代表实时读取文件或自动验证结论。",
        "form-note",
      ),
    );
    box.append(section);
  }
  box.append(
    node(
      "p",
      "记忆版本 " +
        result.memory.version +
        " · " +
        (statusNames[result.memory.status] || result.memory.status),
      "form-note",
    ),
  );
  for (const e of result.events) {
    if (source.source_excerpt) continue;
    const section = node("div", undefined, "source-event");
    section.append(
      node(
        "small",
        "会话 " +
          (e.source_message_id || "未标记") +
          " · " +
          (source.session?.startsWith("repo-casebook-")
            ? recordDate(result.memory)
            : date(e.observed_at)),
      ),
    );
    let text = e.payload.text;
    if (text.startsWith("DIVE_PROJECT_RECORD_V1\n")) {
      const d = JSON.parse(text.split("\n").slice(1).join("\n"));
      text =
        d.key +
        "：" +
        d.value +
        "\n状态：" +
        disposition[d.disposition] +
        "\n依据：" +
        (d.reason || "未填写");
    }
    section.append(node("p", text));
    box.append(section);
  }
  const detail = node("details", undefined, "technical");
  detail.append(node("summary", "检查原始来源与版本记录"));
  detail.append(node("pre", JSON.stringify(result, null, 2)));
  box.append(detail);
  $("source-dialog").showModal();
}

async function showWriteDecision(eventId) {
  const context = contextVersion;
  const result = await api(
    "/api/events/" + encodeURIComponent(eventId) + "?" + params(),
  );
  if (context !== contextVersion) return;
  const box = $("source-content");
  box.replaceChildren(node("p", "本次输入的实际写入判定"));
  for (const decision of result.write_decisions) {
    box.append(
      node(
        "p",
        (decision.accepted ? "保留为记忆：" : "未保留为记忆：") +
          decision.reason,
      ),
    );
  }
  const details = node("details", undefined, "technical");
  details.append(
    node("summary", "检查写入记录"),
    node("pre", JSON.stringify(result, null, 2)),
  );
  box.append(details);
  $("source-dialog").showModal();
}
function setInputMode() {
  const mode = document.querySelector('[name="input-mode"]:checked').value;
  const structured = mode === "record";
  $("record-fields").hidden = !structured;
  $("text-fields").hidden = structured;
  $("record-key").required = structured;
  $("record-value").required = structured;
  $("event-text").required = !structured;
  $("input-note").textContent = structured
    ? "由你明确确认字段和状态，系统保留原始输入及版本。"
    : "使用现有启发式写入门；这不是完整的语义抽取模型。拒绝写入也会保留判定。";
}
function openRecord(memory = null) {
  recordRequestId = crypto.randomUUID();
  recordObservedAt = new Date().toISOString();
  recordDateDisplay = localDate(recordObservedAt);
  $("record-form").reset();
  $("replaces").value = memory?.id || "";
  $("record-heading").textContent = memory ? "更新明确记录" : "记录新信息";
  $("record-date").value = recordDateDisplay;
  document.querySelector('[name="input-mode"][value="text"]').disabled =
    !!memory;
  $("record-key").readOnly = !!memory;
  $("record-category").disabled = !!memory;
  $("record-disposition").disabled = !!memory;
  if (memory) {
    const d = memory.structured_content;
    $("record-key").value = d.key;
    $("record-value").value = d.value;
    $("record-category").value = d.category;
    $("record-disposition").value = d.disposition;
    $("record-reason").value = d.reason;
    $("record-session").value = d.session;
  }
  setInputMode();
  $("record-dialog").showModal();
}
function openForget(memory) {
  $("forget-id").value = memory.id;
  $("forget-label").textContent =
    memory.structured_content.key || memory.content;
  $("forget-dialog").showModal();
}
function renderTrace() {
  $("trace-empty").hidden = true;
  $("trace-result").hidden = false;
  $("answer-status").textContent =
    lastTrace.status === "EVIDENCE_ONLY"
      ? "证据已检索 · 未调用回答模型"
      : lastTrace.status;
  $("answer-text").textContent =
    lastTrace.answer ||
    (lastTrace.status === "EVIDENCE_ONLY"
      ? "以下是本次检索得到的记忆。它们不代表系统已经生成或验证了问题的答案。"
      : lastTrace.reader?.reason_code || "没有得到有效回答。");
  $("evidence-budget").textContent =
    lastTrace.evidence.length +
    " 个片段 · 估计 " +
    lastTrace.estimated_tokens +
    " / " +
    lastTrace.token_budget +
    " tokens";
  $("evidence-list").replaceChildren(
    ...lastTrace.evidence.map((m) => recordCard(m)),
  );
  $("answer-citations").replaceChildren();
  for (const c of lastTrace.reader?.citations || []) {
    $("answer-citations").append(
      action("“" + c.quote + "” ↗", () => showSource(c.memory_id)),
    );
  }
  $("trace-detail").textContent = JSON.stringify(
    {
      mode: lastTrace.mode,
      timing_ms: lastTrace.timing_ms,
      token_counter: lastTrace.token_counter,
      retrieval: lastTrace.retrieval,
      packing: lastTrace.packing,
      reader: lastTrace.reader,
    },
    null,
    2,
  );
}
async function loadVerification() {
  const d = await api("/api/verification");
  const box = $("verification");
  box.replaceChildren();
  for (const section of d.sections) {
    const part = node(
      section.collapsed ? "details" : "section",
      undefined,
      "report",
    );
    part.append(node(section.collapsed ? "summary" : "h2", section.title));
    if (section.value) part.append(node("div", section.value, "report-stat"));
    part.append(node("p", section.description));
    if (section.rows) {
      const table = node("table");
      const head = node("thead");
      const hr = node("tr");
      for (const x of section.columns) hr.append(node("th", x));
      head.append(hr);
      table.append(head);
      const body = node("tbody");
      for (const row of section.rows) {
        const tr = node("tr");
        for (const value of row) tr.append(node("td", String(value)));
        body.append(tr);
      }
      table.append(body);
      part.append(table);
    }
    if (section.items) {
      const list = node("ul", undefined, "pending-list");
      for (const x of section.items) list.append(node("li", x));
      part.append(list);
    }
    box.append(part);
  }
  box.append(node("p", "报告来源：" + d.provenance, "form-note"));
}
async function run(fn) {
  try {
    await fn();
  } catch (error) {
    showMessage(error.message, true);
  }
}
document.querySelectorAll("[data-tab]").forEach((b) =>
  b.addEventListener("click", () => {
    switchTab(b.dataset.tab);
    if (b.dataset.tab === "verify") run(loadVerification);
  }),
);
document
  .querySelectorAll("[data-close]")
  .forEach((b) =>
    b.addEventListener("click", () => $(b.dataset.close).close()),
  );
document
  .querySelectorAll('[name="input-mode"]')
  .forEach((el) => el.addEventListener("change", setInputMode));
$("add-record").onclick = () => openRecord();
$("empty-add").onclick = () => openRecord();
$("open-project").onclick = () =>
  run(async () => {
    project = $("project").value.trim();
    cutoff = null;
    since = null;
    $("since").value = "";
    $("as-of").value = "";
    invalidate();
    $("confirmed").replaceChildren();
    $("pending").replaceChildren();
    $("casebook-banner").hidden = true;
    await refresh();
  });
$("apply-token").onclick = () =>
  run(async () => {
    accessToken = $("access-token").value;
    $("access-token").value = "";
    await refresh();
    showMessage("访问设置已应用。");
  });
$("apply-time").onclick = () =>
  run(async () => {
    cutoff = $("as-of").value ? new Date($("as-of").value).toISOString() : null;
    invalidate();
    await refresh();
  });
$("now").onclick = () =>
  run(async () => {
    cutoff = null;
    $("as-of").value = "";
    invalidate();
    await refresh();
  });
async function loadProjectExample(kind) {
  const buttons = [$("load-demo"), $("load-casebook"), $("empty-casebook")];
  if (buttons.some((b) => b.disabled)) return;
  buttons.forEach((b) => {
    b.disabled = true;
  });
  try {
    const name = kind + "-" + crypto.randomUUID().slice(0, 8);
    const d = await api("/api/" + kind, {
      method: "POST",
      body: JSON.stringify({ project: name }),
    });
    project = d.namespace;
    $("project").value = project;
    cutoff = null;
    since = null;
    $("as-of").value = "";
    $("since").value = "";
    invalidate();
    await refresh();
    switchTab("brief");
    $("record-management").open = kind === "demo";
    if (kind === "casebook") $("message").hidden = true;
    else showMessage(d.notice);
  } finally {
    buttons.forEach((b) => {
      b.disabled = false;
    });
  }
}
$("load-demo").onclick = () => run(() => loadProjectExample("demo"));
$("load-casebook").onclick = () => run(() => loadProjectExample("casebook"));
$("empty-casebook").onclick = () => run(() => loadProjectExample("casebook"));
$("answer-mode").onchange = setAnswerMode;
$("apply-since").onclick = () =>
  run(async () => {
    since = $("since").value ? new Date($("since").value).toISOString() : null;
    invalidate();
    await refresh();
  });
$("clear-since").onclick = () =>
  run(async () => {
    since = null;
    $("since").value = "";
    invalidate();
    await refresh();
  });
$("copy-handoff").onclick = () =>
  run(async () => {
    if (!brief) return;
    try {
      await navigator.clipboard.writeText(brief.markdown);
      showMessage(
        "交接简报已复制，包含当前状态、待确认事项和来源。可带到下一次会话。",
      );
    } catch {
      downloadText(
        brief.markdown,
        "project-handoff.md",
        "text/markdown;charset=utf-8",
      );
      showMessage("浏览器未允许复制，已改为下载同一份交接简报。");
    }
  });
$("download-handoff").onclick = () => {
  if (brief)
    downloadText(
      brief.markdown,
      "project-handoff.md",
      "text/markdown;charset=utf-8",
    );
};
$("record-form").onsubmit = (e) => {
  e.preventDefault();
  run(async () => {
    const submit = e.submitter;
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      const structured =
        document.querySelector('[name="input-mode"]:checked').value ===
        "record";
      const body = {
        project,
        idempotency_key: recordRequestId,
        observed_at:
          $("record-date").value === recordDateDisplay
            ? recordObservedAt
            : new Date($("record-date").value).toISOString(),
        session: $("record-session").value,
      };
      if (structured)
        Object.assign(body, {
          key: $("record-key").value,
          value: $("record-value").value,
          category: $("record-category").value,
          disposition: $("record-disposition").value,
          reason: $("record-reason").value,
          replaces: $("replaces").value || null,
        });
      else
        Object.assign(body, {
          text: $("event-text").value,
          explicit: $("event-explicit").checked,
        });
      const result = await api(structured ? "/api/records" : "/api/events", {
        method: "POST",
        body: JSON.stringify(body),
      });
      $("record-dialog").close();
      invalidate();
      await refresh();
      showMessage(
        result.memory_ids.length
          ? "记录已保存，当前状态已重新读取。"
          : "事件已保存；当前写入门未将其保留为长期记忆。",
      );
      $("message").append(
        action("查看本次写入依据 ↗", () => showWriteDecision(result.event_id)),
      );
    } finally {
      submit.disabled = false;
    }
  });
};
$("lookup-form").onsubmit = (e) => {
  e.preventDefault();
  run(async () => {
    const context = contextVersion;
    const d = await api(
      "/api/lookup?" +
        params({
          key: $("lookup-key").value,
          ...(cutoff ? { as_of: cutoff } : {}),
        }),
    );
    if (context !== contextVersion) return;
    $("lookup-result").textContent =
      d.status === "ANSWER"
        ? "确认记录：" + d.value
        : "保持未知：" +
          (d.reason === "conflict"
            ? "有多个冲突值，需要你明确处理。"
            : "没有找到对应的已确认记录。");
    for (const m of d.sources)
      $("lookup-result").append(action(" 查看来源 ↗", () => showSource(m.id)));
  });
};
$("ask-form").onsubmit = (e) => {
  e.preventDefault();
  run(async () => {
    const b = e.submitter;
    b.disabled = true;
    try {
      const context = contextVersion;
      const answerRequest = ++answerVersion;
      const local = $("answer-mode").value === "local";
      const response = await api(local ? "/api/consult" : "/api/answer", {
        method: "POST",
        body: JSON.stringify({
          project,
          question: $("question").value,
          as_of: cutoff,
          ...(local ? { since } : {}),
        }),
      });
      if (context !== contextVersion || answerRequest !== answerVersion) return;
      if (local) {
        lastConsult = response;
        renderConsult();
      } else {
        lastTrace = response;
        $("consult-result").hidden = true;
        renderTrace();
      }
    } finally {
      b.disabled = false;
    }
  });
};
$("forget-form").onsubmit = (e) => {
  e.preventDefault();
  run(async () => {
    await api(
      "/api/memories/" +
        encodeURIComponent($("forget-id").value) +
        "?" +
        params(),
      { method: "DELETE" },
    );
    $("forget-dialog").close();
    invalidate();
    await refresh();
    showMessage("来源已遗忘，相关记忆和本项目旧回答追踪已清除。");
  });
};
$("download-trace").onclick = () => {
  if (!lastTrace) return;
  downloadText(
    JSON.stringify(lastTrace, null, 2),
    lastTrace.trace_id + ".json",
    "application/json",
  );
};
run(refresh);
