import assert from "node:assert/strict";
import test from "node:test";
import { parseTurnRecoveryStatus, ContractError } from "../src/lib/contracts.ts";
import { allowRecovery, inputHash, matchesRecovery, upsertCompletedTurn } from "../src/lib/turnRecovery.ts";

const ready = { request_id: "request-1", status: "recoverable", can_recover: true, reason_code: "SCENE_READY" };

test("strict status parser accepts only the frozen combinations and matching request", () => {
  assert.deepEqual(parseTurnRecoveryStatus(ready, "request-1"), ready);
  for (const [status, can_recover, reason_code] of [
    ["complete", true, "RECEIPT_READY"],
    ["recoverable", true, "PREPARED_READY"], ["recoverable", true, "SCENE_READY"],
    ...["INPUT_MISMATCH", "LATER_REQUEST", "EVIDENCE_INVALID", "CONFIG_CHANGED", "USAGE_UNAVAILABLE", "LEGACY_REQUEST"]
      .map((reason) => ["blocked", false, reason]),
    ["not_available", false, "NO_SNAPSHOT"], ["not_available", false, "UNSUPPORTED"],
  ]) assert.doesNotThrow(() => parseTurnRecoveryStatus({ request_id: "request-1", status, can_recover, reason_code }, "request-1"));
  for (const value of [
    { ...ready, status: "future" }, { ...ready, reason_code: "FUTURE" },
    { ...ready, can_recover: false }, { ...ready, extra: true },
    { ...ready, request_id: "request-2" },
    { ...ready, status: "complete", reason_code: "SCENE_READY" },
    { ...ready, status: "blocked", can_recover: false, reason_code: "NO_SNAPSHOT" },
  ]) assert.throws(() => parseTurnRecoveryStatus(value, "request-1"), ContractError);
});

test("positive status grants a manual action only for its live pending identity", () => {
  const proof = { gameId: "game-a", requestId: "request-1", text: " unchanged ", generation: 2, status: ready };
  assert.equal(allowRecovery(ready, false), true);
  assert.equal(allowRecovery(ready, true), false);
  assert.equal(allowRecovery({ request_id: "request-1", status: "blocked", can_recover: false, reason_code: "LATER_REQUEST" }, false), false);
  assert.equal(matchesRecovery(proof, "game-a", { id: "request-1", text: " unchanged " }, 2, false), true);
  assert.equal(matchesRecovery(proof, "game-b", { id: "request-1", text: " unchanged " }, 2, false), false);
  assert.equal(matchesRecovery(proof, "game-a", { id: "request-2", text: " unchanged " }, 2, false), false);
  assert.equal(matchesRecovery(proof, "game-a", { id: "request-1", text: "changed" }, 2, false), false);
  assert.equal(matchesRecovery(proof, "game-a", { id: "request-1", text: " unchanged " }, 3, false), false);
  assert.equal(matchesRecovery(proof, "game-a", { id: "request-1", text: " unchanged " }, 2, true), false);
});

test("hash uses unchanged UTF-8 input", async () => {
  assert.equal(await inputHash(" 你好\n"), "e43dbe3a548f25dabf4b87968b82735de6cfaf353d6bdd1b9690cc76de5dd209");
});

test("completed original request replaces its history row once", () => {
  const existing = [{ request_id: "request-1", input: null, response: { body: "old" } }];
  const result = upsertCompletedTurn(existing, "request-1", "原文", { body: "new" });
  assert.deepEqual(result, [{ request_id: "request-1", input: "原文", response: { body: "new" } }]);
  assert.equal(upsertCompletedTurn(result, "request-1", "原文", { body: "new" }).length, 1);
});
