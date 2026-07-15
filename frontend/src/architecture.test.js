import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const sources = ["ui.jsx", "scanners.jsx", "portfolios.jsx", "StrategyChart.jsx", "BacktestReport.jsx", "BatchReport.jsx"]
  .map((name) => readFileSync(new URL(name, import.meta.url), "utf8"))
  .join("\n");

test("React workspace does not regress to iframe rendering", () => {
  assert.equal(sources.includes("<iframe"), false);
  assert.equal(sources.includes("ChartFrame"), false);
});

test("market risk state refreshes after initial bootstrap", () => {
  const main = readFileSync(new URL("main.jsx", import.meta.url), "utf8");
  assert.match(main, /setInterval\(refreshMarketState, 300000\)/);
  assert.match(main, /window\.addEventListener\("focus", refreshMarketState\)/);
  assert.match(main, /visibilitychange/);
});
