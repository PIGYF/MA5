# MA5 Frontend

React/Vite strategy workspace for the US and A-share scanners, watchlists, and backtests.

## Interaction model

- Scanner results support local sorting and secondary filtering without downloading prices again.
- Layout sizes, forms, selected symbols, result filters, and chart controls are stored under versioned `ma5.ui.v1.*` browser keys.
- Candidate charts share one control set for periods, MA5/MA20, volume, KDJ, signals, and strategy lines.
- Backtest frames render report-only documents; React remains the single owner of input forms and navigation.

## Checks

```bash
pnpm test
pnpm build
```

The deployment workflow runs both commands and verifies that the committed `dist` assets match the source build.

This directory is the first React/Vite migration layer for the MA5 web app.

- Local dev server: `pnpm dev`
- Production build: `pnpm build`
- Served by Python backend at `/app/`

The Python backend still owns strategy calculation, data fetching, scans, reports,
and watchlist persistence. React reads the new `/api/*` endpoints and embeds the
existing chart/detail pages during the migration.
