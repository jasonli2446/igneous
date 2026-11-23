// The corrections check: drives the Graph page's corrections the way a person would and checks each against the API's
// contract, then Ask, Search and Sources. It fails on any console error or warning.
//
//   1. A correction while a rebuild runs: the notice says it applies when the rebuild publishes, the link and what it
//      brought on the graph stay until then, and both are gone once it publishes; then it is undone from the panel.
//   2. Links you added: "+ Connect to…" never offers "same person"; someone not on the graph joins it with the link,
//      leaves with Undo and joins again when you connect them once more; a link to a top connection shows as yours in
//      the Why panel, is removed there and brought back with Undo.
//   3. "Same person as…" asks you to confirm (the check goes back without merging).
//   4. A person split: "Not the same person" on one of a person's other identities, then Undo.
//   5. A project renamed and hidden, each undone from its panel once the notice has gone.
//   6. A suggestion answered: the check marks one inferred link "unsure", as the verifier does when the records don't
//      settle it (there are no verdicts without the model), and answers it "Right" under "Links to check".
//   7. Ask, Search and Sources: one title each, the labelled question and the state Ask is in, the search in the URL
//      (a new search, then Back), a person Search names opened on the Graph page kept from before, every labelled
//      section on the Sources page, and the Sources page loaded again when it is shown again.
//
// It writes to the database the API reads, so run it on a copy of one (web/README.md shows how). Whatever happens, the
// check leaves the database as it found it: it lets go of the rebuild it holds and waits for it, undoes every
// correction its clicks made (each by the override id the API answered, newest first), and puts back the verdict it
// planted. The rebuild itself is real: it derives the graph again, as any rebuild does. The check prints counts and
// the names you give it, never a name or an address it finds. From web/, with the API and the web app running:
//   DATABASE_URL=postgresql://igneous:igneous@localhost:5432/<your copy> API_URL=http://localhost:8000 \
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
// For links you add: a person, one of their top connections (its Why is a button on their panel), and someone not on
// their graph (joining the graph with the link shows it drawn).
const [LINKER, LINKED, AWAY] = (process.env.CHECK_LINK ?? "Dana Kimura|Rafael Ortiz|Kevin Osei").split("|");
// A person with another address merged into them.
const SPLIT = process.env.CHECK_SPLIT ?? "Dana Kimura";
// A project, to rename and hide (none, CHECK_PROJECT="", skips that step: the Enron demo has no project of its own).
const PROJECT = process.env.CHECK_PROJECT ?? "Tidepool";
// A search with results, people among them.
const QUERY = process.env.CHECK_QUERY ?? "lisbon";
const TIMEOUT = 30_000;
const SOFTWARE_GL = /GL Driver Message \(OpenGL, Performance, /;

const problems = [];
let step = "start";
// What the check changed, for the clean-up: the corrections its clicks made, the rebuild it holds, the verdict it
// planted.
const made = new Set();
let release = null;
let rebuild = null;
let planted = null;

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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Run a step whose locators carry a name or an address the check found: a failure says what didn't happen, never
 *  what it was looking for. */
async function quietly(what, run) {
  try {
    return await run();
  } catch (e) {
    throw new Error(`${what} (${e.name === "TimeoutError" ? "timed out" : "failed"})`);
  }
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
    await browser.close();
    await cleanUp();
  }
}

/** Put the database back as the check found it, whatever step it stopped at. */
async function cleanUp() {
  const was = step;
  step = "clean-up";
  try {
    await release?.();
    if (rebuild) {
      const deadline = Date.now() + 300_000;
      while ((await apiJson(`/jobs/${rebuild}`)).status === "running") {
        check(Date.now() < deadline, "the rebuild the check started is still running");
        await sleep(500);
      }
    }
    // Newest first, each the way its notice's Undo would: through the API, which applies it now.
    const ids = [...made].sort((a, b) => b - a);
    const active = ids.length ? (await sql(`SELECT id FROM overrides WHERE undone_at IS NULL AND id = ANY(CAST(:'ids' AS bigint[]))
                                            ORDER BY id DESC`, { ids: `{${ids.join(",")}}` })).split("\n").filter(Boolean) : [];
    for (const id of active) await apiJson(`/overrides/${id}`, { method: "DELETE" });
    if (planted) await unplant();
    if (active.length) console.log(`ok  clean-up (undid ${active.length} correction${active.length === 1 ? "" : "s"} a failed step left)`);
  } catch (e) {
    problems.push(`[clean-up after "${was}"] ${e.message}`);
  } finally {
    step = was;
  }
}

