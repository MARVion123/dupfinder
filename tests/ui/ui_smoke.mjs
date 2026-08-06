/**
 * Headless browser smoke test for the Duplicate Finder UI.
 *
 *   cd tests/ui && npm install && npx playwright install chromium && npm test
 *
 * Self-contained: it builds a throwaway tree of duplicate files, starts the
 * server against a throwaway data dir, drives the real UI in Chromium, and
 * tears everything down. Nothing outside the temp directory is touched.
 *
 * Every console error, uncaught exception and failed request the page produces
 * is collected and reported at the end - a step can "pass" visually while the
 * page is quietly throwing, and that should not go unnoticed.
 */

import { spawn } from "node:child_process";
import { chromium } from "playwright";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const SHOTS = path.join(HERE, "screenshots");
const PORT = 8791 + (process.pid % 100);
const BASE = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.DUPFINDER_PYTHON || "python";

const results = [];
const pageErrors = [];
const badResponses = [];

function step(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

// ---------------------------------------------------------------- fixture

function buildFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "dupfinder-ui-"));
  const tree = path.join(root, "tree");
  for (const d of ["a", "b/sub", "c"]) {
    fs.mkdirSync(path.join(tree, d), { recursive: true });
  }
  // High-entropy content. Repetitive filler (e.g. one hash repeated) makes the
  // CTPH rolling hash degenerate and the near-duplicate pass finds nothing, so
  // seed a PRNG instead - random but reproducible.
  const big = Buffer.alloc(300000);
  let seed = 0x2545f491;
  for (let i = 0; i < big.length; i++) {
    seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
    big[i] = seed & 0xff;
  }
  fs.writeFileSync(path.join(tree, "a/report.bin"), big);
  fs.writeFileSync(path.join(tree, "b/sub/report copy.bin"), big);
  fs.writeFileSync(path.join(tree, "c/report (1).bin"), big);

  const near = Buffer.from(big);                              // near-duplicate
  crypto.randomFillSync(near, near.length - 5000);
  fs.writeFileSync(path.join(tree, "b/report_edited.bin"), near);

  const text = Buffer.from("hello world ".repeat(5000));
  fs.writeFileSync(path.join(tree, "a/notes.txt"), text);
  fs.writeFileSync(path.join(tree, "c/notes-backup.txt"), text);

  fs.writeFileSync(path.join(tree, "a/unique.bin"), crypto.randomBytes(100000));

  const data = path.join(root, "data");
  fs.mkdirSync(data);
  // No BOM - the server silently falls back to defaults on a corrupt config.
  fs.writeFileSync(
    path.join(data, "config.json"),
    JSON.stringify({ roots_allowlist: [tree], ai_enabled: false, port: PORT }, null, 2),
    "utf8",
  );
  return { root, tree, data };
}

// ---------------------------------------------------------------- server

