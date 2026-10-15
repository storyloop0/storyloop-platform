import { expect, test, type Page, type Route } from "@playwright/test";
import { commonApi, view } from "./support";
import { createHash } from "node:crypto";

async function playerApi(page: Page, initial: typeof view & Record<string, unknown>, turn: (route: Route) => Promise<void>,
  recovery?: (route: Route) => Promise<void>) {
  await page.route("**/v1/**", async (route) => {
    if (await commonApi(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/turns/stream")) return turn(route);
    if (path.endsWith("/recovery")) return recovery
      ? recovery(route) : route.fulfill({ status: 404, json: { detail: "No snapshot" } });
    if (path === "/v1/saves") return route.fulfill({ json: { saves: [{ ...initial, title: "测试故事", available: true }] } });
    if (path.endsWith("/resume")) return route.fulfill({ json: initial });
    if (path.endsWith("/history")) return route.fulfill({ json: { game_id: "game", intro: initial, turns: [] } });
    if (path.endsWith("/cast")) return route.fulfill({ json: { cast: [] } });
    if (path.endsWith("/player-card")) return route.fulfill({ json: { name: "", fields: [] } });
    if (path.endsWith("/settings")) return route.fulfill({ json: { temperature: 1, context_window_tokens: 65536,
      default_temperature: 1, default_context_window_tokens: 65536, max_context_window_tokens: 65536 } });
    return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
  });
}

test("positive read-only proof manually retrieves the original request once after reload", async ({ page }) => {
  const text = " 你好\n";
  const posts: { request_id: string; text: string }[] = [];
  const queries: string[] = [];
  await page.addInitScript((pending) => sessionStorage.setItem("storyloop.pending.game", JSON.stringify(pending)),
    { id: "saved-request", text, retryable: false, error: "需要人工恢复" });
  await playerApi(page, view, async (route) => {
    posts.push(route.request().postDataJSON());
    const response = { ...view, state_version: 1, body: "原结果已取回。", turn_id: "saved-request" };
    await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({ type: "complete", view: response })}\n\n` });
  }, async (route) => {
    queries.push(route.request().url());
    await route.fulfill({ json: { request_id: "saved-request", status: "complete", can_recover: true,
      reason_code: "RECEIPT_READY" } });
  });
  await page.goto("/play/game");
  await expect(page.getByRole("button", { name: "取回结果" })).toBeVisible();
  expect(queries).toHaveLength(1);
  expect(new URL(queries[0]).searchParams.get("input_hash")).toBe(createHash("sha256").update(text).digest("hex"));
  await page.getByRole("button", { name: "取回结果" }).click();
  await expect(page.getByText("原结果已取回。", { exact: true })).toHaveCount(1);
  await expect(page.locator(".player-entry").filter({ hasText: "你好" })).toHaveCount(1);
  expect(posts).toEqual([{ request_id: "saved-request", text }]);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("storyloop.pending.game"))).toBeNull();
});

test("uncertain GET locks a disconnected original request through reload and invalid reads", async ({ page }) => {
  const posts: { request_id: string; text: string }[] = [];
  let reads = 0;
  await playerApi(page, view, async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ contentType: "text/event-stream", body: 'data: {"type":"stage","stage":"thinking"}\n\n' });
  }, async (route) => {
    reads++;
    if (reads > 1) return route.fulfill({ status: 500, json: { detail: "offline" } });
    return route.fulfill({ json: { request_id: posts[0].request_id, status: "blocked",
      can_recover: false, reason_code: "GENERATION_UNCERTAIN" } });
  });
  await page.goto("/play/game");
  await page.getByLabel("写下你的行动或想说的话").fill("我走向码头");
  await page.getByLabel("写下你的行动或想说的话").press("Enter");
  const copy = "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。";
  await expect(page.getByText(copy, { exact: true })).toBeVisible();
  const saved = await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!));
  expect(saved).toEqual({ id: posts[0].request_id, text: "我走向码头", retryable: false, error: copy });
  await page.reload();
  await expect(page.getByText(copy, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试这条行动" })).toHaveCount(0);
  await expect(page.getByLabel("写下你的行动或想说的话")).toHaveCount(0);
  expect(posts).toHaveLength(1);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toEqual(saved);
});

test("uncertain SSE preserves the lock until a later valid manual scene recovery", async ({ page }) => {
  const posts: { request_id: string; text: string }[] = [];
  let ready = false;
  await playerApi(page, view, async (route) => {
    const input = route.request().postDataJSON();
    posts.push(input);
    const event = posts.length === 1
      ? { type: "error", message: "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。",
        code: "GENERATION_UNCERTAIN", retryable: false, commit_state: "unknown", request_id: input.request_id }
      : { type: "complete", view: { ...view, state_version: 1, body: "原结果已取回。", turn_id: input.request_id } };
    await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify(event)}\n\n` });
  }, async (route) => route.fulfill({ json: { request_id: posts[0].request_id,
    status: ready ? "recoverable" : "blocked", can_recover: ready,
    reason_code: ready ? "SCENE_READY" : "GENERATION_UNCERTAIN" } }));
  await page.goto("/play/game");
  await page.getByLabel("写下你的行动或想说的话").fill("我走向码头");
  await page.getByLabel("写下你的行动或想说的话").press("Enter");
  const copy = "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。";
  await expect(page.getByText(copy, { exact: true })).toBeVisible();
  expect(posts).toHaveLength(1);
  await expect(page.getByRole("button", { name: "重试这条行动" })).toHaveCount(0);
  ready = true;
  await page.reload();
  await expect(page.getByRole("button", { name: "继续恢复这条行动" })).toBeVisible();
  expect(posts).toHaveLength(1);
  await page.getByRole("button", { name: "继续恢复这条行动" }).click();
  await expect(page.getByText("原结果已取回。", { exact: true })).toBeVisible();
  expect(posts).toEqual([posts[0], posts[0]]);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("storyloop.pending.game"))).toBeNull();
});

