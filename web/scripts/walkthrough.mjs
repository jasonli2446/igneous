// The explorer walkthrough: drives the Graph page in Chromium the way a person would (search, expand, collapse,
// Back, a correction and its undo, the Reader, another tab and back, twenty changes of centre) and checks what the
// page shows after each step. It fails on any console error or warning, a WebGL context warning among them.
//
// Needs the API and the web app running against the Enron demo database (make demo). Usage, from web/:
//   npm run walkthrough                              # the app at http://localhost:3000
//   WEB_URL=http://localhost:3011 npm run walkthrough
// The names it searches for come from the demo mailbox; WALK_PEOPLE, WALK_PAIR and WALK_SEEDS override them.
//
// The correction is undone before the walkthrough moves on, so the database ends as it started.

import { chromium } from "playwright";

const WEB = (process.env.WEB_URL ?? "http://localhost:3000").replace(/\/$/, "");
// Two people to centre on, then Back between them.
const [FIRST, SECOND] = (process.env.WALK_PEOPLE ?? "K Allen|Phillip K Allen").split("|");
// A person and a top connection of theirs joined by an inferred link, which "This is wrong" can remove.
const [PERSON, OTHER] = (process.env.WALK_PAIR ?? "Mery L Brown|Pallen").split("|");
// Centres to cycle through for the twenty changes.
const SEEDS = (process.env.WALK_SEEDS ?? "K Allen|Phillip K Allen|Ina Rangel|John Lavorato|Pallen").split("|");
const CHANGES = 20;
const TIMEOUT = 30_000;

// Headless Chromium draws WebGL in software (SwiftShader) and reports its own pixel read-backs as driver
// performance notes. They come from the browser's renderer, not from the page; every other warning fails.
const SOFTWARE_GL = /GL Driver Message \(OpenGL, Performance, /;

const problems = [];
let step = "start";

function check(ok, what) {
  if (!ok) throw new Error(what);
}

async function main() {
  const browser = await chromium.launch();
  try {
    await firstPaint(browser);
    await walk(browser);
  } finally {
    await browser.close();
  }
}

/** A dark-mode reader gets the dark theme before the first paint (no light flash). */
async function firstPaint(browser) {
  step = "theme before first paint";
  const context = await browser.newContext({ colorScheme: "dark" });
  await context.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      window.__walkTheme = document.documentElement.getAttribute("data-theme");
    });
  });
  const page = await context.newPage();
  listen(page);
  await page.goto(`${WEB}/`, { waitUntil: "domcontentloaded" });
  const theme = await page.waitForFunction(() => window.__walkTheme ?? null, null, { timeout: TIMEOUT });
  check(await theme.jsonValue() === "dark", "a dark-mode reader saw the light theme first");
  await context.close();
  pass();
}

