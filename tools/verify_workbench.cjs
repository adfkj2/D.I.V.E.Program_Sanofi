/* Run against a disposable local database. No external network or saved answers. */
const { chromium } = require("playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const output = path.resolve(process.argv[2] || "eval/reports/ui-check");
  await fs.mkdir(output, { recursive: false });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.DIVE_BROWSER_EXECUTABLE
      ? { executablePath: process.env.DIVE_BROWSER_EXECUTABLE }
      : {}),
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1050 },
    reducedMotion: "reduce",
  });
  const errors = [],
    requests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("request", (req) => requests.push(req.url()));
  const checks = [];
  try {
    await page.goto(process.env.DIVE_TEST_URL || "http://127.0.0.1:8765/");
    await page.getByText("AI 助手 · 待连接模型", { exact: true }).waitFor();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator("#assistant-onboarding:not([hidden])").waitFor();
    assert.equal(await page.locator("#assistant-send").isEnabled(), false);
    await page.locator("#assistant-connect").click();
    assert.equal(
      await page.locator("#assistant-model").inputValue(),
      "deepseek-chat",
    );
    await page.locator("#assistant-settings-close").click();
    checks.push(
      "assistant is the primary entry, model setup is explicit and no offline AI reply is fabricated",
    );
    await page.screenshot({ path: path.join(output, "00-onboarding.png") });
    await page.locator("#new-project").click();
    await page.locator("#project-name").fill("个人论文 · 自动验证");
    await page.locator("#project-goal").fill("完成可复现的初稿");
    // Hold a real activation request to exercise the loading boundary reliably.
    let releaseActivation;
    const activationGate = new Promise((resolve) => {
      releaseActivation = resolve;
    });
    const activationStarted = page.waitForRequest(
      (request) =>
        request.method() === "GET" &&
        new URL(request.url()).pathname === "/api/project",
    );
    await page.route(
      "**/api/project?*",
      async (route) => {
        await activationGate;
        await route.continue();
      },
      { times: 1 },
    );
    await page.locator("#project-form button[type=submit]").click();
    await activationStarted;
    const visibleWhileLoading = await page.locator(".page:visible").count();
    releaseActivation();
    assert.equal(visibleWhileLoading, 0);
    // Closing the dialog only confirms creation; activation still has to finish.
    await page
      .locator("#message")
      .getByText("项目已创建。连接模型后，告诉助手你想推进什么。", {
        exact: true,
      })
      .waitFor();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page
      .locator("#workspace-heading")
      .getByText("个人论文 · 自动验证")
      .waitFor();
    checks.push(
      "project creation waits for real activation before navigating, including a delayed response",
    );
    const personalProject = await page.locator("#project").inputValue();
    await page.locator("#add-record").click();
    await page.locator("#record-key").fill("实验进展");
    await page.locator("#record-value").fill("基线完成，接下来比较误差。");
    await page.reload();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page.locator("#add-record").click();
    assert.equal(
      await page.locator("#record-value").inputValue(),
      "基线完成，接下来比较误差。",
    );
    await page.locator("#record-form button[type=submit]").click();
    await page.locator("#record-search").fill("比较误差");
    await page
      .locator("#search-status")
      .getByText(/找到 1 条/)
      .waitFor();
    await page.locator("#add-task").click();
    await page.locator("#task-title").fill("整理三篇论文");
    await page.locator("#task-form button[type=submit]").click();
    await page
      .getByRole("button", { name: "完成：整理三篇论文", exact: true })
      .click();
    await page.locator("#completed-tasks > summary").click();
    await page
      .getByRole("button", { name: "重新打开：整理三篇论文", exact: true })
      .click();
    await page.locator("#finish-session").click();
    await page
      .locator("#checkpoint-note")
      .fill("已完成基线，下次核查误差定义。");
    await page.locator("#checkpoint-next").fill("比较两种方案的误差");
    await page.locator("#checkpoint-form button[type=submit]").click();
    await page.locator("#checkpoint-dialog").waitFor({ state: "hidden" });
    await page.reload();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page
      .locator("#checkpoint-latest")
      .getByText("已完成基线，下次核查误差定义。")
      .waitFor();
    assert.equal(await page.locator("#project").inputValue(), personalProject);
    assert.equal(await page.locator("#task-list .task-row").count(), 2);
    await page.locator(".time-history > summary").click();
    await page.locator("#as-of").fill("2026-01-01T10:00");
    await page.locator("#apply-time").click();
    await page
      .locator("#time-label")
      .getByText(/历史时点/)
      .waitFor();
    assert.equal(await page.locator("#daily-work").isVisible(), false);
    assert.equal(await page.locator("#since").inputValue(), "");
    await page.locator("#now").click();
    await page.locator("#daily-work").waitFor();
    await page.locator(".time-history > summary").click();
    const personalDownload = page.waitForEvent("download");
    await page.locator("#download-handoff").click();
    const personalHandoffPath = path.join(output, "personal-handoff.md");
    await (await personalDownload).saveAs(personalHandoffPath);
    const personalHandoff = await fs.readFile(personalHandoffPath, "utf8");
    assert.match(personalHandoff, /比较两种方案的误差/);
    assert.match(personalHandoff, /个人论文/);
    await page.locator("#backup-open").click();
    const personalBackup = page.waitForEvent("download");
    await page.locator("#download-backup").click();
    const personalBackupPath = path.join(output, "personal-backup.db");
    await (await personalBackup).saveAs(personalBackupPath);
    assert.equal(
      (await fs.readFile(personalBackupPath)).subarray(0, 15).toString(),
      "SQLite format 3",
    );
    checks.push(
      "personal project: draft reload, save/search, complete/reopen, checkpoint, resume, handoff and full backup",
    );
    await page.locator("#load-demo").click();
    await page
      .locator("#confirmed .record")
      .filter({ hasText: "预算上限" })
      .getByText("40 元", { exact: true })
      .waitFor();
    checks.push(
      "real demo input renders confirmed/proposed/rejected separately",
    );
    await page.screenshot({
      path: path.join(output, "01-current.png"),
      fullPage: true,
    });
    const budget = page.locator("#confirmed .record").filter({
      has: page.getByRole("heading", { name: "预算上限", exact: true }),
    });
    await budget.getByRole("button", { name: "查看来源 ↗" }).click();
    await page.locator("#source-dialog[open]").waitFor();
    assert.match(await page.locator("#source-content").innerText(), /40 元/);
    await page.locator("#source-dialog .close").click();
    await page.locator(".time-history > summary").click();
    await page.locator("#as-of").fill("2026-10-02T23:59");
    await page.locator("#apply-time").click();
    await budget.getByText("20 元", { exact: true }).waitFor();
    checks.push("historical cutoff restores old budget");
    await page.locator("#now").click();
    await budget.getByText("40 元", { exact: true }).waitFor();
    await budget.getByRole("button", { name: "更新", exact: true }).click();
    await page.locator("#record-value").fill("50 元");
    await page
      .locator("#record-reason")
      .fill("Browser verification: explicit user correction");
    await page.locator("#record-form button[type=submit]").click();
    await budget.getByText("50 元", { exact: true }).waitFor();
    checks.push("UI correction persists through real API");
    await budget.getByRole("button", { name: "更新", exact: true }).click();
    await page.locator("#record-value").fill("55 元");
    await page
      .locator("#record-reason")
      .fill("Second correction without losing timestamp precision");
    await page.locator("#record-form button[type=submit]").click();
    await budget.getByText("55 元", { exact: true }).waitFor();
    checks.push(
      "successive corrections preserve timestamp precision and create valid replacements",
    );
    await page.locator("#lookup-key").fill("方案 C 结果");
    await page.locator("#lookup-form button").click();
    await page
      .locator("#lookup-result")
      .getByText(/保持未知/)
      .waitFor();
    await page.locator("[data-tab=timeline]").click();
    assert.match(await page.locator("#timeline-list").innerText(), /20 元/);
    await page.screenshot({
      path: path.join(output, "02-timeline.png"),
      fullPage: true,
    });
    await page.locator("[data-tab=evidence]").click();
    await page.locator("#answer-mode").selectOption("reader");
    await page.locator("#question").fill("预算上限与当前基线");
    await page.locator("#ask-form button").click();
    await page
      .getByText("证据已检索 · 未调用回答模型", { exact: true })
      .waitFor();
    assert.match(await page.locator("#evidence-list").innerText(), /55 元/);
    const downloadPromise = page.waitForEvent("download");
    await page.locator("#download-trace").click();
    const download = await downloadPromise;
    const tracePath = path.join(output, "exported-trace.json");
    await download.saveAs(tracePath);
    const trace = JSON.parse(await fs.readFile(tracePath, "utf8"));
    assert.equal(trace.status, "EVIDENCE_ONLY");
    assert.equal(trace.cache_replay, false);
    checks.push(
      "real evidence retrieval and trace export; no model answer claimed",
    );
    await page.screenshot({
      path: path.join(output, "03-evidence.png"),
      fullPage: true,
    });
    await page.locator("[data-tab=brief]").click();
    const preference = page
      .locator("#confirmed .record")
      .filter({ hasText: "阅读偏好" });
    await preference.getByRole("button", { name: "遗忘", exact: true }).click();
    await page.locator("#forget-form button[type=submit]").click();
    await page.waitForFunction(
      () =>
        !document.querySelector("#confirmed").innerText.includes("阅读偏好"),
    );
    checks.push(
      "hard erasure removes visible preference and invalidates answer",
    );
    await page.reload();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    // Reload restores the last project; the legacy ID entry remains supported.
    await page.locator(".legacy-project > summary").click();
    await page.locator("#project").fill(trace.namespace);
    await page.locator("#open-project").click();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page.locator("#record-management > summary").click();
    await budget.getByText("55 元", { exact: true }).waitFor();
    assert(
      !(await page
        .locator("#confirmed")
        .innerText()
        .then((t) => t.includes("阅读偏好"))),
    );
    checks.push("server persists edits across page reload");

    // The new primary flow: recover this repository's actual project history.
    await page.locator("#load-casebook").click();
    await page.waitForFunction(() =>
      document.querySelector("#project").value.startsWith("casebook-"),
    );
    await page.locator("#resume-summary").filter({ hasText: /./ }).waitFor();
    const caseProject = await page.locator("#project").inputValue();
    assert(caseProject.startsWith("casebook-"));
    await page.locator("#casebook-banner:not([hidden])").waitFor();
    assert.match(
      await page.locator("#resume-sections .results").innerText(),
      /451\/500/,
    );
    assert.match(
      await page.locator("#resume-sections .rejected").innerText(),
      /不推广/,
    );
    checks.push(
      "repository case loads through live record API with adoption separated",
    );
    await page.screenshot({
      path: path.join(output, "06-project-resumption.png"),
      fullPage: true,
    });
    await page.screenshot({
      path: path.join(output, "06-project-resumption-viewport.png"),
    });
    await page.locator("[data-tab=evidence]").click();
    await page.locator("#answer-mode").selectOption("local");
    await page.locator("#question").fill("官方成绩");
    await page.locator("#ask-form button[type=submit]").click();
    await page
      .locator("#consult-result")
      .getByText(/未评估/)
      .first()
      .waitFor();
    await page.locator("#consult-result button").first().click();
    await page
      .locator("#source-dialog[open] .source-excerpt blockquote")
      .waitFor();
    assert.match(
      await page.locator("#source-content").innerText(),
      /NOT_EVALUATED/,
    );
    assert.match(
      await page.locator("#source-content").innerText(),
      /docs\/oracle\//,
    );
    await page.screenshot({
      path: path.join(output, "07-source-inspection.png"),
      fullPage: true,
    });
    await page.locator("#source-dialog .close").click();
    checks.push(
      "local answer opens stored verbatim repository excerpt and provenance",
    );
    await page.locator("#question").fill("R27 推广决定");
    await page.locator("#ask-form button[type=submit]").click();
    await page
      .locator("#consult-result")
      .getByText(/未采用/)
      .first()
      .waitFor();
    assert.match(await page.locator("#consult-result").innerText(), /不推广/);
    await page.locator("#question").fill("明年产品市场规模会有多大？");
    await page.locator("#ask-form button[type=submit]").click();
    await page.locator("#consult-result.unsupported").waitFor();
    checks.push(
      "rejected decision stays rejected and unsupported prediction produces no invented answer",
    );
    await page.locator("[data-tab=brief]").click();
    if (!(await page.locator("#as-of").isVisible()))
      await page.locator(".time-history > summary").click();
    await page.locator("#as-of").fill("2026-10-10T00:00");
    await page.locator("#apply-time").click();
    await page
      .locator("#resume-sections")
      .getByText(/R26：422\/500/)
      .first()
      .waitFor();
    assert(
      !(await page.locator("#resume-sections").innerText()).includes("451/500"),
    );
    await page.locator("#now").click();
    await page
      .locator("#resume-sections")
      .getByText(/R40：451\/500/)
      .first()
      .waitFor();
    await page.locator("#since").fill("2026-10-10T00:00");
    await page.locator("#apply-since").click();
    await page
      .locator("#recent-changes .change-before")
      .filter({ hasText: "422/500" })
      .waitFor();
    assert.match(await page.locator("#recent-changes").innerText(), /451\/500/);
    checks.push(
      "historical brief and explicit comparison keep old and new full results distinct",
    );
    const handoffDownload = page.waitForEvent("download");
    await page.locator("#download-handoff").click();
    const handoff = await handoffDownload;
    const handoffPath = path.join(output, "exported-handoff.md");
    await handoff.saveAs(handoffPath);
    const markdown = await fs.readFile(handoffPath, "utf8");
    assert.match(markdown, /451\/500/);
    assert.match(markdown, /NOT_EVALUATED/);
    assert.match(markdown, /docs\/oracle\//);
    checks.push(
      "downloaded handoff carries actual project state, caveats and source references",
    );
    await page
      .context()
      .grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.locator("#copy-handoff").click();
    await page.getByText(/交接简报已复制/).waitFor();
    assert.equal(
      (await page.evaluate(() => navigator.clipboard.readText())).replace(
        /\r\n/g,
        "\n",
      ),
      markdown,
    );
    checks.push(
      "clipboard handoff matches the downloaded current-state markdown",
    );
    await page.locator("#clear-since").click();
    await page.reload();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page.locator("#resume-summary").filter({ hasText: /./ }).waitFor();
    assert.equal(await page.locator("#project").inputValue(), caseProject);
    checks.push("page URL restores selected project after reload");
    await page.locator("[data-tab=verify]").click();
    await page.locator("#verification .report").first().waitFor();
    await page.screenshot({
      path: path.join(output, "04-verification.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("[data-tab=brief]").click();
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await page.screenshot({
      path: path.join(output, "05-mobile.png"),
      fullPage: true,
    });
    await page.screenshot({
      path: path.join(output, "05-mobile-viewport.png"),
    });
    checks.push("390px layout has no horizontal overflow");
    await page.locator("[data-tab=evidence]").click();
    await page.locator("#question-shortcuts button").first().click();
    await page.locator("#consult-result:not([hidden])").waitFor();
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    checks.push("mobile local consult completes without horizontal overflow");
    await page.locator("[data-tab=brief]").click();
    await page.locator("#record-management > summary").click();
    const coldCost = page.locator("#confirmed .record").filter({
      has: page.getByRole("heading", { name: "独立冷启动费用", exact: true }),
    });
    await coldCost.getByRole("button", { name: "更新", exact: true }).click();
    await page.locator("#record-value").fill("用户尚未启动新测量");
    await page.locator("#record-form button[type=submit]").click();
    await coldCost.getByText("用户尚未启动新测量", { exact: true }).waitFor();
    assert(!(await coldCost.innerText()).includes("日期精度"));
    await coldCost.getByRole("button", { name: "查看来源 ↗" }).click();
    await page.locator("#source-dialog[open]").waitFor();
    assert.equal(
      await page.locator("#source-dialog .source-excerpt").count(),
      0,
    );
    await page.locator("#source-dialog .close").click();
    checks.push(
      "manual case correction keeps its actual time and does not inherit the frozen report citation",
    );
    assert(requests.every((url) => new URL(url).hostname === "127.0.0.1"));
    assert.deepEqual(errors, []);
    await fs.writeFile(
      path.join(output, "summary.json"),
      JSON.stringify(
        {
          passed: true,
          checks,
          consoleErrors: errors,
          externalRequests: 0,
          browser: await browser.version(),
          viewports: [
            [1440, 1050],
            [390, 844],
          ],
          actualApi: true,
        },
        null,
        2,
      ),
    );
    console.log(
      JSON.stringify({ passed: true, checks: checks.length, output }),
    );
  } catch (error) {
    await page.screenshot({
      path: path.join(output, "failure.png"),
      fullPage: true,
    });
    await fs.writeFile(
      path.join(output, "failure.json"),
      JSON.stringify({ error: String(error), checks, errors }, null, 2),
    );
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
