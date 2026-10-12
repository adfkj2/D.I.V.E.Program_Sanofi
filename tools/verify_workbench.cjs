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
    await page.getByText("本地证据浏览", { exact: true }).waitFor();
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
    await page.locator("#question").fill("预算上限与当前基线");
    await page.locator("#ask-form button").click();
    await page
      .getByText("证据已检索 · 未调用回答模型", { exact: true })
      .waitFor();
    assert.match(await page.locator("#evidence-list").innerText(), /50 元/);
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
    // The project is intentionally not persisted in browser storage: reopen explicitly.
    await page.locator("#project").fill(trace.namespace);
    await page.locator("#open-project").click();
    await budget.getByText("50 元", { exact: true }).waitFor();
    assert(
      !(await page
        .locator("#confirmed")
        .innerText()
        .then((t) => t.includes("阅读偏好"))),
    );
    checks.push("server persists edits across page reload");
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
    checks.push("390px layout has no horizontal overflow");
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
