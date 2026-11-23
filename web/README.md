# Igneous web

The browser app: the 3D graph explorer (`/`), Ask (`/ask`), Search (`/search`) and the Sources page
(`/settings`, where every source is connected and the graph is built). It talks to the API at `NEXT_PUBLIC_API_URL`
(default `http://localhost:8000`), which Next.js inlines when it compiles, so restart `npm run dev` or rebuild after
changing it (`web/.env.example`).

```bash
npm install && npm run dev    # http://localhost:3000, with the API running (see the root README)
```

This is Next.js 16 with React 19 and Tailwind CSS 4. Its APIs differ from older versions: the guides in
`node_modules/next/dist/docs/` describe this version; read the relevant one before changing Next-specific code.

## Layout

- `src/app/`: one folder per route, the root layout (nav, theme script) and `globals.css`. Each route's `page.tsx` is a
  Server Component that sets the page's title and renders its heading; the page's own client component lives in
  `src/components/` (`Explorer`, `AskPage`, `SearchPage`, `SourcesPage`). Search and Sources read their URL with
  `useSearchParams`, inside a `Suspense` boundary.
- `src/components/Explorer.tsx`: the Graph page. `Graph3D.tsx` draws it with 3d-force-graph (WebGL, so it is only
  ever loaded in the browser); `Inspector`, `EntityInspector`, `WhyConnected` and `Suggestions` fill the details panel.
- `src/lib/api.ts`: every call to the API, with the exact shapes it answers.
- `src/lib/sources.ts`: `GET /sources` as every page shares it, fetched once per page load: what a record from each
  stored source is called (the one label map, from the names the Sources page uses) and each source's entry.