/** The verdict the suggestion step planted (or the user's label that answered it) gives way to what was there. */
async function unplant() {
  const { edge, a, b, type, extractor, props, previous } = planted;
  await sql(`
    DELETE FROM edge_verdicts WHERE src_key = :'a' AND dst_key = :'b' AND type = :'t' AND extractor = :'x';
    INSERT INTO edge_verdicts SELECT * FROM jsonb_populate_record(NULL::edge_verdicts, CAST(:'previous' AS jsonb))
      WHERE CAST(:'previous' AS jsonb) <> 'null';
    UPDATE edges SET props = CAST(:'props' AS jsonb) WHERE id = :'edge';
    UPDATE graph_version SET version = version + 1;`,
  { a, b, t: type, x: extractor, previous: JSON.stringify(previous), props: JSON.stringify(props), edge });
  planted = null;
}

async function graph(page) {
  const search = page.getByRole("combobox", { name: "Search the graph" });
  const panel = page.getByRole("complementary", { name: "Details" });
  const notice = (text) => page.getByRole("status").filter({ hasText: text });
  const status = page.getByRole("status").filter({ hasText: /^Centred on / });

  /** How many things are on the graph, from the page's status line. */
  async function onGraph() {
    const m = ((await status.textContent({ timeout: TIMEOUT })) ?? "").match(/, (\d+) on the graph\.$/);
    check(m, "no status line");
    return Number(m[1]);
  }

  /** Search for a name and open the best match that carries exactly that name (you are listed as "You"). */
  async function open(name) {
    await search.fill(name);
    await page.getByRole("option", { name: new RegExp(`^${escape(name)} (\\d|You$)`) }).first().click({ timeout: TIMEOUT });
    await panel.getByRole("heading", { level: 2, name }).waitFor({ timeout: TIMEOUT });
  }

  // Every correction the page makes answers with the id that undoes it: the clean-up undoes each one still standing.
  page.on("response", async (r) => {
    if (!r.url().startsWith(API) || !["POST", "DELETE"].includes(r.request().method())) return;
    const id = (await r.json().catch(() => null))?.override_id;
    if (Number.isInteger(id)) made.add(id);
  });

  step = "open the graph";
  await page.goto(`${WEB}/`, { waitUntil: "networkidle" });
  await status.waitFor({ timeout: TIMEOUT });
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
  const before = await onGraph();
  release = await holdDeriving();
  ({ id: rebuild } = await apiJson("/pipeline/run", { method: "POST" }));
  await until(page, "the rebuild to wait to derive", async () => (await apiJson("/graph/version")).rebuilding &&
    Number(await sql(`SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted
                        AND database = (SELECT oid FROM pg_database WHERE datname = current_database())`)) > 0, 120_000);
  await wrong.first().click();
  await notice(/applies when it publishes/).waitFor({ timeout: TIMEOUT });
  await earlier.waitFor({ timeout: TIMEOUT });
  check(await wrong.count() === links, "the link left before the rebuild published");
  check(await onGraph() === before, "the graph changed before the rebuild published");
  await release();
  await notice(/^The rebuild published/).waitFor({ timeout: 120_000 });
  await until(page, "the link gone once the rebuild published", async () => (await wrong.count()) < links);
  // What the link brought on the graph leaves with it (the other end, when nothing else drew it).
  const published = await onGraph();
  check(published <= before, `${before} on the graph before, ${published} once it published`);
  const job = await until(page, "the rebuild to finish", async () => {
    const j = await apiJson(`/jobs/${rebuild}`);
    return j.status !== "running" && j;
  });
  check(job.status === "succeeded", `the rebuild ${job.status}: ${job.error}`);
  rebuild = null;
  await panel.getByRole("button", { name: "Undo removal" }).click({ timeout: TIMEOUT });
  await until(page, "the correction undone from the panel", async () => (await earlier.count()) === 0 && (await wrong.count()) === links);
  pass(`"applies when it publishes" shown; ${links} -> ${links - 1}+ links and ${before} -> ${published} on the graph once it published; undone from the panel`);

  step = `links you added from ${LINKER}`;
  const since = await sql("SELECT now()");
  const left = async () => Number(await sql("SELECT count(*) FROM overrides WHERE created_at >= :'since'", { since }));
  await page.keyboard.press("Escape");
  await open(LINKER);
  const alone = await onGraph();
  let offered = [];
  /** "+ Connect to…" `other` from the open panel, the first kind of connection offered. */
  async function connect(other) {
    await panel.getByRole("button", { name: "+ Connect to…" }).click();
    await panel.getByRole("textbox", { name: "Search for the thing to connect" }).fill(other);
    await panel.getByRole("list", { name: "Things to connect to" }).getByRole("button", { name: new RegExp(`^${escape(other)}`) })
      .first().click({ timeout: TIMEOUT });
    const kinds = panel.getByRole("radiogroup", { name: "Kind of connection" }).getByRole("radio");
    await kinds.first().waitFor({ timeout: TIMEOUT });
    offered = await kinds.allTextContents();
    check(!offered.some((k) => /same person/i.test(k)), `"+ Connect to…" offered ${offered.join(", ")}`);
    await panel.getByRole("button", { name: "Connect", exact: true }).click();
    await notice(`Connected to ${other}.`).waitFor({ timeout: TIMEOUT });
  }
  // Someone not on the graph joins it with the link, leaves with its Undo, and joins again when you connect them once
  // more: the pair the Undo took apart is joined by the new link.
  for (const round of ["added", "added again"]) {
    await connect(AWAY);
    await until(page, `${AWAY} on the graph, ${round}`, async () => (await onGraph()) === alone + 1);
    await notice(`Connected to ${AWAY}.`).getByRole("button", { name: "Undo" }).click();
    await notice("Undone.").waitFor({ timeout: TIMEOUT });
    await until(page, `${AWAY} off the graph again`, async () => (await onGraph()) === alone);
    check(await left() === 0, "undoing the link you added left a correction behind");
  }
  pass(`offered ${offered.join(", ")}; ${AWAY} ${alone} -> ${alone + 1} on the graph each time it was connected, and back with Undo`);

  step = `a link you added to ${LINKED}, removed from the Why panel and brought back`;
  await connect(LINKED);
  await panel.getByRole("button", { name: `Why is ${LINKED} connected?` }).click({ timeout: TIMEOUT });
  const remove = panel.getByRole("button", { name: "Remove", exact: true });
  await remove.waitFor({ timeout: TIMEOUT });
  await panel.getByText("Yours", { exact: true }).waitFor({ timeout: TIMEOUT });
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
  pass("shown as yours, removed, brought back with Undo, undone");

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
  const known = panel.getByRole("button", { name: /^Not the same person: / });
  await known.first().waitFor({ timeout: TIMEOUT });
  const identities = await known.count();
  const label = (await known.first().getAttribute("aria-label")).slice("Not the same person: ".length);
  const row = panel.getByRole("button", { name: `Not the same person: ${label}`, exact: true });
  const splitNotice = notice(`${label} is no longer ${SPLIT}.`);
  await quietly("separate the identity", async () => {
    await row.click();
    await panel.getByRole("group", { name: `Separate ${label}`, exact: true }).getByRole("button", { name: "Separate them" }).click();
    await splitNotice.waitFor({ timeout: TIMEOUT });
  });
  // It leaves the list, with whatever was joined to the person only through it.
  await until(page, "the identity split off", async () => (await row.count()) === 0);
  const kept = await known.count();
  await quietly("undo the split from its notice", () => splitNotice.getByRole("button", { name: "Undo" }).click());
  await until(page, "the identity back", async () => (await row.count()) === 1);
  await until(page, "the split's correction forgotten once undone", async () => (await left()) === 0);
  pass(`one of ${identities} other identities split off (${identities - kept} left the list) and joined again`);

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
  // An inferred link between two people or projects that nobody labelled yourself, marked "unsure" as the verifier
  // marks one it can't settle (most confident first: the page lists it among the first). Any verdict it had comes back.
  const candidates = JSON.parse(await sql(`
    SELECT COALESCE(json_agg(c), '[]') FROM (
      SELECT e.id AS edge, a.type || ':' || a.key AS a, b.type || ':' || b.key AS b, e.type, e.extractor,
             COALESCE(e.props, '{}') AS props, to_jsonb(v) AS previous
      FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
      LEFT JOIN edge_verdicts v ON v.src_key = a.type || ':' || a.key AND v.dst_key = b.type || ':' || b.key
        AND v.type = e.type AND v.extractor = e.extractor
      WHERE e.extractor NOT IN ('spine', 'user', 'profile', 'contacts', 'self', 'github', 'linkedin')
        AND e.extractor NOT LIKE 'agent:%' AND e.type <> 'SAME_AS'
        AND a.type IN ('Person', 'Project') AND b.type IN ('Person', 'Project')
        AND COALESCE(v.source, '') <> 'user' AND COALESCE(v.verdict, '') <> 'unsure'
      ORDER BY (v IS NULL) DESC, (e.type = 'INTRODUCED') DESC, e.id DESC LIMIT 20) c`));
  let listed = -1;
  for (const c of candidates) {
    planted = c;
    await sql(`INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, reason, confidence, source)
               VALUES (:'a', :'b', :'t', :'x', 'web-check', 'unsure', 'The records don''t settle it.', 1.0, 'model')
               ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET verdict = 'unsure', evidence = 'web-check',
                 reason = EXCLUDED.reason, confidence = 1.0, source = 'model'`,
    { a: c.a, b: c.b, t: c.type, x: c.extractor });
    listed = (await apiJson("/graph/suggestions")).suggestions.findIndex((s) => s.edge === c.edge);
    if (listed >= 0) break;
    await unplant(); // a hidden end, or one person under two addresses: the page never asks about it
  }
  check(listed >= 0, `none of ${candidates.length} inferred links between two people or projects can be asked about`);
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("button", { name: /^Links to check/ }).click({ timeout: TIMEOUT });
  await panel.getByRole("heading", { level: 2, name: "Links to check" }).waitFor({ timeout: TIMEOUT });
  // The page lists them in the API's order: the planted one is at its place.
  const asked = panel.getByRole("listitem").filter({ has: page.getByRole("button", { name: /^Right: / }) });
  await asked.first().waitFor({ timeout: TIMEOUT });
  const claim = await asked.nth(listed).getAttribute("aria-label");
  const before6 = (await apiJson("/graph/suggestions")).total;
  const same = panel.getByRole("listitem", { name: claim, exact: true });
  const twins = await same.count();
  await quietly("answer the planted link Right", async () => {
    await asked.nth(listed).getByRole("button", { name: `Right: ${claim}`, exact: true }).click();
    await notice(`Kept: ${claim}.`).waitFor({ timeout: TIMEOUT });
  });
  await until(page, "the answered link gone from the list", async () => (await same.count()) === twins - 1);
  const label_ = await sql(`SELECT verdict || ' ' || source FROM edge_verdicts
                            WHERE src_key = :'a' AND dst_key = :'b' AND type = :'t' AND extractor = :'x'`,
  { a: planted.a, b: planted.b, t: planted.type, x: planted.extractor });
  check(label_ === "keep user", `the answer is stored as "${label_}", not your keep`);
  const after6 = await apiJson("/graph/suggestions");
  check(!after6.suggestions.some((s) => s.edge === planted.edge), "the answered suggestion is still listed");
  check(after6.total === before6 - 1, `${before6} links to check before the answer, ${after6.total} after`);
  await unplant();
  pass(`the planted link kept and stored as your label; ${before6} -> ${after6.total} to check`);
}

