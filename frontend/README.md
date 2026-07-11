# MA5 Frontend

This directory is the first React/Vite migration layer for the MA5 web app.

- Local dev server: `pnpm dev`
- Production build: `pnpm build`
- Served by Python backend at `/app/`

The Python backend still owns strategy calculation, data fetching, scans, reports,
and watchlist persistence. React reads the new `/api/*` endpoints and embeds the
existing chart/detail pages during the migration.
