import { type ChildProcess, spawn } from "node:child_process";
import process from "node:process";
import { readBootstrap, retrievalEnvironment } from "./runtime.ts";

let child: ChildProcess | undefined;
let stopped = false;
let ready = false;
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
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    stopped = true;
    signalGroup(signal);
    killTimer = setTimeout(() => signalGroup("SIGKILL"), 5000);
    release();
  });
}
process.on("SIGUSR1", () => ready = true);

try {
  const data = readBootstrap("/run/secrets/onepassword.json");
  child = spawn("/usr/local/bin/op", [
    "run",
    "--environment",
    data.environment_id,
    "--",
    Deno.execPath(),
    "run",
    "--allow-read",
    "--allow-write",
    "--allow-env",
    "--allow-run",
    "--allow-sys",
    "/app/managed/app.ts",
    String(process.pid),
    ...Deno.args,
  ], {
    env: retrievalEnvironment(Deno.env.toObject(), data.service_account_token),
    detached: true,
    stdio: ["ignore", "inherit", "ignore"],
  });
  if (stopped) signalGroup("SIGTERM");
  const deadline = setTimeout(() => {
    if (!ready) {
      signalGroup("SIGTERM");
      killTimer = setTimeout(() => signalGroup("SIGKILL"), 5000);
    }
  }, 60000);
  const outcome = await new Promise<number | null>((resolve, reject) => {
    child!.once("error", reject);
    child!.once("exit", resolve);
  }).finally(() => {
    clearTimeout(deadline);
    clearTimeout(killTimer);
    signalGroup("SIGTERM");
  });
  if (ready && outcome === 0) Deno.exit(0);
} catch {
  signalGroup("SIGTERM");
}
if (!stopped) {
  console.error(
    "Managed upstream stopped. Check health and configuration; restart to retry.",
  );
  await stop;
}
clearTimeout(killTimer);
