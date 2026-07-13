import test from "node:test";
import assert from "node:assert/strict";
import { xueqiuUrl } from "./lib.js";

test("builds Xueqiu links for US and A-share symbols", () => {
  assert.equal(xueqiuUrl("nvda"), "https://xueqiu.com/k?q=NVDA");
  assert.equal(xueqiuUrl("600519"), "https://xueqiu.com/k?q=600519");
});
