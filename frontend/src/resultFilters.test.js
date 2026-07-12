import test from "node:test";
import assert from "node:assert/strict";
import { filterCandidates } from "./resultFilters.js";

const rows = [
  { symbol: "NVDA", company_display_name: "英伟达", signal_type: "B1_trend_confirm", technical_rating: "Strong", technical_score: 4.6, selection_streak: 2 },
  { symbol: "MU", company_display_name: "美光科技", signal_type: "B2_reentry", technical_rating: "Medium", technical_score: 3.1, is_new_candidate: true, selection_streak: 1 },
];

test("filters candidates without another market request", () => {
  assert.deepEqual(filterCandidates(rows, "us", { query: "英伟达", signal: "all", rating: "all", minScore: "", onlyNew: false, consecutive: false }).map((row) => row.symbol), ["NVDA"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "B2", rating: "all", minScore: 3, onlyNew: true, consecutive: false }).map((row) => row.symbol), ["MU"]);
  assert.deepEqual(filterCandidates(rows, "us", { query: "", signal: "all", rating: "Strong", minScore: 4, onlyNew: false, consecutive: true }).map((row) => row.symbol), ["NVDA"]);
});
