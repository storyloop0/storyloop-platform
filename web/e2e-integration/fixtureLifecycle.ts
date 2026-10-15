import { spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, realpathSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { basename, dirname, resolve } from "node:path";

const apiPort = 8765;

export async function requireFreeApiPort(): Promise<void> {
  const server = createServer();
  try {
    await new Promise<void>((done, reject) => {
      server.once("error", reject);
      server.listen(apiPort, "127.0.0.1", done);
    });
    await new Promise<void>((done) => server.close(() => done()));
  } catch (error) {
    server.close();
    throw new Error(`Port ${apiPort} is occupied before fixture startup`, { cause: error });
  }
}

export async function stopOwnedApi(child: ChildProcess): Promise<void> {
  if (child.exitCode === null && child.signalCode === null) {
    const closed = new Promise<void>((done) => child.once("close", () => done()));
    if (!child.kill()) throw new Error("Could not stop owned test API process");
    await closed;
  }
  // Windows can release the child's process handle before its listening socket.
  // Poll only after stopping this fixture's own child; never kill a port owner.
  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      await requireFreeApiPort();
      return;
    } catch (error) {
      if (attempt === 49) throw new Error("Owned test API exited but its port stayed occupied", { cause: error });
      await new Promise((done) => setTimeout(done, 100));
    }
  }
}

export function removeFixtureDir(temp: string, repo: string, prefix: ".player-smoke-" | ".player-recovery-", python: string): void {
  const root = resolve(repo);
  const target = resolve(temp);
  if (dirname(target) !== root || !basename(target).startsWith(prefix))
    throw new Error(`Unsafe fixture cleanup target: ${target}`);
  if (!existsSync(target)) return;
  if (dirname(realpathSync(target)) !== realpathSync(root))
    throw new Error(`Fixture cleanup target escapes repository: ${target}`);
  try {
    rmSync(target, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
    const cleanup = spawnSync(python, ["-c",
      "import pathlib,shutil,sys\np=pathlib.Path(sys.argv[1]).resolve()\nroot=pathlib.Path(sys.argv[2]).resolve()\nif p.parent!=root or not p.name.startswith(sys.argv[3]):\n raise SystemExit('unsafe cleanup path')\nshutil.rmtree(p)",
      target, root, prefix], { cwd: root, encoding: "utf8" });
    if (cleanup.status !== 0) throw new Error(`Python fixture cleanup failed: ${cleanup.stderr}`);
  }
}
