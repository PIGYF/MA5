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
