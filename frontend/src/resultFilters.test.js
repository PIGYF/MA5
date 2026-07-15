import test from "node:test";
import assert from "node:assert/strict";
import { filterCandidates, refreshStaleDefaultRange } from "./resultFilters.js";

const rows = [
  { symbol: "NVDA", company_display_name: "英伟达", signal_type: "B1_trend_confirm", technical_rating: "Strong", technical_score: 4.6, selection_streak: 2, big_red_b1: true, above_ma5_3d: true },
  { symbol: "MU", company_display_name: "美光科技", signal_type: "B2_reentry", technical_rating: "Medium", technical_score: 3.1, is_new_candidate: true, selection_streak: 1, big_red_b1: false, above_ma5_3d: false },
];

test("filters candidates without another market request", () => {
  assert.deepEqual(filterCandidates(rows, "us", { query: "英伟达", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false }).map((row) => row.symbol), ["NVDA"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "B2", rating: "all", minScore: 3, onlyNew: true, consecutive: false }).map((row) => row.symbol), ["MU"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "all", rating: "Strong", minScore: 4, onlyNew: false, consecutive: true }).map((row) => row.symbol), ["NVDA"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false, bigRedB1: true }).map((row) => row.symbol), ["NVDA"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false, aboveMa5ThreeDays: true }).map((row) => row.symbol), ["NVDA"]);
});

test("refreshes only a stale default scan range", () => {
  const defaults = { start: "2026-05-05", end: "2026-07-14", max_symbols: 500 };
  assert.deepEqual(
    refreshStaleDefaultRange({ start: "2026-05-04", end: "2026-07-13", max_symbols: 300 }, defaults),
    { start: "2026-05-05", end: "2026-07-14", max_symbols: 300 },
  );
  assert.deepEqual(
    refreshStaleDefaultRange({ start: "2026-01-01", end: "2026-07-13", max_symbols: 300 }, defaults),
    { start: "2026-01-01", end: "2026-07-13", max_symbols: 300 },
  );
});
