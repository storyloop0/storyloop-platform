import assert from "node:assert/strict";
import { test } from "node:test";

import * as pending from "../src/lib/pendingTurn.ts";
const { clearPendingTurn, readPendingTurn, savePendingTurn } = pending;

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

test("an interrupted turn keeps its request ID across a page reload", () => {
  const storage = memoryStorage();
  savePendingTurn("game-a", { id: "request-1", text: "我走向门口" }, storage);
  assert.deepEqual(readPendingTurn("game-a", storage), {
    id: "request-1", text: "我走向门口",
  });
  assert.equal(readPendingTurn("game-b", storage), null);
  clearPendingTurn("game-a", storage);
  assert.equal(readPendingTurn("game-a", storage), null);
});

test("blocked browser storage does not prevent sending a turn", () => {
  const blocked = {
    getItem: () => { throw new Error("storage blocked"); },
    setItem: () => { throw new Error("storage blocked"); },
    removeItem: () => { throw new Error("storage blocked"); },
  };
  assert.doesNotThrow(() => savePendingTurn("game-a", { id: "request-1", text: "你好" }, blocked));
  assert.equal(readPendingTurn("game-a", blocked), null);
  assert.doesNotThrow(() => clearPendingTurn("game-a", blocked));
});

test("manual recovery state survives refresh without discarding the original request", () => {
  const storage = memoryStorage();
  const turn = { id: "request-1", text: "我走向门口", retryable: false, error: "需要人工恢复" };
  savePendingTurn("game-a", turn, storage);
  assert.deepEqual(readPendingTurn("game-a", storage), turn);
});

test("uncertain lock persists the exact original request and safe copy", () => {
  const storage = memoryStorage();
  const turn = { id: "request-uncertain", text: " 我走向门口 ", retryable: false,
    error: "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。" };
  savePendingTurn("game-a", turn, storage);
  assert.deepEqual(readPendingTurn("game-a", storage), turn);
});

test("only matching explicitly unstarted requests unlock editing and clear persisted pending", () => {
  const storage = memoryStorage();
  savePendingTurn("game-a", { id: "request-1", text: "long message" }, storage);
  for (const failure of [undefined, {commit_state: "unknown", request_id: "request-1"},
    {commit_state: "committed", request_id: "request-1"},
    {commit_state: "not_started", request_id: "another-request"}]) {
    assert.equal(pending.discardUnstartedTurn("game-a", "request-1", failure, storage), false);
    assert.equal(readPendingTurn("game-a", storage).id, "request-1");
  }
  assert.equal(pending.discardUnstartedTurn("game-a", "request-1",
    {commit_state: "not_started", request_id: "request-1"}, storage), true);
  assert.equal(readPendingTurn("game-a", storage), null);
});
