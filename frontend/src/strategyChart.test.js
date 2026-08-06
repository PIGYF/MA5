import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("StrategyChart.jsx", import.meta.url), "utf8");

test("chart entry conditions are interactive controls", () => {
  assert.match(source, /className="condition-statuses" role="group"/);
  assert.match(source, /aria-pressed=\{Boolean\(payload\?\.conditions\?\.\[key\]\)\}/);
  assert.match(source, /onClick=\{\(\) => toggleCondition\(key\)\}/);
});

test("condition overrides are included in chart requests", () => {
  assert.match(source, /\{ \.\.\.params, \.\.\.conditionOverrides, symbol, preset, market \}/);
});
