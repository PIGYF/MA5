import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(directory, "scanners.jsx"), "utf8");

test("scanner refreshes server results and keeps result filters session-local", () => {
  assert.match(source, /getJson\(`\$\{prefix\}\/scan\/latest`\)/);
  assert.match(source, /setInterval\(syncServerState, 5000\)/);
  assert.doesNotMatch(source, /usePersistentState\(`scanner\.\$\{market\}\.resultFilter`/);
});
