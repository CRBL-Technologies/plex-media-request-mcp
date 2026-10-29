import { spawn } from "node:child_process";
import process from "node:process";
import { projectUpstream } from "./runtime.ts";

let environment: Record<string, string>;
try {
  environment = projectUpstream(
    Deno.env.toObject(),
    "/run/media-secrets/upstream.env",
  );
} catch {
  console.log("Managed upstream runtime fields are incomplete or invalid.");
  Deno.exit(1);
}
process.kill(Number(Deno.args[0]), "SIGUSR1");
const child = spawn(Deno.execPath(), [
  "run",
  "--env-file=/run/media-secrets/upstream.env",
  "--allow-read",
  "--allow-write",
  "--allow-env",
  "--allow-run",
  "--allow-net",
  "packages/media-server-mcp/src/index.ts",
  ...Deno.args.slice(1),
], { env: environment, stdio: ["ignore", 1, 1] });
// The supervisor signals the process group, which includes this native child.
// Keep this intermediary alive until the native app has completed shutdown.
process.on("SIGTERM", () => {});
process.on("SIGINT", () => {});
child.once("error", () => {
  console.log("Upstream process could not start.");
  Deno.exit(1);
});
child.once("exit", (code) => Deno.exit(code ?? 1));
