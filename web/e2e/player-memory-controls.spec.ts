import { expect, test } from "@playwright/test";
import { commonApi } from "./support";

test("player can correct and delete one preference", async ({ page }) => {
  let memories = [{ id: "first", text: "喜欢快节奏", created_at: "2026-10-01T00:00:00Z",
    updated_at: "", source: "story_turn_batch", freshness: "current", expires_at: "2026-12-30T00:00:00Z" }];
  const mutations: string[] = [];
  const status = () => ({ available: true, enabled: true, memories, queued_inputs: 0,
    batch_size: 6, min_interval_hours: 12, charged_points: 0, generated_preference_max_age_days: 90 });
  await page.route("**/v1/**", async (route) => {
    if (await commonApi(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/me/memory" && route.request().method() === "GET") {
      return route.fulfill({ json: status() });
    }
    if (path === "/v1/me/memory/first" && route.request().method() === "PATCH") {
      expect(route.request().postDataJSON()).toEqual({ text: "喜欢慢节奏" });
      mutations.push("correct");
      memories = [{ ...memories[0], text: "喜欢慢节奏", source: "user_correction",
        updated_at: "2026-10-12T00:00:00Z", freshness: "current", expires_at: null }];
      return route.fulfill({ json: status() });
    }
    if (path === "/v1/me/memory/first" && route.request().method() === "DELETE") {
      mutations.push("delete");
      memories = [];
      return route.fulfill({ json: status() });
    }
    return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
  });

  await page.goto("/memory");
  await expect(page.getByText("喜欢快节奏")).toBeVisible();
  await page.getByRole("button", { name: "纠正偏好" }).click();
  await page.getByRole("textbox", { name: "偏好内容" }).fill("喜欢慢节奏");
  await page.getByRole("button", { name: "保存纠正" }).click();
  await expect(page.getByText("喜欢慢节奏")).toBeVisible();
  await expect(page.getByText("你已纠正，持续有效", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "删除偏好" }).click();
  await expect(page.getByText("确定删除这条偏好？")).toBeVisible();
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByText("喜欢慢节奏")).toHaveCount(0);
  expect(mutations).toEqual(["correct", "delete"]);
});

test("failed correction refreshes server state and reports error", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let reads = 0;
  await page.route("**/v1/**", async (route) => {
    if (await commonApi(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/me/memory" && route.request().method() === "GET") {
      reads++;
      return route.fulfill({ json: { available: true, enabled: true,
        memories: [{ id: "first", text: reads === 1 ? "旧偏好" : "服务器上的偏好",
          created_at: "", updated_at: "", source: "story_turn_batch", freshness: "unknown", expires_at: null }],
        queued_inputs: 0, batch_size: 6, min_interval_hours: 12, charged_points: 0,
        generated_preference_max_age_days: 90 } });
    }
    if (path === "/v1/me/memory/first" && route.request().method() === "PATCH") {
      return route.fulfill({ status: 503, json: { detail: "temporary error" } });
    }
    return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
  });
  await page.goto("/memory");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "纠正偏好" }).click();
  await page.getByRole("textbox", { name: "偏好内容" }).fill("新偏好");
  await page.getByRole("button", { name: "保存纠正" }).click();
  await expect(page.getByRole("alert")).toContainText("纠正未能完成");
  await expect(page.getByText("服务器上的偏好")).toBeVisible();
  expect(reads).toBeGreaterThan(1);
});

test("freshness labels keep stale and unknown preferences editable", async ({ page }) => {
  let off = false;
  const memories = [
    { id: "expired", text: "过期偏好", created_at: "2026-01-01T00:00:00Z", updated_at: "", source: "story_turn_batch", freshness: "expired", expires_at: "2026-04-01T00:00:00Z" },
    { id: "unknown", text: "日期未知偏好", created_at: "", updated_at: "", source: "", freshness: "unknown", expires_at: null },
    { id: "corrected", text: "人工纠正偏好", created_at: "2026-01-01T00:00:00Z", updated_at: "", source: "user_correction", freshness: "current", expires_at: null },
    { id: "current", text: "当前偏好", created_at: "2026-10-01T00:00:00Z", updated_at: "", source: "story_turn_batch", freshness: "current", expires_at: "2026-12-30T00:00:00Z" },
  ];
  await page.route("**/v1/**", async (route) => {
    if (await commonApi(route)) return;
    if (new URL(route.request().url()).pathname === "/v1/me/memory") {
      return route.fulfill({ json: { available: true, enabled: true, memories, queued_inputs: 0,
        batch_size: 6, min_interval_hours: 12, charged_points: 0,
        generated_preference_max_age_days: off ? null : 90 } });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto("/memory");
  await expect(page.getByText("自动提炼的偏好会在创建 90 天后停止供故事参考，记录仍可纠正或删除。")).toBeVisible();
  for (const [text, label] of [["过期偏好", "已过期，不再供故事参考"], ["日期未知偏好", "时间信息不足，暂不供故事参考"], ["人工纠正偏好", "你已纠正，持续有效"], ["当前偏好", "有效"]]) {
    const row = page.getByRole("listitem").filter({ hasText: text });
    await expect(row.getByText(label, { exact: true })).toBeVisible();
    await expect(row.getByRole("button", { name: "纠正偏好" })).toBeVisible();
    await expect(row.getByRole("button", { name: "删除偏好" })).toBeVisible();
  }
  off = true;
  await page.getByRole("button", { name: "刷新" }).click();
  await expect(page.getByText("未启用自动过期")).toBeVisible();
});
