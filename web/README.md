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

- `src/app/`: one folder per route, the root layout (nav, theme script) and `globals.css`.
- `src/components/Explorer.tsx`: the Graph page. `Graph3D.tsx` draws it with 3d-force-graph (WebGL, so it is only
  ever loaded in the browser); `Inspector`, `EntityInspector` and `WhyConnected` fill the details panel.
- `src/lib/api.ts`: every call to the API, with its types.
- `src/lib/graph-style.ts`: every color drawn outside CSS (section hues, the theme's colors for the graph).
  `dates.ts` formats dates, `theme.ts` holds the light/dark theme, `hooks.ts` the request hooks that let only the
  latest reply land.

## How the explorer behaves

- The URL names the centre (`/?seed=<node id>`; your own graph is `/`), so a reload or a shared link opens the same
  graph.
- Next keeps a page alive while you use the other tabs and runs its effects again when you return. The explorer
  loads only on its first mount, so coming back shows the graph as you left it.
- What is on screen is the centre's view plus layers: each expansion, each node opened from the panel, each link you
  made. Collapsing a node removes what it brought in. When the API's graph version changes, and after every
  correction, the view and each layer are fetched again in place: the selection, the panel and positions stay.
- One WebGL renderer lives as long as the Graph page; it is released (with its WebGL context) when the page unmounts
  or is hidden.

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

### The explorer walkthrough

`npm run walkthrough` drives the Graph page in headless Chromium the way a person would: search, collapse and expand,
Back, a correction undone from the notice and from the panel, the Reader (Escape closes only it), another tab and
back, and twenty changes of centre. It fails on any console error or warning, a WebGL context warning among them.
It needs the API and the web app running against the Enron demo database (`make demo`):

```bash
npx playwright install chromium               # once: the browser build this Playwright version drives
WEB_URL=http://localhost:3000 npm run walkthrough
```

The correction it makes is undone before it moves on, so the database ends as it started. Headless Chromium draws
WebGL in software and logs its own "GPU stall due to ReadPixels" driver notes; those are the only console warnings it
ignores.
The names it searches for come from the demo mailbox (`WALK_PEOPLE`, `WALK_PAIR` and `WALK_SEEDS` override them).
On the synthetic persona (`make persona`), name its people:

```bash
WALK_PEOPLE="Dana Kimura|Priya Raman" WALK_PAIR="Owen Brandt|Mira Castellanos" \
  WALK_SEEDS="Dana Kimura|Priya Raman|Owen Brandt|Rafael Ortiz|Sofia Lindgren" npm run walkthrough
```

Mira, the owner, is never searched for (search lists her as "You"); she is only the pair's other end, linked to Owen
Brandt by the introduction Ruth Adeyemi made, an inferred link the correction can remove.