for (const invalidStatus of [
  { request_id: "other-request", status: "recoverable", can_recover: true, reason_code: "SCENE_READY" },
  { request_id: "saved-request", status: "recoverable", can_recover: false, reason_code: "SCENE_READY" },
  { request_id: "saved-request", status: "recoverable", can_recover: true, reason_code: "SCENE_READY", extra: true },
]) test(`invalid status ${JSON.stringify(invalidStatus)} keeps the nonretryable pending action locked`, async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("storyloop.pending.game",
    JSON.stringify({ id: "saved-request", text: "原文", retryable: false, error: "需要人工恢复" })));
  let posts = 0;
  await playerApi(page, view, async (route) => { posts++; await route.abort(); }, async (route) => {
    await route.fulfill({ json: invalidStatus });
  });
  await page.goto("/play/game");
  await expect(page.getByText("需要人工恢复", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "继续恢复这条行动" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重试这条行动" })).toHaveCount(0);
  expect(posts).toBe(0);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toMatchObject({ id: "saved-request", text: "原文" });
});

test("scene recovery sends once and disables the recovery action during the send", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("storyloop.pending.game",
    JSON.stringify({ id: "saved-request", text: "原文", retryable: false, error: "需要人工恢复" })));
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const posts: { request_id: string; text: string }[] = [];
  await playerApi(page, view, async (route) => {
    posts.push(route.request().postDataJSON());
    await held;
    await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({ type: "complete",
      view: { ...view, state_version: 1, body: "恢复后的故事。", turn_id: "saved-request" } })}\n\n` });
  }, async (route) => route.fulfill({ json: { request_id: "saved-request", status: "recoverable",
    can_recover: true, reason_code: "SCENE_READY" } }));
  await page.goto("/play/game");
  const button = page.getByRole("button", { name: "继续恢复这条行动" });
  await expect(button).toBeVisible();
  await button.click();
  await expect(button).toHaveCount(0);
  expect(posts).toEqual([{ request_id: "saved-request", text: "原文" }]);
  release();
  await expect(page.getByText("恢复后的故事。", { exact: true })).toHaveCount(1);
});

test("a failed approved replay keeps the original nonretryable lock", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("storyloop.pending.game",
    JSON.stringify({ id: "saved-request", text: "原文", retryable: false, error: "需要人工恢复" })));
  let statusReads = 0;
  await playerApi(page, view, async (route) => route.abort(), async (route) => {
    statusReads++;
    if (statusReads === 1) return route.fulfill({ json: { request_id: "saved-request", status: "recoverable",
      can_recover: true, reason_code: "SCENE_READY" } });
    return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
  });
  await page.goto("/play/game");
  await page.getByRole("button", { name: "继续恢复这条行动" }).click();
  await expect.poll(() => statusReads).toBe(2);
  await expect(page.getByRole("button", { name: "重试这条行动" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "继续恢复这条行动" })).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toMatchObject({
    id: "saved-request", text: "原文", retryable: false,
  });
});

test("a delayed status for save A cannot unlock save B", async ({ page }) => {
  await page.addInitScript(() => {
    for (const game of ["game", "game-b"]) sessionStorage.setItem(`storyloop.pending.${game}`,
      JSON.stringify({ id: `request-${game}`, text: `action-${game}`, retryable: false, error: "需要人工恢复" }));
  });
  let release!: () => void;
  let started!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const queried = new Promise<void>((resolve) => { started = resolve; });
  await playerApi(page, view, async (route) => route.abort(), async (route) => {
    if (new URL(route.request().url()).pathname.includes("/saves/game/")) {
      started();
      await held;
      await route.fulfill({ json: { request_id: "request-game", status: "recoverable",
        can_recover: true, reason_code: "SCENE_READY" } }).catch(() => {});
    } else {
      await route.fulfill({ json: { request_id: "request-game-b", status: "blocked",
        can_recover: false, reason_code: "LATER_REQUEST" } });
    }
  });
  await page.goto("/play/game");
  await queried;
  await page.goto("/play/game-b");
  release();
  await expect(page.getByText("action-game-b", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "继续恢复这条行动" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "取回结果" })).toHaveCount(0);
});

test("old oversized choice unlocks editing and submits a shorter note with a fresh ID", async ({ page }) => {
  const oldNote = "字".repeat(10000);
  await page.addInitScript((text) => sessionStorage.setItem("storyloop.pending.game",
    JSON.stringify({ id: "old-request", text })), `/choose dockhand ${oldNote}`);
  const calls: { request_id: string; text: string }[] = [];
  await playerApi(page, { ...view, interaction: { id: "choice", kind: "message", prompt: "给谁留言？",
    options: [{ id: "dockhand", label: "码头工", enabled: true, requires_text: true }] } }, async (route) => {
    const input = route.request().postDataJSON();
    calls.push(input);
    const event = calls.length === 1
      ? { type: "error", message: "留言过长", code: "INVALID_TURN_INPUT", retryable: false,
        commit_state: "not_started", request_id: input.request_id }
      : { type: "complete", view: { ...view, state_version: 1, body: "留言已送达。", turn_id: input.request_id } };
    await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify(event)}\n\n` });
  });
  await page.goto("/play/game");
  await page.getByRole("button", { name: "重试这条行动" }).click();
  const note = page.getByRole("textbox", { name: "写下你的留言" });
  await expect(note).toBeEnabled();
  await expect(note).toHaveValue(oldNote);
  await expect(page.getByRole("button", { name: "确认选择" })).toBeDisabled();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("storyloop.pending.game"))).toBeNull();
  await note.fill("你好");
  await page.getByRole("button", { name: "确认选择" }).click();
  await expect(page.getByText("留言已送达。", { exact: true })).toBeVisible();
  expect(calls).toHaveLength(2);
  expect(calls[0].request_id).toBe("old-request");
  expect(calls[1].request_id).not.toBe("old-request");
  expect(calls[1].text).toBe("/choose dockhand 你好");
});

