/* Real browser and API check against a disposable local database. */
const { chromium } = require("playwright");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const output = path.resolve(process.argv[2]);
  await fs.mkdir(output, { recursive: false });
  const temporaryRoot = await fs.realpath(os.tmpdir());
  const fixturePrefix = "dive-project-import-";
  const temporaryPath = await fs.mkdtemp(
    path.join(temporaryRoot, fixturePrefix),
  );
  try {
    await verifyImport(output, path.join(temporaryPath, "论文项目"));
  } finally {
    // Only remove the directory created above; never derive cleanup from output.
    const ownedDirectory = path.resolve(temporaryPath);
    assert(path.isAbsolute(temporaryPath));
    assert.equal(path.dirname(ownedDirectory), temporaryRoot);
    assert(path.basename(ownedDirectory).startsWith(fixturePrefix));
    const stat = await fs.lstat(ownedDirectory);
    assert(stat.isDirectory() && !stat.isSymbolicLink());
    assert.equal(await fs.realpath(ownedDirectory), ownedDirectory);
    await fs.rm(ownedDirectory, { recursive: true });
  }
}

async function verifyImport(output, fixture) {
  await fs.mkdir(path.join(fixture, "docs"), { recursive: true });
  await fs.mkdir(path.join(fixture, "src"));
  await fs.mkdir(path.join(fixture, "node_modules", "pkg"), {
    recursive: true,
  });
  await fs.writeFile(
    path.join(fixture, "README.md"),
    "# 论文项目\n比较两种误差指标。\n",
  );
  await fs.writeFile(
    path.join(fixture, "docs", "进度.md"),
    "# 当前进度\n已完成基线实验。\n- [x] 整理样本\n- [ ] 比较 MAE\n- [ ] 撰写初稿\n",
  );
  await fs.writeFile(
    path.join(fixture, "src", "main.py"),
    "print('example')\n",
  );
  await fs.writeFile(
    path.join(fixture, ".env"),
    "API_KEY=fixture-never-import-this\n",
  );
  await fs.writeFile(
    path.join(fixture, "node_modules", "pkg", "README.md"),
    "- [x] 依赖示例，不是项目进度\n",
  );
  const largeFixture = path.join(path.dirname(fixture), "large-project");
  await fs.mkdir(path.join(largeFixture, "a-src"), { recursive: true });
  await fs.mkdir(path.join(largeFixture, "z-docs"));
  for (let offset = 0; offset < 2000; offset += 100) {
    await Promise.all(
      Array.from({ length: 100 }, (_, index) =>
        fs.writeFile(
          path.join(
            largeFixture,
            "a-src",
            String(offset + index).padStart(4, "0") + ".py",
          ),
          "# fixture\n",
        ),
      ),
    );
  }
  await fs.writeFile(
    path.join(largeFixture, "z-docs", "README.md"),
    "# Large project\n保留 README 证据。\n",
  );
  await fs.writeFile(
    path.join(largeFixture, "z-docs", "progress.md"),
    "# 当前进度\n保留进度证据。\n- [ ] 核对优先资料\n",
  );
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.DIVE_BROWSER_EXECUTABLE
      ? { executablePath: process.env.DIVE_BROWSER_EXECUTABLE }
      : {}),
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1050 },
  });
  const errors = [],
    requests = [],
    checks = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => requests.push(request.url()));
  const url = process.env.DIVE_TEST_URL || "http://127.0.0.1:8765/";
  try {
    await page.goto(url);
    await page.locator("#new-project").click();
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    await page.route(
      "**/api/project-import/preview",
      async (route) => {
        await gate;
        await route.continue();
      },
      { times: 1 },
    );
    const started = page.waitForRequest("**/api/project-import/preview");
    await page.locator("#project-folder").setInputFiles(fixture);
    await started;
    assert.equal(await page.locator("#project-save").isDisabled(), true);
    await page.locator("#project-import-clear").click();
    const response = page.waitForResponse("**/api/project-import/preview");
    release();
    await response;
    // Round-trip another browser task after the stale response's handler.
    await page.locator("#project-name").fill("new name");
    assert.equal(
      await page.locator("#project-import-preview").isVisible(),
      false,
    );
    assert.equal(await page.locator("#project-save").isEnabled(), true);
    checks.push(
      "analysis blocks premature save; removing the folder discards a delayed preview",
    );
    await page.locator("#project-name").fill("");
    await page.locator("#project-folder").setInputFiles(fixture);
    await page.locator("#project-import-preview:not([hidden])").waitFor();
    assert.equal(await page.locator("#project-name").inputValue(), "论文项目");
    assert.match(
      await page.locator("#project-progress").inputValue(),
      /已完成基线实验/,
    );
    assert.match(
      await page.locator("#project-progress").inputValue(),
      /docs\/进度.md:3/,
    );
    assert.equal(await page.locator("#project-import-tasks input").count(), 2);
    const firstTask = page.locator("#project-import-tasks input").first();
    await firstTask.uncheck();
    assert.equal(await page.locator("#project-next-step").inputValue(), "");
    await firstTask.check();
    await page.locator("#project-next-step").fill("比较 MAE");
    await page.locator("#project-import-tasks input").last().uncheck();
    await page
      .locator("#project-progress")
      .fill("已核对：基线完成，目前需要比较 MAE。\n资料来自 docs/进度.md。");
    await page.screenshot({
      path: path.join(output, "01-import-desktop.png"),
      fullPage: true,
    });
    checks.push(
      "Chinese folder and nested documents populate sourced progress and editable task selection",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    assert(
      await page
        .locator("#project-dialog")
        .evaluate((element) => element.scrollWidth <= element.clientWidth),
    );
    await page.screenshot({ path: path.join(output, "02-import-mobile.png") });
    checks.push("390px import dialog has no horizontal overflow");
    await page
      .locator("#project-progress")
      .fill(
        (await page.locator("#project-progress").inputValue()) +
          "<script>document.title='unsafe'</script>",
      );
    await page.locator("#project-save").click();
    await page
      .locator("#message")
      .getByText("项目已导入，初始进度、资料来源和下一步已保存。", {
        exact: true,
      })
      .waitFor();
    const project = new URL(page.url()).searchParams.get("project");
    const details = await (
      await page.request.get(
        new URL("/api/project?project=" + project, url).href,
      )
    ).json();
    assert.equal(details.initialization.folder_name, "论文项目");
    assert.equal(details.initialization.sources.length, 2);
    assert.equal(details.tasks.length, 1);
    assert.equal(details.tasks[0].title, "比较 MAE");
    const snapshot = await (
      await page.request.get(
        new URL("/api/snapshot?project=" + project, url).href,
      )
    ).json();
    assert.equal(snapshot.memories.length, 3);
    assert(!JSON.stringify(snapshot).includes("fixture-never-import-this"));
    assert(!JSON.stringify(snapshot).includes("依赖示例"));
    await page.reload();
    await page.locator("#assistant:not([hidden])").waitFor();
    await page.locator('[data-tab="brief"]').click();
    await page
      .locator("#checkpoint-latest")
      .getByText(/已核对：基线完成/)
      .waitFor();
    assert.notEqual(await page.title(), "unsafe");
    await page.screenshot({
      path: path.join(output, "03-restored-progress-mobile.png"),
      fullPage: true,
    });
    checks.push(
      "confirmed progress, selected tasks and source memories survive reload, exclude secrets and render text safely",
    );
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.locator("#edit-project").click();
    assert.equal(await page.locator("#project-import").isVisible(), false);
    await page.locator("#project-name").fill("论文 · 已导入");
    await page.locator("#project-save").click();
    await page.getByText("项目设置已保存。", { exact: true }).waitFor();
    checks.push(
      "existing project settings remain editable after folder initialization",
    );
    await page.locator("#new-project").click();
    await page.locator("#project-folder").evaluate((input) => {
      // Native directory enumeration varies by OS; keep real files and place
      // source files first to exercise the adversarial enumeration order.
      input.addEventListener(
        "change",
        () => {
          const transfer = new DataTransfer();
          Array.from(input.files)
            .sort((a, b) =>
              a.webkitRelativePath.localeCompare(b.webkitRelativePath),
            )
            .forEach((file) => transfer.items.add(file));
          input.files = transfer.files;
        },
        { capture: true, once: true },
      );
    });
    const largeRequest = page.waitForRequest("**/api/project-import/preview");
    await page.locator("#project-folder").setInputFiles(largeFixture);
    const largeBody = (await largeRequest).postDataJSON();
    await page.locator("#project-import-preview:not([hidden])").waitFor();
    assert.equal(largeBody.paths.length, 2000);
    assert.deepEqual(
      largeBody.documents.map((document) => document.path).sort(),
      ["z-docs/README.md", "z-docs/progress.md"],
    );
    assert.match(
      await page.locator("#project-progress").inputValue(),
      /保留进度证据/,
    );
    assert.equal(await page.locator("#project-import-tasks input").count(), 1);
    assert.match(
      await page.locator("#project-import-warnings").textContent(),
      /达到导入上限/,
    );
    await page
      .locator('#project-dialog [data-close="project-dialog"]')
      .last()
      .click();
    checks.push(
      "the 2000-path limit retains progress and README evidence in a real 2002-file folder",
    );
    await page.locator("#new-project").click();
    await page.locator("#project-name").fill("迟到的真实保存");
    let releaseSave;
    const saveGate = new Promise((resolve) => {
      releaseSave = resolve;
    });
    await page.route(
      "**/api/projects",
      async (route) => {
        await saveGate;
        await route.continue();
      },
      { times: 1 },
    );
    const saveStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/projects",
    );
    await page.locator("#project-save").click();
    await saveStarted;
    await page
      .locator('#project-dialog [data-close="project-dialog"]')
      .last()
      .click();
    await page.locator("#new-project").click();
    await page.locator("#project-name").fill("保留的新项目草稿");
    const savedResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/projects",
    );
    releaseSave();
    const saved = await (await savedResponse).json();
    assert.equal(saved.name, "迟到的真实保存");
    await page.request.get(new URL("/api/projects", url).href);
    assert.equal(await page.locator("#project-dialog").isVisible(), true);
    assert.equal(
      await page.locator("#project-name").inputValue(),
      "保留的新项目草稿",
    );
    assert.equal(await page.locator("#project-save").isEnabled(), true);
    assert.equal(await page.locator("#project-import-clear").isEnabled(), true);
    assert.equal(await page.locator("#project-folder").isEnabled(), true);
    await page.locator("#project-folder").setInputFiles(fixture);
    await page.locator("#project-import-preview:not([hidden])").waitFor();
    assert.equal(
      await page.locator("#project-name").inputValue(),
      "保留的新项目草稿",
    );
    assert.equal(await page.locator("#project-save").isEnabled(), true);
    await page
      .locator('#project-dialog [data-close="project-dialog"]')
      .last()
      .click();
    checks.push(
      "a delayed real project save preserves a newer draft and its usable folder selection",
    );
    assert.deepEqual(errors, []);
    assert(
      requests.every((request) => new URL(request).hostname === "127.0.0.1"),
    );
    const summary = {
      passed: true,
      checks,
      consoleErrors: errors,
      externalRequests: 0,
      browser: await browser.version(),
    };
    await fs.writeFile(
      path.join(output, "summary.json"),
      JSON.stringify(summary, null, 2),
    );
    console.log(JSON.stringify(summary));
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