async function walk(browser) {
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
  listen(page);
  const search = page.getByRole("combobox", { name: "Search the graph" });
  const panel = page.getByRole("complementary", { name: "Details" });
  const panelOpen = () => panel.evaluate((el) => !el.inert);
  const status = page.getByRole("status").filter({ hasText: /^Centred on / });

  /** The centre and how many things are on the graph, from the page's status line. */
  async function shown() {
    const text = (await status.textContent({ timeout: TIMEOUT })) ?? "";
    const m = text.match(/^Centred on (.+), (\d+) on the graph\.$/);
    check(m, `unexpected status line: "${text}"`);
    return { name: m[1], count: Number(m[2]) };
  }

  async function until(what, test) {
    const deadline = Date.now() + TIMEOUT;
    for (;;) {
      const got = await test().catch(() => false);
      if (got) return got;
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
      await page.waitForTimeout(100);
    }
  }

  /** Search for a name and open the best match that carries exactly that name. */
  async function centreOn(name) {
    await search.fill(name);
    await page.getByRole("option", { name: new RegExp(`^${escape(name)} \\d`) }).first().click({ timeout: TIMEOUT });
    await until(`the graph centred on ${name}`, async () => (await shown()).name === name);
    return shown();
  }

  step = "open the graph";
  await page.goto(`${WEB}/`, { waitUntil: "domcontentloaded" });
  check(await page.title() === "Graph · Igneous", `page title is "${await page.title()}"`);
  check(await page.getByRole("heading", { level: 1, name: "Graph" }).count() === 1, "no Graph heading");
  // Your own graph, or, when no source says which person is you (several people's mailboxes), the empty state.
  const empty = page.getByText("Nobody to start from yet", { exact: true });
  const opened = await until("your graph or the empty state", async () =>
    (await status.count()) ? "your graph" : (await empty.count()) ? "nobody to start from" : false);
  pass(opened);

  step = `search for ${FIRST}`;
  await centreOn(FIRST);
  // The page writes the centre into the URL just after it shows the new graph.
  const seed = await until("the URL to name the centre", async () => new URL(page.url()).searchParams.get("seed"));
  check(await panelOpen(), "the panel didn't open on the centre you searched for");
  // A centre you searched for opens with its own connections shown.
  await panel.getByRole("button", { name: "Hide their connections" }).waitFor({ timeout: TIMEOUT });
  const first = await shown();
  pass(`${first.count} on the graph, /?seed=${seed}`);

  step = "collapse";
  await panel.getByRole("button", { name: "Hide their connections" }).click();
  const folded = await until("fewer things on the graph", async () => {
    const s = await shown();
    return s.count < first.count && s;
  });
  pass(`${first.count} -> ${folded.count}`);

  step = "expand";
  await panel.getByRole("button", { name: "Show their connections on the graph" }).click();
  await until("the connections back on the graph", async () => (await shown()).count === first.count);
  pass(`${folded.count} -> ${first.count}`);

  step = "Back";
  await centreOn(SECOND);
  await page.getByRole("button", { name: `Back to ${FIRST}` }).click();
  await until(`the graph back on ${FIRST}`, async () => (await shown()).name === FIRST);
  await until("the URL to follow Back", async () => new URL(page.url()).searchParams.get("seed") === seed);
  pass();

  step = "reload";
  await page.reload({ waitUntil: "domcontentloaded" });
  await until(`the reloaded page centred on ${FIRST}`, async () => (await shown()).name === FIRST);
  // A development build exposes its renderer: the layout must spread out, not stay stacked where it started.
  await until("the layout to spread out", () => page.evaluate(() => {
    const g = window.__fg;
    return !g || g.graphData().nodes.some((n) => Math.hypot(n.x ?? 0, n.y ?? 0, n.z ?? 0) > 1);
  }));
  pass(`/?seed=${seed} opens ${FIRST} again`);

  step = `a correction between ${PERSON} and ${OTHER}`;
  await centreOn(PERSON);
  await panel.getByRole("button", { name: `Why is ${OTHER} connected?` }).click({ timeout: TIMEOUT });
  const why = panel.getByRole("heading", { level: 2, name: `Why ${PERSON} and ${OTHER} are connected` });
  await why.waitFor({ timeout: TIMEOUT });
  const corrections = panel.getByRole("region", { name: "Earlier corrections" });
  check(await corrections.count() === 0, "the pair already has a correction; undo it on the page first");
  await panel.getByRole("button", { name: "This is wrong" }).first().click({ timeout: TIMEOUT });
  const toast = page.getByRole("status").filter({ hasText: /^Removed \d+ inferred connection/ });
  await toast.waitFor({ timeout: TIMEOUT });
  await corrections.waitFor({ timeout: TIMEOUT });
  check(await panelOpen(), "the panel closed after the correction");
  await toast.getByRole("button", { name: "Undo" }).click();
  await until("the correction undone from the notice", async () => await corrections.count() === 0);
  check(await panelOpen() && await why.count() === 1, "undoing from the notice closed the panel");
  await panel.getByRole("button", { name: "This is wrong" }).first().click({ timeout: TIMEOUT });
  await panel.getByRole("button", { name: "Undo removal" }).click({ timeout: TIMEOUT });
  await until("the correction undone from the panel", async () => await corrections.count() === 0);
  check(await panelOpen() && await why.count() === 1, "undoing from the panel closed the panel");
  pass("removed, undone from the notice, removed again, undone from the panel");

  step = "the Reader";
  const view = panel.getByRole("button", { name: /^View / }).first();
  const viewName = await view.getAttribute("aria-label");
  await view.click();
  const reader = page.getByRole("dialog");
  await reader.waitFor({ timeout: TIMEOUT });
  check(await page.evaluate(() => !!document.activeElement?.closest("dialog[open]")), "focus didn't move into the Reader");
  await page.keyboard.press("Escape");
  await reader.waitFor({ state: "detached", timeout: TIMEOUT });
  check(await panelOpen(), "Escape in the Reader also closed the panel");
  check(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")) === viewName,
    "focus didn't return to the button that opened the Reader");
  await page.keyboard.press("Escape");
  await until("Escape to close the panel", async () => !(await panelOpen()));
  pass();

  step = "another tab and back";
  const before = await shown();
  const url = page.url();
  await page.getByRole("link", { name: "Ask" }).click();
  await page.getByRole("heading", { level: 1, name: "Ask" }).waitFor({ timeout: TIMEOUT });
  await page.getByRole("link", { name: "Graph" }).click();
  await until("the Graph page shown again", async () => (await shown()).name === before.name);
  const after = await shown();
  check(after.count === before.count, `the graph changed from ${before.count} to ${after.count} things`);
  await until("the URL to name the centre again", async () => page.url() === url);
  pass(`still centred on ${after.name}`);

  step = `${CHANGES} changes of centre`;
  for (let i = 0; i < CHANGES; i++) await centreOn(SEEDS[(i + 1) % SEEDS.length]);
  await page.waitForTimeout(2000); // a late context warning would arrive now
  pass();
}

function listen(page) {
  page.on("console", (m) => {
    if (!["error", "warning", "assert"].includes(m.type()) || SOFTWARE_GL.test(m.text())) return;
    problems.push(`[${step}] console ${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (e) => problems.push(`[${step}] uncaught: ${e.message}`));
}

function pass(detail) {
  console.log(`ok  ${step}${detail ? ` (${detail})` : ""}`);
}

function escape(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

main().then(
  () => {
    for (const p of problems) console.error(p);
    if (problems.length) {
      console.error(`FAIL: ${problems.length} console error(s) or warning(s)`);
      process.exit(1);
    }
    console.log("PASS: the walkthrough finished with no console errors or warnings");
  },
  (e) => {
    for (const p of problems) console.error(p);
    console.error(`FAIL at "${step}": ${e.message}`);
    process.exit(1);
  },
);
