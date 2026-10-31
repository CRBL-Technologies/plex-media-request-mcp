import { type ChildProcess, spawn } from "node:child_process";
import process from "node:process";
import { projectUpstream } from "./runtime.ts";

let environment: Record<string, string> = {};
const source = Deno.env.toObject();
let child: ChildProcess | undefined;
let stopped = false;
let stopSignal: NodeJS.Signals = "SIGTERM";
let killTimer: ReturnType<typeof setTimeout> | undefined;
let release: () => void = () => {};
const stop = new Promise<void>((resolve) => release = resolve);
function signalGroup(signal: NodeJS.Signals): void {
  if (child?.pid) {
    try {
      process.kill(-child.pid, signal);
    } catch { /* Already stopped. */ }
  }
}
function stopChild(): void {
  signalGroup(stopSignal);
  if (child && !killTimer) {
    // Reap the app before the outer loader's five-second deadline.
    killTimer = setTimeout(() => signalGroup("SIGKILL"), 4000);
  }
}
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    if (stopped) return;
    stopped = true;
    stopSignal = signal;
    stopChild();
    release();
  });
}
try {
  environment = projectUpstream(
    source,
    "/run/media-secrets/upstream.env",
  );
} catch {
  console.log("Managed upstream runtime fields are incomplete or invalid.");
  Deno.exit(1);
} finally {
  for (const key of Object.keys(source)) {
    if (key.startsWith("OP_") || !(key in environment)) Deno.env.delete(key);
    delete source[key];
  }
}
if (stopped) Deno.exit(0);
process.kill(Number(Deno.args[0]), "SIGUSR1");
let delay = 1000;
while (!stopped) {
  try {
    child = spawn(Deno.execPath(), [
      "run",
      "--env-file=/run/media-secrets/upstream.env",
      "--allow-read",
      "--allow-write",
      "--allow-env",
      "--allow-run",
      "--allow-net",
      "packages/media-server-mcp/src/index.ts",
      ...Deno.args.slice(1),
    ], { env: environment, detached: true, stdio: ["ignore", 1, 1] });
    const exited = new Promise<void>((resolve, reject) => {
      child!.once("error", reject);
      child!.once("exit", () => resolve());
    });
    if (stopped) stopChild();
    await exited;
  } catch {
    console.log("Upstream process could not start.");
  } finally {
    clearTimeout(killTimer);
    killTimer = undefined;
    signalGroup("SIGTERM");
    child = undefined;
  }
  if (stopped) break;
  console.log(`Upstream process stopped; retrying in ${delay / 1000} seconds.`);
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    stop,
    new Promise<void>((resolve) => timer = setTimeout(resolve, delay)),
  ]);
  clearTimeout(timer);
  delay = Math.min(delay * 2, 30000);
}
