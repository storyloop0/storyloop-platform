import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { removeFixtureDir, requireFreeApiPort, stopOwnedApi } from "./fixtureLifecycle";

const repo = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const fixture = join(repo, "tests", "player_smoke_server.py");
const localPython = join(repo, ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.STORYLOOP_TEST_PYTHON ||
  (existsSync(localPython) ? localPython : "python");
const health = "http://127.0.0.1:8765/health";

async function waitForCompletedTurns(page: import("@playwright/test").Page, gameId: string, count: number) {
  await expect.poll(async () => {
    const response = await page.request.get(`/v1/saves/${gameId}/history`);
    if (!response.ok()) return -1;
    const history = await response.json();
    return history.turns.filter((turn: { response?: { body?: string } }) =>
      typeof turn.response?.body === "string" && turn.response.body.trim().length > 0).length;
  }, { timeout: 30000 }).toBe(count);
}

async function startApi(db: string): Promise<ChildProcess> {
  await requireFreeApiPort();
  const child = spawn(python, [fixture, "--db", db], {
    cwd: repo,
    env: { ...process.env, PYTHONPATH: join(repo, "src") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let spawnError: Error | null = null;
  child.once("error", (error) => { spawnError = error; });
  child.stdout?.on("data", (chunk) => { output += String(chunk); });
  child.stderr?.on("data", (chunk) => { output += String(chunk); });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (spawnError) throw new Error(`test API could not start: ${spawnError.message}`);
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error(`test API exited: ${output}`);
    try {
      const response = await fetch(health);
      if (response.ok) return child;
    } catch { /* wait for startup */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  await stopOwnedApi(child);
  throw new Error(`test API did not start: ${output}`);
}

test("player can register, play, restart the API, log in and continue", async ({ page }) => {
  const temp = mkdtempSync(join(repo, ".player-smoke-"));
  const db = join(temp, "player.sqlite3");
  let api: ChildProcess | null = null;
  try {
    api = await startApi(db);
    await page.route("**/*", (route) => {
      const url = new URL(route.request().url());
      return url.protocol.startsWith("http") && url.origin !== "http://127.0.0.1:5173"
        ? route.abort() : route.continue();
    });
    await page.goto("/login");
    await page.getByRole("tab", { name: "注册" }).click();
    await page.getByLabel("用户名").fill(`smoke${Date.now()}`);
    await page.getByLabel("密码").fill("smoke-password-123");
    const username = await page.getByLabel("用户名").inputValue();
    await page.getByRole("button", { name: "创建账号" }).click();
    await expect(page.getByRole("heading", { name: "港口 NPC 对话" })).toBeVisible();
    await page.getByRole("article").filter({
      has: page.getByRole("heading", { name: "港口 NPC 对话" }),
    }).getByRole("button", { name: "开始旅程" }).click();
    await expect(page).toHaveURL(/\/play\/[^/]+$/);
    const gameId = new URL(page.url()).pathname.split("/").pop();
    const action = page.getByLabel("写下你的行动或想说的话");
    await action.fill("我走向码头");
    await action.press("Enter");
    await waitForCompletedTurns(page, gameId!, 1);
    await expect(action).toBeVisible();
    await expect(page.getByText("我走向码头", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await stopOwnedApi(api);
    api = null;
    api = await startApi(db);
    await page.getByLabel("用户名").fill(username);
    await page.getByLabel("密码").fill("smoke-password-123");
    await page.getByRole("button", { name: "登录并继续" }).click();
    await expect(page.getByRole("heading", { name: "港口 NPC 对话" })).toBeVisible();
    await page.getByRole("link", { name: "我的存档" }).click();
    await expect(page.getByRole("heading", { name: "所有存档" })).toBeVisible();
    const savedGame = page.locator(".save-row").filter({ hasText: "港口 NPC 对话" });
    const continueLink = savedGame.getByRole("link", { name: "继续阅读" });
    await expect(continueLink).toHaveAttribute("href", `/play/${gameId}`);
    await continueLink.click();
    await expect(page).toHaveURL(new RegExp(`/play/${gameId}$`));
    await expect(page.getByText("我走向码头", { exact: true })).toBeVisible();
    const nextAction = page.getByLabel("写下你的行动或想说的话");
    await expect(nextAction).toBeVisible({ timeout: 10000 });
    await nextAction.fill("我询问码头工");
    await nextAction.press("Enter");
    await waitForCompletedTurns(page, gameId!, 2);
    await expect(nextAction).toBeVisible();
    await expect(page.getByText("我询问码头工", { exact: true })).toBeVisible();
  } finally {
    await page.close();
    if (api) await stopOwnedApi(api);
    removeFixtureDir(temp, repo, ".player-smoke-", python);
  }
});