async function startServer(dataDir) {
  const proc = spawn(
    PYTHON,
    ["-m", "dupfinder", "--data-dir", dataDir, "serve", "--host", "127.0.0.1", "--port", String(PORT)],
    { cwd: REPO, stdio: ["ignore", "pipe", "pipe"] },
  );
  let log = "";
  proc.stdout.on("data", (d) => (log += d));
  proc.stderr.on("data", (d) => (log += d));

  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`${BASE}/api/status`);
      if (r.ok) return proc;
    } catch { /* not listening yet */ }
    if (proc.exitCode !== null) throw new Error(`server exited (${proc.exitCode}):\n${log}`);
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server did not come up in 15s:\n${log}`);
}

// ---------------------------------------------------------------- test

async function run() {
  fs.rmSync(SHOTS, { recursive: true, force: true });
  fs.mkdirSync(SHOTS, { recursive: true });

  const fx = buildFixture();
  console.log(`fixture : ${fx.tree}`);
  const server = await startServer(fx.data);
  console.log(`server  : ${BASE}\n`);

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  page.on("pageerror", (e) => pageErrors.push(`uncaught: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") pageErrors.push(`console.${m.type()}: ${m.text()}`);
  });
  page.on("requestfailed", (r) => badResponses.push(`requestfailed ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.status() >= 400) badResponses.push(`HTTP ${r.status()} ${r.url()}`);
  });

  const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });

  try {
    // 1. Initial load ----------------------------------------------------
    await page.goto(BASE, { waitUntil: "networkidle" });
    step("page loads", (await page.title()) === "Duplicate Finder", await page.title());
    step("empty state visible", await page.locator("#emptyState").isVisible());
    await shot("01-initial");

    // 2. Folder picker ---------------------------------------------------
    await page.click("#btnPick");
    await page.waitForSelector("#modalPick:not(.hidden)");
    await page.waitForSelector("#pickList li");
    step("folder picker opens", await page.locator("#pickList li").count() > 0,
      `${await page.locator("#pickList li").count()} entries`);
    await shot("02-picker-roots");

    // Navigate into the allowed root, then confirm sub-folders are listed.
    await page.click("#pickList li >> nth=0");
    await page.waitForFunction(
      (t) => document.getElementById("pickPath").textContent.includes(t),
      path.basename(fx.tree),
    );
    const dirs = await page.locator("#pickList li").count();
    step("picker navigates into folder", dirs >= 3, `${dirs} entries incl. up-link`);
    step("scan button enabled once a folder is picked",
      !(await page.locator("#btnStartScan").isDisabled()));
    await shot("03-picker-inside");

    // 3. Run a scan ------------------------------------------------------
    await page.click("#btnStartScan");
    // The modal is closed by adding .hidden, so wait on the class, not visibility.
    await page.waitForSelector("#modalPick.hidden", { state: "attached" });
    await page.waitForFunction(() => document.querySelectorAll("tr.group-row").length >= 3,
      null, { timeout: 60000 });
    const groups = await page.locator("tr.group-row").count();
    step("scan completes and renders groups", groups === 3, `${groups} groups`);
    step("summary tiles filled",
      (await page.locator("#sumGroups").textContent()) === "3" &&
      (await page.locator("#sumWasted").textContent()) !== "0 B",
      `${await page.locator("#sumGroups").textContent()} groups / ` +
      `${await page.locator("#sumWasted").textContent()} reclaimable`);
    step("verified badge shown on exact groups",
      await page.locator("span.verified").count() >= 2,
      `${await page.locator("span.verified").count()} verified`);

    // Regression: `.results { overflow: hidden }` made the sticky header stick
    // to the panel instead of the viewport, drawing it over the first data row.
    const layout = await page.evaluate(() => {
      const th = document.querySelector("table.grid thead th").getBoundingClientRect();
      const tr = document.querySelector("tr.group-row").getBoundingClientRect();
      return { overlap: Math.round(Math.min(th.bottom, tr.bottom) - Math.max(th.top, tr.top)) };
    });
    step("table header does not overlap the first row", layout.overlap <= 0,
      `${layout.overlap}px overlap`);
    await shot("04-results");

    // 4. Sorting and filtering ------------------------------------------
    await page.click('th.sortable[data-sort="count"]');
    await page.waitForFunction(() =>
      document.querySelector('th.sortable[data-sort="count"]').classList.contains("sorted"));
    step("column sort marks the header", true, "sorted by copies");

    await page.selectOption("#fKind", "near");
    await page.waitForFunction(() => document.querySelectorAll("tr.group-row").length === 1);
    step("type filter narrows to near-duplicates", true, "1 group");
    await shot("05-filter-near");

    await page.selectOption("#fKind", "all");
    await page.waitForFunction(() => document.querySelectorAll("tr.group-row").length === 3);

    await page.fill("#fSearch", "notes");
    await page.waitForFunction(() => document.querySelectorAll("tr.group-row").length === 1,
      null, { timeout: 5000 });
    step("path search filters", true, "1 group for 'notes'");
    await page.fill("#fSearch", "");
    await page.waitForFunction(() => document.querySelectorAll("tr.group-row").length === 3);

    // 5. Expand a group --------------------------------------------------
    await page.click('th.sortable[data-sort="wasted"]');
    await page.waitForTimeout(300);
    await page.click("tr.group-row >> nth=0");
    await page.waitForSelector("tr.detail");
    const files = await page.locator("tr.detail table.files tr").count();
    step("group expands with its files", files === 3, `${files} files listed`);
    step("per-file verdict tags rendered",
      await page.locator("tr.detail span.tag").count() === 3,
      `${await page.locator("tr.detail span.tag.keep").count()} keep / ` +
      `${await page.locator("tr.detail span.tag.delete").count()} delete`);
    await shot("06-group-expanded");

    // 6. Selection -------------------------------------------------------
    await page.click(".act-select-sugg");
    await page.waitForSelector("#actionBar:not(.hidden)");
    const selText = await page.locator("#selCount").textContent();
    step("'select suggested deletions' fills the action bar", selText.startsWith("2"),
      `${selText}, ${await page.locator("#selBytes").textContent()}`);
    await shot("07-selected");

    // 7. Delete with confirmation ---------------------------------------
    await page.selectOption("#delMode", "quarantine");
    await page.click("#btnDelete");
    await page.waitForSelector("#modalConfirm:not(.hidden)");
    step("delete asks for confirmation first",
      (await page.locator("#confirmTitle").textContent()).includes("Delete 2 files"),
      await page.locator("#confirmText").textContent());
    await shot("08-confirm");

    await page.click("#btnConfirmGo");
    // The toast is shared and may still be showing "Scan finished." - wait for
    // the delete's own text rather than for the element to become visible.
    await page.waitForFunction(
      () => document.getElementById("toast").textContent.includes("removed"),
      null, { timeout: 15000 });
    const toast = (await page.locator("#toast").textContent()).trim();
    step("quarantine delete reports back", toast.includes("2 removed"), toast);

    // 8. Action log + restore -------------------------------------------
    await page.click("#btnLog");
    await page.waitForSelector("#modalLog:not(.hidden)");
    await page.waitForSelector("#logBody tr");
    const logRows = await page.locator("#logBody tr").count();
    const restorable = await page.locator(".act-restore").count();
    step("action log lists the moves", logRows === 2 && restorable === 2,
      `${logRows} rows, ${restorable} restorable`);
    await shot("09-log");

    // Restoring appends a "restore" row and re-renders the log. Note the
    // original quarantine row keeps its Restore button, so the button count
    // does not drop - assert on the new row and the toast instead.
    await page.click(".act-restore >> nth=0");
    await page.waitForFunction(() => document.querySelectorAll("#logBody tr").length === 3,
      null, { timeout: 15000 });
    const restoreToast = (await page.locator("#toast").textContent()).trim();
    const restoredOnDisk = fs.existsSync(path.join(fx.tree, "a", "report.bin")) ||
      fs.existsSync(path.join(fx.tree, "b", "sub", "report copy.bin")) ||
      fs.existsSync(path.join(fx.tree, "c", "report (1).bin"));
    step("restore puts the file back on disk",
      restoreToast.includes("Restored to") && restoredOnDisk,
      restoreToast);
    await shot("10-after-restore");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#modalLog.hidden", { state: "attached" });
    step("Escape closes the modal", true);

    // 9. Settings --------------------------------------------------------
    await page.click("#btnSettings");
    await page.waitForSelector("#modalSettings:not(.hidden)");
    const roots = await page.locator("#setRoots").inputValue();
    step("settings load current config", roots.includes(fx.tree), `roots: ${roots}`);
    const fuzzyMiB = await page.locator("#setFuzzyMiB").inputValue();
    step("fuzzy byte budget shown in MiB", fuzzyMiB === "16", `${fuzzyMiB} MiB`);
    await shot("11-settings");
    // Change it before saving: a field that only ever round-trips its default
    // would pass even if the save path dropped it entirely.
    await page.locator("#setFuzzyMiB").fill("32");
    await page.click("#btnSaveSettings");
    await page.waitForSelector("#modalSettings.hidden", { state: "attached" });
    const saved = await (await fetch(`${BASE}/api/config`)).json();
    step("settings save round-trips", saved.roots_allowlist.includes(fx.tree),
      `delete_mode=${saved.delete_mode}`);
    step("fuzzy byte budget persists as bytes", saved.fuzzy_max_bytes === 33554432,
      `${saved.fuzzy_max_bytes} bytes`);

    // 10. Dark mode + narrow viewport -----------------------------------
    const dark = await browser.newContext({ colorScheme: "dark", viewport: { width: 1440, height: 900 } });
    const darkPage = await dark.newPage();
    await darkPage.goto(BASE, { waitUntil: "networkidle" });
    await darkPage.waitForSelector("tr.group-row");
    const bg = await darkPage.evaluate(() => getComputedStyle(document.body).backgroundColor);
    step("dark mode applies a dark background", bg !== "rgb(255, 255, 255)", bg);
    await darkPage.screenshot({ path: path.join(SHOTS, "12-dark.png"), fullPage: true });
    await dark.close();

    const narrow = await browser.newContext({ viewport: { width: 420, height: 900 } });
    const narrowPage = await narrow.newPage();
    await narrowPage.goto(BASE, { waitUntil: "networkidle" });
    await narrowPage.waitForSelector("tr.group-row");
    const overflow = await narrowPage.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    step("no horizontal overflow at 420px", overflow <= 0, `${overflow}px overflow`);
    await narrowPage.screenshot({ path: path.join(SHOTS, "13-narrow.png"), fullPage: true });
    await narrow.close();
  } catch (err) {
    // Capture the page as it was when the step failed, before tearing down.
    try { await shot("99-failure"); } catch { /* page may already be gone */ }
    try {
      fs.writeFileSync(path.join(SHOTS, "99-failure.html"), await page.content(), "utf8");
    } catch { /* ditto */ }
    throw err;
  } finally {
    await browser.close();
    server.kill();
    // The server holds the SQLite file open; wait for it to actually exit
    // before removing the fixture, or the cleanup masks the real failure.
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 5000);
      server.on("exit", () => { clearTimeout(timer); resolve(); });
    });
    const clean = results.every((r) => r.ok) && !pageErrors.length && !badResponses.length;
    if (clean) {
      try { fs.rmSync(fx.root, { recursive: true, force: true }); } catch { /* best effort */ }
    } else {
      console.log(`\nfixture kept for inspection: ${fx.root}`);
    }
  }
}

run().then(() => {
  const failed = results.filter((r) => !r.ok);
  console.log("\n" + "=".repeat(66));
  console.log(`steps      : ${results.length - failed.length}/${results.length} passed`);
  console.log(`page errors: ${pageErrors.length}`);
  pageErrors.forEach((e) => console.log(`   ${e}`));
  console.log(`bad HTTP   : ${badResponses.length}`);
  badResponses.forEach((e) => console.log(`   ${e}`));
  console.log(`screenshots: ${SHOTS}`);
  console.log("=".repeat(66));
  process.exit(failed.length || pageErrors.length || badResponses.length ? 1 : 0);
}).catch((err) => {
  console.error("\nharness error:", err);
  process.exit(2);
});