- `src/lib/graph-style.ts`: every color drawn outside CSS (section hues, the theme's colors for the graph) and the words
  for node and link types and for who added a link. `dates.ts` formats dates, `theme.ts` holds the light/dark theme,
  `hooks.ts` the request hooks that let only the latest reply land, `corrections.ts` what a panel hands the page after
  a correction.

## How the explorer behaves

- The URL names the centre (`/?seed=<node id>`; your own graph is `/`), so a reload or a shared link opens the same
  graph.
- Next keeps a page alive while you use the other tabs and runs its effects again when you return. The explorer
  loads only on its first mount, so coming back shows the graph as you left it, unless the URL names another centre
  (Search's "People involved" links a person to `/?seed=`). The Sources page loads again each time it is shown.
- What is on screen is the centre's view plus layers: each expansion, each node opened from the panel, each link you
  made. Collapsing a node removes what it brought in. When the API's graph version changes, and after every
  correction, the view and each layer are fetched again in place: the selection, the panel and positions stay.
- One WebGL renderer lives as long as the Graph page; it is released (with its WebGL context) when the page unmounts
  or is hidden.

### Corrections

Every correction answers when it applies (`api/igneous/graph/entity_api.py`): now; when the rebuild that is running
publishes; or with the next rebuild, when it couldn't apply now and none is running. One that applies now updates the
graph at once. For one that applies later the notice says when and stays until you dismiss it; while a rebuild runs,
the page checks `GET /graph/version` every 3 s instead of every 30 s, and says whether the rebuild published or
stopped without publishing (then the correction waits for the next one).

An answer names the node pairs it touched (`pairs`, each end a person's identity head) and whether a link the graph
draws joins each once it applies, which updates exactly those links on screen: a pair no link joins any more loses its
links, including a node opened from the panel that hung off the other end; a pair joined again gets them back. For a
correction that waits for a rebuild, the outcome is known only once it publishes: its pairs are taken apart then, and
the refresh that follows joins again each pair the API draws a link between. A link you added is drawn hanging off the
node whose panel you added it from (with the other end, if it wasn't on the graph), even between a pair you rejected
inferred links for, and goes when you remove it or undo it.

- "This is wrong" on an inferred link, with Undo in the notice; "Undo removal" in the Why panel undoes it later.
- "+ Connect to…" offers the ways the API allows two things to connect, never "same person". A link you or an agent
  added shows as yours (or as added by the agent) in the Why panel, with Remove; Undo brings it back.
- "Same person as…" merges two people after you confirm. A person's panel lists the other addresses, numbers and
  profiles merged into them ("Also known as"), each with "Not the same person".
- A project's panel lists your rename, your hide and what you merged into it, each with its own undo (Undo rename,
  Unhide, Separate), for as long as the correction stands.
- "Links to check" lists the links the verifier was unsure about (`GET /graph/suggestions`), with how many there are
  in all; "Right" and "Wrong" are labels it learns from.

## Theme and styles

The theme is `data-theme` on `<html>`, set by a script in `<head>` before the first paint (your choice, or the OS
setting until you choose). Colors are tokens in `globals.css` (light under `:root`, dark under
`[data-theme="dark"]`); the 3D graph reads them at runtime. Shared button and field styles are Tailwind utilities
defined there (`btn`, `btn-primary`, `btn-chip`, `btn-close`, `btn-link`, `btn-option`, `field`).

## Checks

`make check` at the repo root runs them with the API's; CI runs the same.

```bash
npx next typegen && npx tsc --noEmit && npm run lint && npm run build
```

Two browser checks drive the app in headless Chromium, with the API and the web app running. Both fail on any console
error or warning. Headless Chromium draws WebGL in software and logs its own "GPU stall due to ReadPixels" driver
notes; those are the only console warnings they ignore. Install the browser once:

```bash
npx playwright install chromium               # the browser build this Playwright version drives
```

### The explorer walkthrough

`npm run walkthrough` drives the Graph page the way a person would: search, collapse and expand, Back, a correction
undone from the notice and from the panel, the Reader (Escape closes only it), another tab and back, finding you by
name, and twenty changes of centre. A WebGL context warning fails it too. It runs against the Enron demo database
(`make demo`):

```bash
WEB_URL=http://localhost:3000 npm run walkthrough
```

The correction it makes is undone before it moves on, so the database ends as it started.
The names it searches for come from the demo mailbox (`WALK_PEOPLE`, `WALK_PAIR` and `WALK_SEEDS` override them); you
are whoever the graph opens on, found by your name (Jump to marks you "You").
On the synthetic persona (`make persona`), name its people:

```bash
WALK_PEOPLE="Dana Kimura|Priya Raman" WALK_PAIR="Owen Brandt|Mira Castellanos" \
  WALK_SEEDS="Dana Kimura|Priya Raman|Owen Brandt|Rafael Ortiz|Sofia Lindgren" npm run walkthrough
```

There the pair is Owen Brandt and Mira, the owner, linked by the introduction Ruth Adeyemi made: an inferred link the
correction can remove.

### The corrections check

`npm run corrections` (`scripts/corrections.mjs`) drives every kind of correction against the API's answers, then
the other pages:

1. A correction while a rebuild runs: its notice says it applies when the rebuild publishes, the link stays until
   then and is gone once it publishes; then it is undone from the Why panel.
2. Links you added: "+ Connect to…" offers no "same person"; someone not on the graph joins it with the link, leaves
   with Undo and joins again when connected once more; a link to a top connection shows as yours in the Why panel, is
   removed there and brought back with Undo.
3. "Same person as…" asks before it merges.
4. A person split, "Not the same person", and its Undo.
5. A project renamed and hidden, each undone from its panel.
6. A suggestion answered under "Links to check".
7. Ask, Search and Sources: one title each, the labelled question and the state Ask is in, the search in the URL
   (a new search, then Back), a person Search names opened on the Graph page kept from before, every labelled section
   on the Sources page, and the Sources page loaded again when it is shown again.

It needs `psql` and the database the API reads, which it writes to: it holds the rebuild before it derives (the
deriving lock, as another rebuild would) so the correction lands while the rebuild runs, and, since there are no
verdicts without the model, it marks one inferred link "unsure" itself. Whatever step it stops at, it leaves the
database as it found it: it lets go of the rebuild and waits for it, undoes every correction its clicks made (by the
override id each answer carries, newest first) and puts back the verdict it marked. The rebuild is real and derives
the graph again, as any rebuild does, so run the check on a copy, with the API reading the copy. It prints counts and
the names you give it, never a name or address it finds. On a copy of the synthetic persona:

```bash
createdb -h localhost -U igneous igneous_check
pg_dump -h localhost -U igneous igneous_persona | psql -q -h localhost -U igneous igneous_check
# the API on the copy: IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/igneous_check
DATABASE_URL=postgresql://igneous:igneous@localhost:5432/igneous_check API_URL=http://localhost:8000 \
  WEB_URL=http://localhost:3000 npm run corrections
dropdb -h localhost -U igneous igneous_check
```

On a copy of the Enron demo, name its people (it has no project of its own, so that step is skipped):

```bash
CHECK_PAIR="Mery L Brown|Pallen" CHECK_LINK="Ina Rangel|Gary Taylor|Keith Holst" CHECK_SPLIT="Jane M. Tholt" \
  CHECK_PROJECT="" CHECK_QUERY=gas DATABASE_URL=postgresql://igneous:igneous@localhost:5432/igneous_check npm run corrections
```

`CHECK_PAIR` is a person and a top connection joined by an inferred link; `CHECK_LINK` a person, one of their top
connections and someone not on their graph; `CHECK_SPLIT` a person with another address merged into them.
