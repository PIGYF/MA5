## [LRN-20260621-001] best_practice

**Logged**: 2026-06-21T00:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: frontend

### Summary
Project optimization should be split into web product development and stock trading strategy work.

### Details
The user wants future optimization to follow two tracks:

- Web product / UI / interaction direction: page structure, dashboard workflow, chart usability, cache controls, deployment usability, responsive behavior.
- Stock selection / trading strategy direction: signal semantics, backtest assumptions, A-share and US-stock screening logic, TradingView parity, risk and execution rules.

Skill usage should follow this split. Use `ui-design` and browser/web testing skills for web-facing work. Use `stock-market-pro` and strategy/backtest code review for stock research and trading logic work. Do not mix UI polish with strategy semantic changes unless the user explicitly asks for both.

### Suggested Action
Before starting a meaningful optimization task, classify it as web/product, strategy/trading, or both. State the classification briefly, choose the matching skill/tooling, and verify with the appropriate checks.

### Metadata
- Source: user_feedback
- Related Files: AGENTS.md
- Tags: workflow, skills, frontend, strategy

### Resolution
- **Resolved**: 2026-06-21T00:00:00+08:00
- **Promoted**: AGENTS.md
- **Notes**: Added project-level optimization split rule.

---

## [LRN-20260806-001] correction

**Logged**: 2026-08-06T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Strategy discussions must use the user's active filter selections, not only the web form defaults.

### Details
The US scanner defaults disable several optional trend filters, but the user had enabled them. Candidate-quality analysis should distinguish configured runtime parameters from defaults before attributing excessive results to disabled gates.

### Suggested Action
When discussing scan quality, ask for or inspect the saved scan parameters and state assumptions explicitly.

### Metadata
- Source: user_feedback
- Related Files: web_app.py, frontend/src/scanners.jsx
- Tags: strategy, scanner, configuration

---

## [LRN-20260806-002] correction

**Logged**: 2026-08-06T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: frontend

### Summary
When a user points to clipped text inside an open panel, fix that field rather than relocating the panel trigger.

### Details
The Risk request referred to the VIX value being truncated in the rightmost detail cell. The initial change incorrectly moved the top-right Risk trigger into a separate page-wide status bar.

### Suggested Action
Use the supplied screenshot to identify the exact clipped element and make the smallest layout change at that element.

### Metadata
- Source: user_feedback
- Related Files: frontend/src/ui.jsx, frontend/src/styles.css
- Tags: risk-panel, text-overflow, scope

### Resolution
- **Resolved**: 2026-08-06T00:00:00+08:00
- **Notes**: Restored the original Risk trigger and allowed the rightmost VIX value to wrap fully.

---

## [LRN-20260806-003] correction

**Logged**: 2026-08-06T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Summary
Controls presented beside selectable strategy modes must be interactive when users are expected to compare holding results.

### Details
The chart's optional conditions were rendered as status-only spans while the adjacent original/market-pressure choices were buttons. This visual grouping implied all items were selectable, but only the first group changed the strategy. Two secondary condition values were also echoed in the payload without affecting historical signals.

### Suggested Action
Use semantic buttons with pressed states for chart-level strategy comparisons, connect each control to real backend calculation, and browser-test that markers or holding intervals actually change.

### Metadata
- Source: user_feedback
- Related Files: frontend/src/StrategyChart.jsx, web_app.py, backtest.py
- Tags: chart, interaction, strategy-conditions, truthful-ui

### Resolution
- **Resolved**: 2026-08-06T00:00:00+08:00
- **Notes**: All five optional conditions now refetch and recalculate the chart; secondary filters have matching backtest logic.

---
