// The corrections check: drives the Graph page's corrections the way a person would and checks each against the API's
// contract, then Ask, Search and Sources. It fails on any console error or warning.
//
//   1. A correction while a rebuild runs: the notice says it applies when the rebuild publishes, the link stays until
//      then, and it is gone once the rebuild publishes; then it is undone from the panel.
//   2. A link you added: "+ Connect to…" never offers "same person"; the link shows as yours with Remove, whose Undo
//      brings it back; then it is taken out again.
//   3. "Same person as…" asks you to confirm (the check goes back without merging).
//   4. A person split: "Not the same person" on one of a person's other identities, then Undo.
//   5. A project renamed and hidden, each undone from its panel once the notice has gone.
//   6. A suggestion answered: the check marks one inferred link "unsure", as the verifier does when the records don't
//      settle it (there are no verdicts without the model), and answers it "Right" under "Links to check".
//   7. Ask, Search and Sources: one title each, a labelled question, the search in the URL, every labelled section.
//
// Needs the API and the web app running against a database you can write to, and psql: the check holds the rebuild
// before it derives (the deriving lock, as another rebuild would) so the correction lands while it runs, and marks the
// suggestion directly in the database. Everything it changes it undoes, so the database ends as it started. From web/:
//   DATABASE_URL=postgresql://igneous:igneous@localhost:5432/igneous_persona API_URL=http://localhost:8000 \
//     WEB_URL=http://localhost:3000 npm run corrections
// The names come from the synthetic persona (make persona); CHECK_PAIR, CHECK_LINK, CHECK_SPLIT, CHECK_PROJECT and
// CHECK_QUERY override them.

import { spawn } from "node:child_process";
import { chromium } from "playwright";