async function pages(page) {
  step = "Ask";
  await page.goto(`${WEB}/ask`, { waitUntil: "networkidle" });
  check(await page.title() === "Ask · Igneous", `page title is "${await page.title()}"`);
  check(await page.getByRole("heading", { level: 1 }).count() === 1, "not one h1");
  await page.getByLabel("Your question").fill(`What did I plan with ${LINKER}?`);
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

  step = "a person Search names, opened on the Graph page kept from before";
  const nav = page.getByRole("navigation");
  const status = page.getByRole("status").filter({ hasText: /^Centred on / });
  await page.goto(`${WEB}/`, { waitUntil: "networkidle" });
  const centre = ((await status.textContent({ timeout: TIMEOUT })) ?? "").match(/^Centred on (.+), \d+ on the graph\.$/)?.[1];
  await nav.getByRole("link", { name: "Search", exact: true }).click();
  await page.getByRole("searchbox", { name: "Search for" }).fill(QUERY);
  await page.getByRole("searchbox", { name: "Search for" }).press("Enter");
  const people = page.getByRole("region", { name: "People involved" }).getByRole("link");
  await people.first().waitFor({ timeout: TIMEOUT });
  const names = await people.allTextContents();
  const pick = names.findIndex((n) => n !== centre);
  check(pick >= 0, "Search names nobody but the person the graph is on");
  await people.nth(pick).click();
  await until(page, "the Graph page centred on the person Search named", async () =>
    (await status.textContent()).startsWith(`Centred on ${names[pick]},`) && new URL(page.url()).searchParams.has("seed"));
  pass(`centred on the ${ordinal(pick + 1)} person Search named`);

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
  // Shown again from another tab, it loads the sources and jobs again.
  let loads = 0;
  page.on("request", (r) => { if (new URL(r.url()).pathname === "/jobs" && r.url().startsWith(API)) loads += 1; });
  await nav.getByRole("link", { name: "Ask", exact: true }).click();
  await page.getByRole("heading", { level: 1, name: "Ask" }).waitFor({ timeout: TIMEOUT });
  await nav.getByRole("link", { name: "Sources", exact: true }).click();
  await until(page, "the Sources page loaded again when shown again", async () => loads > 0);
  pass(`${groups} labelled sections; loaded again when shown again`);
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

function ordinal(n) {
  return ["first", "second", "third"][n - 1] ?? `#${n}`;
}

function escape(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

main().then(
  () => {
    for (const p of problems) console.error(p);
    if (problems.length) {
      console.error(`FAIL: ${problems.length} console error(s) or warning(s), or a clean-up that didn't finish`);
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
