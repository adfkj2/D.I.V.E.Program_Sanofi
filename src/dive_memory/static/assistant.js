"use strict";
// Separate assistant surface; project data remains owned by the workbench.
(() => {
  let loadVersion = 0;
  let renderedProject = null;
  let configured = false;
  let pending = false;
  const requests = new Map();
  const drafts = new Map();
  const busy = new Set();
  const error = (text) => {
    $("assistant-error").textContent = text || "";
    $("assistant-error").hidden = !text;
  };
  const controls = () => {
    $("assistant-send").disabled = !configured || pending || busy.has(project);
    $("assistant-send").textContent = busy.has(project)
      ? "正在思考与查阅…"
      : "发送给助手 ↗";
    $("assistant-message").disabled = busy.has(project) || pending;
  };
  function openSettings() {
    $("assistant-key").value = "";
    $("assistant-config-error").hidden = true;
    $("assistant-settings").showModal();
  }
  $("assistant-settings-open").onclick = openSettings;
  $("assistant-connect").onclick = openSettings;
  $("assistant-settings-close").onclick = () => $("assistant-settings").close();
  $("assistant-settings").addEventListener("close", () => {
    $("assistant-key").value = "";
  });
  $("assistant-local").onclick = () => switchTab("brief");
  $("assistant-create-project").onclick = () => $("new-project").click();
  $("assistant-message").addEventListener("input", () => {
    drafts.set(project, $("assistant-message").value);
  });
  $("assistant-model-form").onsubmit = async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    submit.disabled = true;
    try {
      await api("/api/assistant/config", {
        method: "POST",
        body: JSON.stringify({
          endpoint: $("assistant-endpoint").value,
          model: $("assistant-model").value,
          api_key: $("assistant-key").value,
        }),
      });
      $("assistant-settings").close();
      await load();
    } catch (e) {
      $("assistant-config-error").textContent = e.message;
      $("assistant-config-error").hidden = false;
    } finally {
      submit.disabled = false;
    }
  };
  function actionSummary(item) {
    if (item.kind === "add_task") return "添加待办 · " + item.title;
    if (item.kind === "update_task")
      return (item.completed ? "完成待办 · " : "更新待办 · ") + item.title;
    if (item.kind === "save_checkpoint")
      return (
        "保存交接 · " +
        item.note +
        (item.next_step
          ? "\n下一步（沿用已有待办或新建）：" + item.next_step
          : "")
      );
    return (
      (item.replaces ? "更新记忆 · " : "保存记忆 · ") +
      item.key +
      "\n" +
      item.value +
      "\n状态：" +
      (disposition[item.disposition] || item.disposition) +
      (item.reason ? "\n依据：" + item.reason : "")
    );
  }
  async function decide(turn, approve, selectedProject) {
    try {
      error("");
      await api(
        "/api/assistant/runs/" + encodeURIComponent(turn.id) + "/decision",
        {
          method: "POST",
          body: JSON.stringify({ project: selectedProject, approve }),
        },
      );
      if (project === selectedProject) await refresh();
    } catch (e) {
      if (project === selectedProject) error(e.message);
    }
  }
  function render(turns, selectedProject) {
    const container = $("assistant-thread");
    container.replaceChildren();
    pending = turns.some(
      (t) => t.status === "pending" || t.status === "running",
    );
    if (!turns.length) {
      const welcome = node("div", undefined, "assistant-welcome");
      welcome.append(
        node("h2", "从一个具体问题开始"),
        node("p", "已有资料会随项目保留。先聊清楚，再把重要决定和行动存下来。"),
      );
      for (const prompt of [
        "上次做到哪里了？请查阅项目记忆，建议今天的下一步。",
        "帮我梳理这个项目的目标和还没完成的任务。",
        "我想记录今天的进展，请引导我整理交接。",
      ])
        welcome.append(
          action(prompt, () => {
            $("assistant-message").value = prompt;
            drafts.set(project, prompt);
            $("assistant-message").focus();
          }),
        );
      container.append(welcome);
    }
    for (const turn of turns) {
      const card = node("article", undefined, "assistant-turn");
      card.append(
        node("p", "你 · " + date(turn.created_at), "eyebrow"),
        node("p", turn.prompt, "assistant-user"),
      );
      const labels = {
        pending: "待你确认 · 尚未执行",
        applied: "已执行并核验",
        error: "本轮未完成",
        cancelled: "已取消",
        running: "正在处理",
        completed: "助手回复",
      };
      card.append(node("p", labels[turn.status], "assistant-turn-status"));
      card.append(node("p", turn.message, "assistant-reply"));
      if (turn.trace?.length) {
        const trace = node("details", undefined, "assistant-trace");
        trace.append(node("summary", "查看处理过程"));
        turn.trace.forEach((step) => trace.append(node("p", step.label)));
        card.append(trace);
      }
      for (const id of turn.citations || [])
        card.append(
          action("核对引用来源 ↗", async () => {
            if (project === selectedProject) await showSource(id);
          }),
        );
      if (turn.status === "pending") {
        const changes = node("ol", undefined, "assistant-changes");
        turn.actions.forEach((item) =>
          changes.append(node("li", actionSummary(item))),
        );
        card.append(changes);
        const confirm = action("确认并执行这些变更", () =>
          decide(turn, true, selectedProject),
        );
        confirm.classList.add("primary");
        card.append(
          confirm,
          action("取消提案", () => decide(turn, false, selectedProject)),
        );
      }
      if (turn.status === "running") card.append(action("刷新处理结果", load));
      container.append(card);
    }
    controls();
  }
  async function load() {
    const version = ++loadVersion;
    const selectedProject = project;
    if (renderedProject !== project) {
      renderedProject = project;
      $("assistant-thread").replaceChildren();
      $("assistant-message").value = drafts.get(project) || "";
      pending = false;
      error("");
      controls();
    }
    try {
      const [config, history] = await Promise.all([
        api("/api/assistant/config"),
        api(
          "/api/assistant/history?" +
            new URLSearchParams({ project: selectedProject }),
        ),
      ]);
      if (version !== loadVersion || selectedProject !== project) return;
      configured = config.configured;
      $("assistant-status").textContent = configured
        ? config.model + " · 连接设置已就绪，发送时调用模型"
        : "尚未连接模型 · 项目资料可继续使用";
      $("assistant-onboarding").hidden = configured;
      $("assistant-endpoint").value = config.endpoint;
      $("assistant-model").value = config.model;
      render(history.turns, selectedProject);
      $("mode").textContent = configured
        ? "AI 助手 · " + config.model
        : "AI 助手 · 待连接模型";
    } catch (e) {
      if (selectedProject === project) error(e.message);
    }
  }
  $("assistant-form").onsubmit = async (event) => {
    event.preventDefault();
    const selectedProject = project;
    if (busy.has(selectedProject) || pending || !configured) return;
    const message = $("assistant-message").value.trim();
    if (!message) return;
    let request = requests.get(selectedProject);
    if (!request || request.message !== message) {
      request = { message, request_id: crypto.randomUUID() };
      requests.set(selectedProject, request);
    }
    busy.add(selectedProject);
    error("");
    controls();
    try {
      const result = await api("/api/assistant/chat", {
        method: "POST",
        body: JSON.stringify({ project: selectedProject, ...request }),
      });
      if (result.status !== "running") requests.delete(selectedProject);
      if (result.status !== "error") {
        drafts.delete(selectedProject);
        if (project === selectedProject) $("assistant-message").value = "";
      }
      if (project === selectedProject) await load();
    } catch (e) {
      if (project === selectedProject) error(e.message);
    } finally {
      busy.delete(selectedProject);
      controls();
    }
  };
  document.addEventListener("dive-project-refreshed", load);
})();