const WEB = (process.env.WEB_URL ?? "http://localhost:3000").replace(/\/$/, "");
const API = (process.env.API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const DATABASE = process.env.DATABASE_URL?.replace("postgresql+psycopg://", "postgresql://");
// A person and a top connection of theirs joined by an inferred link (Ruth Adeyemi introduced them).
const [PERSON, OTHER] = (process.env.CHECK_PAIR ?? "Owen Brandt|Mira Castellanos").split("|");
// A person and one of their top connections, for a link you add.
const [LINKER, LINKED] = (process.env.CHECK_LINK ?? "Dana Kimura|Rafael Ortiz").split("|");
// A person with another address merged into them.
const SPLIT = process.env.CHECK_SPLIT ?? "Dana Kimura";
// A project, to rename and hide (none, CHECK_PROJECT="", skips that step: the Enron demo has no project of its own).
const PROJECT = process.env.CHECK_PROJECT ?? "Tidepool";
// A search with results.
const QUERY = process.env.CHECK_QUERY ?? "lisbon";
const TIMEOUT = 30_000;
const SOFTWARE_GL = /GL Driver Message \(OpenGL, Performance, /;

const problems = [];
const cleanups = [];
let step = "start";

function check(ok, what) {
  if (!ok) throw new Error(what);
}

/** Run SQL in the database (on stdin, so psql variables `:'name'` work), and return what it prints. */
function sql(query, vars = {}) {
  return new Promise((resolve, reject) => {
    const args = [DATABASE, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
      ...Object.entries(vars).flatMap(([k, v]) => ["-v", `${k}=${v}`])];
    const psql = spawn("psql", args, { stdio: ["pipe", "pipe", "pipe"] });
    let out = "", err = "";
    psql.stdout.on("data", (d) => { out += d; });
    psql.stderr.on("data", (d) => { err += d; });
    psql.on("error", reject);
    psql.on("close", (code) => (code === 0 ? resolve(out.trim()) : reject(new Error(err.trim() || `psql exited ${code}`))));
    psql.stdin.end(query);
  });
}

/** A session that holds the deriving lock, as a rebuild deriving does: another rebuild reads its sources, then waits
 *  to derive until it is released. Returns the release. */
async function holdDeriving() {
  const psql = spawn("psql", [DATABASE, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"], { stdio: ["pipe", "pipe", "pipe"] });
  await new Promise((resolve, reject) => {
    psql.stdout.once("data", resolve);
    psql.once("exit", (code) => reject(new Error(`psql exited ${code} before it held the lock`)));
    psql.stdin.write("SELECT 'held' FROM (SELECT pg_advisory_lock(hashtext('igneous:deriving'))) x;\n");
  });
  let released = null;
  return () => (released ??= new Promise((resolve) => {
    psql.removeAllListeners("exit");
    psql.once("exit", resolve);
    psql.stdin.end();
  }));
}

async function apiJson(path, init) {
  const r = await fetch(`${API}${path}`, init);
  check(r.ok, `${init?.method ?? "GET"} ${path} answered ${r.status}`);
  return r.json();
}

async function main() {
  check(DATABASE, "set DATABASE_URL to the database the API reads (the check writes to it and undoes what it writes)");
  await sql("SELECT 1");
  const browser = await chromium.launch();
  try {
    const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
    listen(page);
    await graph(page);
    await pages(page);
  } finally {
    for (const undo of cleanups.reverse()) await undo().catch((e) => problems.push(`[cleanup] ${e.message}`));
    await browser.close();
  }
}

async function graph(page) {
  const search = page.getByRole("combobox", { name: "Search the graph" });
  const panel = page.getByRole("complementary", { name: "Details" });
  const notice = (text) => page.getByRole("status").filter({ hasText: text });

  /** Search for a name and open the best match that carries exactly that name (you are listed as "You"). */
  async function open(name) {
    await search.fill(name);
    await page.getByRole("option", { name: new RegExp(`^${escape(name)} (\\d|You$)`) }).first().click({ timeout: TIMEOUT });
    await panel.getByRole("heading", { level: 2, name }).waitFor({ timeout: TIMEOUT });
  }

  step = "open the graph";
  await page.goto(`${WEB}/`, { waitUntil: "networkidle" });
  await page.getByRole("status").filter({ hasText: /^Centred on / }).waitFor({ timeout: TIMEOUT });
  check(await page.title() === "Graph · Igneous", `page title is "${await page.title()}"`);
  pass();

  step = `a correction between ${PERSON} and ${OTHER} while a rebuild runs`;
  await open(PERSON);
  await panel.getByRole("button", { name: `Why is ${OTHER} connected?` }).click({ timeout: TIMEOUT });
  await panel.getByRole("heading", { level: 2, name: `Why ${PERSON} and ${OTHER} are connected` }).waitFor({ timeout: TIMEOUT });
  const wrong = panel.getByRole("button", { name: "This is wrong" });
  const earlier = panel.getByRole("region", { name: "Earlier corrections" });
  check(await earlier.count() === 0, "the pair already has a correction; undo it on the page first");
  await wrong.first().waitFor({ timeout: TIMEOUT });
  const links = await wrong.count();
  const release = await holdDeriving();
  cleanups.push(release);
  const { id: rebuild } = await apiJson("/pipeline/run", { method: "POST" });
  await until(page, "the rebuild to wait to derive", async () => Number(await sql(`
    SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())`)) > 0, 120_000);
  await wrong.first().click();
  await notice(/applies when it publishes/).waitFor({ timeout: TIMEOUT });
  await earlier.waitFor({ timeout: TIMEOUT });
  check(await wrong.count() === links, "the link left before the rebuild published");
  await release();
  await notice(/^The rebuild published/).waitFor({ timeout: 120_000 });
  await until(page, "the link gone once the rebuild published", async () => (await wrong.count()) < links);
  const job = await until(page, "the rebuild to finish", async () => {
    const j = await apiJson(`/jobs/${rebuild}`);
    return j.status !== "running" && j;
  });
  check(job.status === "succeeded", `the rebuild ${job.status}: ${job.error}`);
  await panel.getByRole("button", { name: "Undo removal" }).click({ timeout: TIMEOUT });
  await until(page, "the correction undone from the panel", async () => (await earlier.count()) === 0 && (await wrong.count()) === links);
  pass(`"applies when it publishes" shown, ${links} -> ${links - 1}+ removed on publish, undone from the panel`);

  step = `a link you added between ${LINKER} and ${LINKED}`;
  const since = await sql("SELECT now()");
  const left = async () => Number(await sql("SELECT count(*) FROM overrides WHERE created_at >= :'since'", { since }));
  await page.keyboard.press("Escape");
  await open(LINKER);
  let offered = [];
  async function connect() {
    await panel.getByRole("button", { name: "+ Connect to…" }).click();
    await panel.getByRole("textbox", { name: "Search for the thing to connect" }).fill(LINKED);
    await panel.getByRole("list", { name: "Things to connect to" }).getByRole("button", { name: new RegExp(`^${escape(LINKED)}`) })
      .first().click({ timeout: TIMEOUT });
    const kinds = panel.getByRole("radiogroup", { name: "Kind of connection" }).getByRole("radio");
    await kinds.first().waitFor({ timeout: TIMEOUT });
    offered = await kinds.allTextContents();
    check(!offered.some((k) => /same person/i.test(k)), `"+ Connect to…" offered ${offered.join(", ")}`);
    await panel.getByRole("button", { name: "Connect", exact: true }).click();
    await notice(`Connected to ${LINKED}.`).waitFor({ timeout: TIMEOUT });
  }
  await connect();
  await notice(`Connected to ${LINKED}.`).getByRole("button", { name: "Undo" }).click();
  await notice("Undone.").waitFor({ timeout: TIMEOUT });
  check(await left() === 0, "undoing the link you added left a correction behind");
  await connect();
  await panel.getByRole("button", { name: `Why is ${LINKED} connected?` }).click({ timeout: TIMEOUT });
  const remove = panel.getByRole("button", { name: "Remove", exact: true });
  await remove.waitFor({ timeout: TIMEOUT });
  await remove.click();
  await notice("Removed the connection you added.").getByRole("button", { name: "Undo" }).click({ timeout: TIMEOUT });
  await notice("Undone.").waitFor({ timeout: TIMEOUT });
  await remove.waitFor({ timeout: TIMEOUT }); // back, and yours
  // Its notice has gone: forget the link the way that notice's Undo does, so nothing the check added stays.
  const [added] = (await sql("SELECT id FROM overrides WHERE op = 'link' AND undone_at IS NULL AND created_at >= :'since'",
    { since })).split("\n").filter(Boolean);
  check(added, "the link you added has no correction to undo");
  check((await apiJson(`/overrides/${added}`, { method: "DELETE" })).applied === "now", "undoing the link didn't apply");
  check(await left() === 0, "the link you added left a correction behind");
  pass(`offered ${offered.join(", ")}; added and undone, added, removed and brought back, undone`);

  step = "same person asks first";
  await page.keyboard.press("Escape");
  await open(LINKER);
  await panel.getByRole("button", { name: "Same person as…" }).click();
  await panel.getByRole("textbox", { name: "Search people to merge" }).fill(LINKED);
  await panel.getByRole("list", { name: "People to merge with" }).getByRole("button", { name: new RegExp(`^${escape(LINKED)}`) })
    .first().click({ timeout: TIMEOUT });
  const confirm = panel.getByRole("group", { name: "Confirm same person" });
  await confirm.getByRole("button", { name: "Yes, merge them" }).waitFor({ timeout: TIMEOUT });
  await confirm.getByRole("button", { name: "Back" }).click();
  pass();

  step = `a person split (${SPLIT})`;
  await page.keyboard.press("Escape");
  await open(SPLIT);
  const known = panel.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Not the same person" }) });
  await known.first().waitFor({ timeout: TIMEOUT });
  const label = (await known.first().locator("span").first().textContent()).split(" · ")[0];
  const row = panel.getByRole("listitem").filter({ hasText: label });
  await row.getByRole("button", { name: "Not the same person" }).click();
  await row.getByRole("button", { name: "Separate them" }).click();
  await notice(`${label} is no longer ${SPLIT}.`).waitFor({ timeout: TIMEOUT });
  await until(page, `${label} split off`, async () => (await row.count()) === 0);
  await notice(`${label} is no longer ${SPLIT}.`).getByRole("button", { name: "Undo" }).click();
  await until(page, `${label} back in ${SPLIT}`, async () => (await row.count()) === 1);
  await until(page, "the split's correction forgotten once undone", async () => (await left()) === 0);
  pass(`${label} split off and joined again`);

  step = `a project's corrections undone from its panel (${PROJECT || "no project named"})`;
  if (PROJECT) await project();
  else pass("skipped");

  async function project() {
    await page.keyboard.press("Escape");
    await search.fill(PROJECT);
    await page.getByRole("option", { name: new RegExp(`^${escape(PROJECT)} Projects`) }).first().click({ timeout: TIMEOUT });
    const title = panel.getByRole("heading", { level: 2 }).first();
    await panel.getByRole("button", { name: "Rename" }).click({ timeout: TIMEOUT });
    const renamed = `${PROJECT} (renamed by the check)`;
    await panel.getByRole("textbox", { name: "Project name" }).fill(renamed);
    await panel.getByRole("textbox", { name: "Project name" }).press("Enter");
    await notice(`Renamed to "${renamed}".`).waitFor({ timeout: TIMEOUT });
    const yours = panel.getByRole("list", { name: "Your corrections" });
    await yours.getByRole("button", { name: "Undo rename" }).click({ timeout: TIMEOUT });
    await until(page, "the name back", async () => (await yours.count()) === 0 && (await title.textContent()) === `${PROJECT}Rename`);
    await panel.getByRole("button", { name: "Hide", exact: true }).click();
    await notice(`Hid "${PROJECT}".`).waitFor({ timeout: TIMEOUT });
    await yours.getByRole("button", { name: "Unhide" }).click({ timeout: TIMEOUT });
    await until(page, "the project shown again", async () => (await yours.count()) === 0);
    await until(page, "the project's corrections forgotten once undone", async () => (await left()) === 0);
    pass("renamed and named back, hidden and shown again");
  }

  step = "a suggestion answered";
  await page.keyboard.press("Escape");
  const planted = JSON.parse(await sql(`
    SELECT json_build_object('edge', e.id, 'a', a.type || ':' || a.key, 'b', b.type || ':' || b.key, 'type', e.type,
                             'extractor', e.extractor, 'props', COALESCE(e.props, '{}'))
    FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
    WHERE e.extractor NOT IN ('spine', 'user', 'profile', 'contacts', 'self', 'github', 'linkedin')
      AND e.type <> 'SAME_AS' AND a.type IN ('Person', 'Project') AND b.type IN ('Person', 'Project')
      AND (a.props->>'hidden') IS DISTINCT FROM 'true' AND (b.props->>'hidden') IS DISTINCT FROM 'true'
      AND (a.props->>'kind') IS DISTINCT FROM 'service' AND (b.props->>'kind') IS DISTINCT FROM 'service'
      AND NOT EXISTS (SELECT 1 FROM edge_verdicts v WHERE v.src_key = a.type || ':' || a.key
                      AND v.dst_key = b.type || ':' || b.key AND v.type = e.type AND v.extractor = e.extractor)
    ORDER BY (e.type = 'INTRODUCED') DESC, e.id DESC LIMIT 1`) || "null");
  check(planted, "no inferred link between two people or projects to ask about");
  const keys = { a: planted.a, b: planted.b, t: planted.type, x: planted.extractor };
  await sql(`INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, reason, confidence, source)
             VALUES (:'a', :'b', :'t', :'x', 'web-check', 'unsure', 'The records don''t settle it.', 0.5, 'model')`, keys);
  cleanups.push(() => sql(`
    DELETE FROM edge_verdicts WHERE src_key = :'a' AND dst_key = :'b' AND type = :'t' AND extractor = :'x';
    UPDATE edges SET props = CAST(:'props' AS jsonb) WHERE id = :'edge';
    UPDATE graph_version SET version = version + 1;`, { ...keys, props: JSON.stringify(planted.props), edge: planted.edge }));
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("button", { name: /^Links to check/ }).click({ timeout: TIMEOUT });
  await panel.getByRole("heading", { level: 2, name: "Links to check" }).waitFor({ timeout: TIMEOUT });
  const asked = panel.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Right" }) });
  check(await asked.count() === 1, `${await asked.count()} links to check, not the 1 marked`);
  const claim = await asked.first().getAttribute("aria-label");
  await asked.first().getByRole("button", { name: "Right" }).click();
  await notice(`Kept: ${claim}.`).waitFor({ timeout: TIMEOUT });
  await panel.getByText("Nothing to check right now.").waitFor({ timeout: TIMEOUT });
  const label_ = await sql(`SELECT verdict || ' ' || source FROM edge_verdicts
                            WHERE src_key = :'a' AND dst_key = :'b' AND type = :'t' AND extractor = :'x'`, keys);
  check(label_ === "keep user", `the answer is stored as "${label_}", not your keep`);
  check((await apiJson("/graph/suggestions")).suggestions.length === 0, "the suggestion is still listed");
  pass(`"${claim}" kept, stored as your label`);
}

async function pages(page) {
  step = "Ask";
  await page.goto(`${WEB}/ask`, { waitUntil: "networkidle" });
  check(await page.title() === "Ask · Igneous", `page title is "${await page.title()}"`);
  check(await page.getByRole("heading", { level: 1 }).count() === 1, "not one h1");
  await page.getByLabel("Your question").fill(`What did I plan with ${LINKED}?`);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const states = { "no sources opted in": page.getByText("Nothing to ask yet"),
    "model not set up": page.getByText("The hosted model isn't set up"), answer: page.getByText(/cited of \d+ evidence passages/),
    "model unavailable": page.getByText(/The hosted model can't be used right now|The model couldn't answer/) };
  const state = await until(page, "an answer or why there is none", async () => {
    for (const [name, locator] of Object.entries(states)) if (await locator.count()) return name;
    return false;
  });
  if (state === "no sources opted in" || state === "model not set up") {
    await page.getByRole("link", { name: /Choose what the model may read|Set up the hosted model/ }).click();
    await page.getByRole("heading", { name: "Hosted model" }).waitFor({ timeout: TIMEOUT });
    await until(page, "the model section in focus", () => page.evaluate(() => document.activeElement?.id === "model"));
  }
  pass(state);

  step = "Search";
  const q = encodeURIComponent(QUERY);
  await page.goto(`${WEB}/search?q=${q}`, { waitUntil: "networkidle" });
  check(await page.title() === "Search · Igneous", `page title is "${await page.title()}"`);
  check(await page.getByRole("heading", { level: 1 }).count() === 1, "not one h1");
  const results = page.getByRole("heading", { name: "Results" });
  await results.waitFor({ timeout: TIMEOUT });
  const box = page.getByRole("searchbox", { name: "Search for" });
  check(await box.inputValue() === QUERY, "the box doesn't show the query in the URL");
  await box.fill(LINKED);
  await box.press("Enter");
  await until(page, "the new search in the URL", async () => new URL(page.url()).searchParams.get("q") === LINKED);
  await page.goBack();
  await until(page, "Back to the first search", async () => (await box.inputValue()) === QUERY);
  await results.waitFor({ timeout: TIMEOUT });
  await page.getByRole("region", { name: "Results" }).getByRole("listitem").first().getByRole("button").first().click();
  await page.getByRole("dialog").waitFor({ timeout: TIMEOUT });
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "detached", timeout: TIMEOUT });
  pass(`?q=${QUERY}, ?q=${LINKED}, Back, the Reader`);

  step = "Sources";
  await page.goto(`${WEB}/settings`, { waitUntil: "networkidle" });
  check(await page.title() === "Sources · Igneous", `page title is "${await page.title()}"`);
  check(await page.getByRole("heading", { level: 1 }).count() === 1, "not one h1");
  await page.getByRole("heading", { name: "Hosted model" }).waitFor({ timeout: TIMEOUT });
  const broken = await page.evaluate(() => [...document.querySelectorAll("[aria-labelledby]")]
    .flatMap((el) => el.getAttribute("aria-labelledby").split(/\s+/))
    .filter((id) => !document.getElementById(id)));
  check(broken.length === 0, `aria-labelledby names no element: ${broken.join(", ")}`);
  const groups = await page.getByRole("region").count();
  pass(`${groups} labelled sections`);
}

async function until(page, what, test, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const got = await test().catch(() => false);
    if (got) return got;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await page.waitForTimeout(100);
  }
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
    console.log("PASS: the corrections check finished with no console errors or warnings");
  },
  (e) => {
    for (const p of problems) console.error(p);
    console.error(`FAIL at "${step}": ${e.message}`);
    process.exit(1);
  },
);
