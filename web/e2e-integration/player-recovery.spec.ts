import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const repo = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const fixture = join(repo, "tests", "player_recovery_server.py");
const python = process.env.STORYLOOP_TEST_PYTHON || "python";
const health = "http://127.0.0.1:8765/health";

async function requireFreePort(): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt++) {
    const server = createServer();
    try {
      await new Promise<void>((done, reject) => {
        server.once("error", reject);
        server.listen(8765, "127.0.0.1", done);
      });
      await new Promise<void>((done) => server.close(() => done()));
      return;
    } catch (error) {
      server.close();
      if ((error as NodeJS.ErrnoException).code !== "EADDRINUSE" || attempt === 49)
        throw new Error("Port 8765 is occupied before recovery acceptance", { cause: error });
      await new Promise((done) => setTimeout(done, 100));
    }
  }
}

async function startApi(db: string, posts: string, marker: string): Promise<ChildProcess> {
  await requireFreePort();
  const child = spawn(python, [fixture, "--db", db, "--posts", posts,
    "--fault-marker", marker], {
    cwd: repo,
    env: { ...process.env, PYTHONPATH: join(repo, "src"), PYTHONUTF8: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let spawnError: Error | null = null;
  child.once("error", (error) => { spawnError = error; });
  child.stdout?.on("data", (chunk) => { output += String(chunk); });
  child.stderr?.on("data", (chunk) => { output += String(chunk); });
  for (let attempt = 0; attempt < 200; attempt++) {
    if (spawnError) throw new Error(`test API could not start: ${spawnError.message}`);
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error(`test API exited: ${output}`);
    try {
      if ((await fetch(health)).ok) return child;
    } catch { /* wait for startup */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  child.kill();
  throw new Error(`test API did not start: ${output}`);
}

async function stopApi(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise<void>((done) => child.once("exit", () => done()));
  child.kill();
  await exited;
}

async function completedTurns(page: import("@playwright/test").Page, gameId: string) {
  const response = await page.request.get(`/v1/saves/${gameId}/history`);
  if (!response.ok()) return -1;
  const history = await response.json();
  return history.turns.filter((turn: { response?: { body?: string } }) =>
    typeof turn.response?.body === "string" && turn.response.body.trim()).length;
}

test("manual recovery replays the original request after API restart and permits the next turn", async ({ page }) => {
  const temp = mkdtempSync(join(repo, ".player-recovery-"));
  const db = join(temp, "world.sqlite3");
  const posts = join(temp, "sdk-posts.log");
  const marker = join(temp, "interrupted.marker");
  const sent: { text: string; request_id: string }[] = [];
  const statuses: string[] = [];
  let api: ChildProcess | null = null;
  try {
    api = await startApi(db, posts, marker);
    await page.route("**/*", (route) => {
      const url = new URL(route.request().url());
      return url.protocol.startsWith("http") && url.origin !== "http://127.0.0.1:5173"
        ? route.abort() : route.continue();
    });
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/v1\/saves\/[^/]+\/turns\/stream$/.test(request.url()))
        sent.push(request.postDataJSON());
    });
    page.on("response", (response) => {
      if (/\/turns\/[^/]+\/recovery\?input_hash=/.test(response.url()))
        statuses.push(response.url());
    });
    await page.goto("/login");
    await page.getByRole("tab", { name: "注册" }).click();
    await page.getByLabel("用户名").fill(`recovery${Date.now()}`);
    await page.getByLabel("密码").fill("recovery-password-123");
    await page.getByRole("button", { name: "创建账号" }).click();
    await page.getByRole("article").filter({
      has: page.getByRole("heading", { name: "港口 NPC 对话" }),
    }).getByRole("button", { name: "开始旅程" }).click();
    await expect(page).toHaveURL(/\/play\/[^/]+$/);
    const gameId = new URL(page.url()).pathname.split("/").pop()!;
    const action = page.getByLabel("写下你的行动或想说的话");
    const firstText = "我询问早班船什么时候靠岸";
    await action.fill(firstText);
    await action.press("Enter");
    await expect.poll(() => existsSync(marker), { timeout: 15000 }).toBe(true);
    await expect(page.locator(".pending-turn .turn-status-error")).toBeVisible();
    expect(sent).toHaveLength(1);
    const originalId = sent[0].request_id;
    expect(originalId).toMatch(/^[A-Za-z0-9_-]{1,64}$/);

    await stopApi(api);
    api = null;
    api = await startApi(db, posts, marker);
    await page.reload();
    const recover = page.getByRole("button", { name: "继续恢复这条行动" });
    await expect(recover).toBeVisible({ timeout: 15000 });
    expect(statuses.some((url) => url.includes(`/turns/${originalId}/recovery?input_hash=`))).toBe(true);
    await recover.click();
    await expect.poll(() => completedTurns(page, gameId), { timeout: 30000 }).toBe(1);
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual(sent[0]);
    expect(readFileSync(posts, "utf8").trim().split("\n")).toHaveLength(1);
    await expect(page.getByText(firstText, { exact: true })).toBeVisible();

    const nextText = "我向码头工道谢";
    await action.fill(nextText);
    await action.press("Enter");
    await expect.poll(() => completedTurns(page, gameId), { timeout: 30000 }).toBe(2);
    expect(sent).toHaveLength(3);
    expect(sent[2].request_id).not.toBe(originalId);
    expect(readFileSync(posts, "utf8").trim().split("\n")).toHaveLength(2);
  } finally {
    await page.close();
    if (api) await stopApi(api);
    if (resolve(temp).startsWith(repo + "\\") || resolve(temp).startsWith(repo + "/")) {
      try {
        rmSync(temp, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
        // On Windows the SQLite file is owned by the Python fixture process.
        const cleanup = spawnSync(python, ["-c",
          "import pathlib,shutil,sys\np=pathlib.Path(sys.argv[1]).resolve()\nroot=pathlib.Path(sys.argv[2]).resolve()\nif p.parent!=root or not p.name.startswith('.player-recovery-'):\n raise SystemExit('unsafe cleanup path')\nshutil.rmtree(p)",
          temp, repo], { cwd: repo, encoding: "utf8" });
        if (cleanup.status !== 0) throw new Error(`Python fixture cleanup failed: ${cleanup.stderr}`);
      }
    }
  }
});
