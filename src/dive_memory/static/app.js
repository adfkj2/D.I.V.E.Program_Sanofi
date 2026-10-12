"use strict";
const $ = (id) => document.getElementById(id);
let project = "my-research",
  state = null,
  lastTrace = null,
  accessToken = "",
  cutoff = null,
  recordRequestId = null;
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
function switchTab(name) {
  document.querySelectorAll(".page").forEach((p) => (p.hidden = p.id !== name));
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.tab === name);
    b.setAttribute("aria-current", b.dataset.tab === name ? "page" : "false");
  });
}
async function refresh() {
  const [ready, snapshot] = await Promise.all([
    api("/api/readiness"),
    api("/api/snapshot?" + params(cutoff ? { as_of: cutoff } : {})),
  ]);
  state = snapshot;
  $("project-label").textContent = project;
  $("mode").textContent =
    ready.mode === "live_model" ? "在线模型已配置" : "本地证据浏览";
  $("reader-description").textContent =
    ready.mode === "live_model"
      ? "回答器仅接收本次选中的证据；引用和失败状态可以检查。"
      : "当前未配置回答模型。检索照常运行，只展示真实证据，不生成模型回答。";
  renderBrief();
  renderTimeline();
}
function invalidate() {
  lastTrace = null;
  $("trace-result").hidden = true;
  $("trace-empty").hidden = false;
  $("lookup-result").replaceChildren();
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
        date(memory.valid_from || memory.observed_at),
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
      !m.structured_content.disposition ||
      m.structured_content.disposition === "confirmed",
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
function renderTimeline() {
  const rows = [...state.timeline].sort((a, b) =>
    (b.valid_from || "").localeCompare(a.valid_from || ""),
  );
  $("timeline-list").replaceChildren(
    ...rows.map((m) => {
      const item = node("div", undefined, "timeline-item");
      item.append(node("time", date(m.valid_from || m.observed_at)));
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
  const result = await api(
    "/api/sources/" + encodeURIComponent(id) + "?" + params(),
  );
  const box = $("source-content");
  box.replaceChildren();
  box.append(node("p", result.memory.content));
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
    const section = node("div", undefined, "source-event");
    section.append(
      node(
        "small",
        "会话 " +
          (e.source_message_id || "未标记") +
          " · " +
          date(e.observed_at),
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
  const result = await api(
    "/api/events/" + encodeURIComponent(eventId) + "?" + params(),
  );
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
  $("record-form").reset();
  $("replaces").value = memory?.id || "";
  $("record-heading").textContent = memory ? "更新明确记录" : "记录新信息";
  $("record-date").value = localDate();
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
    $("as-of").value = "";
    invalidate();
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
$("load-demo").onclick = () =>
  run(async () => {
    const name =
      "demo-" + new Date().toISOString().replace(/\D/g, "").slice(0, 14);
    const d = await api("/api/demo", {
      method: "POST",
      body: JSON.stringify({ project: name }),
    });
    project = d.namespace;
    $("project").value = project;
    cutoff = null;
    $("as-of").value = "";
    invalidate();
    await refresh();
    switchTab("brief");
    showMessage(d.notice);
  });
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
        observed_at: new Date($("record-date").value).toISOString(),
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
    const d = await api(
      "/api/lookup?" +
        params({
          key: $("lookup-key").value,
          ...(cutoff ? { as_of: cutoff } : {}),
        }),
    );
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
      lastTrace = await api("/api/answer", {
        method: "POST",
        body: JSON.stringify({
          project,
          question: $("question").value,
          as_of: cutoff,
        }),
      });
      renderTrace();
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
  const a = document.createElement("a");
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(lastTrace, null, 2)], {
      type: "application/json",
    }),
  );
  a.href = url;
  a.download = lastTrace.trace_id + ".json";
  a.click();
  URL.revokeObjectURL(url);
};
run(refresh);