test("disconnected stream retains original input and request ID across refresh", async ({ page }) => {
  const calls: { request_id: string; text: string }[] = [];
  await playerApi(page, view, async (route) => {
    calls.push(route.request().postDataJSON());
    await route.fulfill({ contentType: "text/event-stream", body: 'data: {"type":"stage","stage":"thinking"}\n\n' });
  });
  await page.goto("/play/game");
  await page.getByLabel("写下你的行动或想说的话").fill("我走向码头");
  await page.getByLabel("写下你的行动或想说的话").press("Enter");
  await expect(page.getByRole("button", { name: "重试这条行动" })).toBeVisible();
  await expect(page.getByLabel("写下你的行动或想说的话")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "重试这条行动" }).click();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls[1]).toEqual(calls[0]);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toEqual({
    id: calls[0].request_id, text: "我走向码头",
  });
});

test("manual recovery remains blocked after refresh with the original request preserved", async ({ page }) => {
  let requestId = "";
  await playerApi(page, view, async (route) => {
    requestId = route.request().postDataJSON().request_id;
    await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({
      type: "error", message: "这条行动需要人工恢复，请联系管理员。",
      code: "TURN_RECOVERY_REQUIRED", commit_state: "unknown", retryable: false, request_id: requestId,
    })}\n\n` });
  });
  await page.goto("/play/game");
  await page.getByLabel("写下你的行动或想说的话").fill("我走向码头");
  await page.getByLabel("写下你的行动或想说的话").press("Enter");
  await expect(page.getByText("这条行动需要人工恢复，请联系管理员。", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("这条行动需要人工恢复，请联系管理员。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试这条行动" })).toHaveCount(0);
  await expect(page.getByLabel("写下你的行动或想说的话")).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toEqual({
    id: requestId, text: "我走向码头", retryable: false, error: "这条行动需要人工恢复，请联系管理员。",
  });
});

for (const outcome of ["unknown event", "malformed complete", "invalid JSON", "malformed HTTP failure"]) {
  test(`${outcome} retains input and request ID across refresh and retry`, async ({ page }) => {
    const calls: { request_id: string; text: string }[] = [];
    await playerApi(page, view, async (route) => {
      const input = route.request().postDataJSON();
      calls.push(input);
      if (outcome === "malformed HTTP failure") {
        return route.fulfill({ status: 400, json: { detail: "Incomplete failure metadata",
          commit_state: "not_started", request_id: input.request_id } });
      }
      const data = outcome === "unknown event" ? JSON.stringify({ type: "future", commit_state: "not_started", request_id: input.request_id })
        : outcome === "malformed complete" ? JSON.stringify({ type: "complete", view: { body: "Malformed content" } }) : "{invalid";
      await route.fulfill({ contentType: "text/event-stream", body: `data: ${data}\n\n` });
    });
    await page.goto("/play/game");
    await page.getByLabel("写下你的行动或想说的话").fill("我走向码头");
    await page.getByLabel("写下你的行动或想说的话").press("Enter");
    await expect(page.getByRole("button", { name: "重试这条行动" })).toBeVisible();
    await expect(page.getByLabel("写下你的行动或想说的话")).toHaveCount(0);
    await expect(page.getByText("Malformed content", { exact: true })).toHaveCount(0);
    await page.reload();
    await page.getByRole("button", { name: "重试这条行动" }).click();
    await expect.poll(() => calls.length).toBe(2);
    expect(calls[1]).toEqual(calls[0]);
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem("storyloop.pending.game")!))).toEqual({
      id: calls[0].request_id, text: "我走向码头",
    });
  });
}
