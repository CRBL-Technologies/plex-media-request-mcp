// Run in the pinned managed upstream image; no external packages or network.
import assert from "node:assert/strict";
import { join } from "node:path";
import process from "node:process";
import {
  projectUpstream,
  readBootstrap,
  retrievalEnvironment,
} from "/app/managed/runtime.ts";

function fetched(): Record<string, string> {
  return {
    MCP_AUTH_TOKEN: "dummy-auth-token-with-more-than-thirty-two-bytes",
    RADARR_URL: "http://radarr:7878",
    RADARR_API_KEY: 'literal$#=with"quotes"',
    SONARR_URL: "http://sonarr:8989",
    SONARR_API_KEY: "dummy-sonarr",
    PLEX_URL: "http://plex:32400",
    PLEX_API_KEY: "dummy-plex",
    TMDB_API_KEY: "dummy-tmdb",
    TOOL_PROFILE: "full",
  };
}

Deno.test("bootstrap requires its exact schema and private regular file", () => {
  const directory = Deno.makeTempDirSync();
  try {
    const path = join(directory, "bootstrap.json");
    const value = {
      environment_id: "a".repeat(26),
      service_account_token: "dummy-token",
    };
    Deno.writeTextFileSync(path, JSON.stringify(value), { mode: 0o600 });
    assert.deepEqual(readBootstrap(path), value);
    Deno.chmodSync(path, 0o644);
    assert.throws(() => readBootstrap(path));
    Deno.chmodSync(path, 0o600);
    Deno.symlinkSync(path, join(directory, "link"));
    assert.throws(() => readBootstrap(join(directory, "link")));
    Deno.writeTextFileSync(
      path,
      JSON.stringify({ ...value, unexpected: "field" }),
    );
    assert.throws(() => readBootstrap(path));
  } finally {
    Deno.removeSync(directory, { recursive: true });
  }
});

Deno.test("native env-file round-trip preserves fetched values and removes bootstrap", async () => {
  const directory = Deno.makeTempDirSync();
  try {
    const path = join(directory, "upstream.env");
    const source: Record<string, string> = {
      ...Deno.env.toObject(),
      ...fetched(),
      OP_SERVICE_ACCOUNT_TOKEN: "dummy-token",
      PATH: Deno.env.get("PATH")!,
    };
    const clean = projectUpstream(source, path);
    assert.equal(clean.OP_SERVICE_ACCOUNT_TOKEN, undefined);
    assert.equal(clean.RADARR_API_KEY, undefined);
    assert.equal(clean.TOOL_PROFILE, "full");
    assert.ok(!Deno.readTextFileSync(path).includes("TOOL_PROFILE"));
    assert.equal(Deno.statSync(path).mode! & 0o777, 0o600);
    const check = `if (Deno.env.get("RADARR_API_KEY") !== ${
      JSON.stringify(source.RADARR_API_KEY)
    } || Deno.env.get("OP_SERVICE_ACCOUNT_TOKEN")) throw new Error("Native dotenv changed the dummy value");`;
    const output = await new Deno.Command(Deno.execPath(), {
      args: ["eval", `--env-file=${path}`, check],
      clearEnv: true,
      env: clean,
      stdout: "piped",
      stderr: "piped",
    }).output();
    assert.equal(output.code, 0, new TextDecoder().decode(output.stderr));
    assert.throws(() => projectUpstream(source, path));
  } finally {
    Deno.removeSync(directory, { recursive: true });
  }
});

Deno.test("inherited values never satisfy a missing fetch; bad scope and injection fail", () => {
  const directory = Deno.makeTempDirSync();
  try {
    const clean = retrievalEnvironment({
      ...fetched(),
      OP_SESSION_OLD: "old",
      TELEGRAM_BOT_TOKEN: "old",
      PATH: "keep",
    }, "dummy");
    assert.deepEqual(clean, {
      PATH: "keep",
      TOOL_PROFILE: "full",
      OP_SERVICE_ACCOUNT_TOKEN: "dummy",
      OP_CONFIG_DIR: "/tmp/onepassword",
    });
    const path = join(directory, "upstream.env");
    assert.throws(() => projectUpstream(clean, path));
    assert.throws(() =>
      projectUpstream({ ...fetched(), RADARR_API_KEY: "x\nOTHER=y" }, path)
    );
    assert.throws(() =>
      projectUpstream({ ...fetched(), TELEGRAM_BOT_TOKEN: "wrong-scope" }, path)
    );
    assert.throws(() =>
      projectUpstream({ ...fetched(), TOOL_PROFILE: "minimal" }, path)
    );
    assert.equal(Array.from(Deno.readDirSync(directory)).length, 0);
    const { TMDB_API_KEY: _omitted, ...withoutTmdb } = fetched();
    projectUpstream(withoutTmdb, path);
    assert.ok(!Deno.readTextFileSync(path).includes("TMDB_API_KEY"));
  } finally {
    Deno.removeSync(directory, { recursive: true });
  }
});

for (const mode of ["running", "backoff", "forced-stop"]) {
  Deno.test(`app recovers with projected config and stops during ${mode}`, async () => {
    const directory = Deno.makeTempDirSync();
    const events = join(directory, "events");
    const native = join(directory, "native.ts");
    const wrapper = join(directory, "app.ts");
    const loader = join(directory, "start.ts");
    const cli = join(directory, "op.sh");
    const bootstrap = join(directory, "bootstrap.json");
    const fetches = join(directory, "fetches");
    let child: Deno.ChildProcess | undefined;
    try {
      Deno.writeTextFileSync(
        native,
        `
import assert from "node:assert/strict";
assert.ok(!Object.keys(Deno.env.toObject()).some(key => key.startsWith("OP_")));
assert.equal(Deno.env.get("RADARR_API_KEY"), ${
          JSON.stringify(fetched().RADARR_API_KEY)
        });
assert.equal(Deno.env.get("TOOL_PROFILE"), "full");
assert.deepEqual(Deno.args, [${JSON.stringify(mode)}, "--transport", "http"]);
const events = ${JSON.stringify(events)};
let previous = "";
try { previous = Deno.readTextFileSync(events); } catch { /* First launch. */ }
Deno.writeTextFileSync(events, String(Deno.pid) + "\\n", { append: true });
if (!previous || Deno.args[0] === "backoff") Deno.exit(1);
const stop = () => {
  if (Deno.args[0] === "forced-stop") return;
  console.log("native child stopped");
  Deno.exit(0);
};
Deno.addSignalListener("SIGTERM", stop);
Deno.addSignalListener("SIGINT", stop);
console.log("native child ready");
setInterval(() => {}, 1000);
`,
      );
      Deno.writeTextFileSync(
        wrapper,
        Deno.readTextFileSync("/app/managed/app.ts")
          .replace('"./runtime.ts"', '"/app/managed/runtime.ts"')
          .replaceAll(
            "/run/media-secrets/upstream.env",
            join(directory, "upstream.env"),
          )
          .replace("packages/media-server-mcp/src/index.ts", native),
      );
      Deno.writeTextFileSync(
        bootstrap,
        JSON.stringify({
          environment_id: "a".repeat(26),
          service_account_token: "dummy-bootstrap-token",
        }),
        { mode: 0o600 },
      );
      Deno.writeTextFileSync(
        cli,
        `set -eu
test "$1" = run
test "$2" = --environment
test "$4" = --
test "$OP_SERVICE_ACCOUNT_TOKEN" = dummy-bootstrap-token
printf 'fetched\\n' >> '${fetches}'
shift 4
${
          Object.entries(fetched()).map(([key, value]) =>
            `export ${key}='${value}'`
          ).join("\n")
        }
exec "$@"
`,
      );
      Deno.writeTextFileSync(
        loader,
        Deno.readTextFileSync("/app/managed/start.ts")
          .replace('"./runtime.ts"', '"/app/managed/runtime.ts"')
          .replace("/run/secrets/onepassword.json", bootstrap)
          .replace("/app/managed/app.ts", wrapper)
          .replace(
            'spawn("/usr/local/bin/op", [',
            `spawn("/bin/sh", [${JSON.stringify(cli)},`,
          ),
      );
      child = new Deno.Command(Deno.execPath(), {
        args: [
          "run",
          "--allow-read",
          "--allow-write",
          "--allow-env",
          "--allow-run",
          "--allow-sys",
          loader,
          mode,
          "--transport",
          "http",
        ],
        clearEnv: true,
        env: {
          ...Deno.env.toObject(),
          ...fetched(),
          OP_SERVICE_ACCOUNT_TOKEN: "dummy-bootstrap-token",
        },
        stdout: "piped",
        stderr: "piped",
      }).spawn();
      let stdout = "";
      const output = (async () => {
        for await (const chunk of child!.stdout) {
          stdout += new TextDecoder().decode(chunk);
        }
      })();
      let errorOutput = "";
      const stderr = new Response(child.stderr).text().then((text) =>
        errorOutput = text
      );
      const expected = mode === "backoff"
        ? "retrying in 1 seconds"
        : "native child ready";
      const deadline = Date.now() + 8000;
      while (!stdout.includes(expected) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      assert.ok(stdout.includes(expected), stdout + errorOutput);
      const started = Date.now();
      child.kill(mode === "running" ? "SIGINT" : "SIGTERM");
      let timeout: ReturnType<typeof setTimeout> | undefined;
      const status = await Promise.race([
        child.status,
        new Promise<never>((_, reject) => {
          timeout = setTimeout(
            () => reject(new Error("Shutdown did not finish")),
            5000,
          );
        }),
      ]).finally(() => clearTimeout(timeout));
      await output;
      assert.equal(status.code, 0, stdout + await stderr);
      assert.ok(Date.now() - started < (mode === "forced-stop" ? 5000 : 1000));
      const pids = Deno.readTextFileSync(events).trim().split("\n").map(Number);
      assert.equal(pids.length, mode === "backoff" ? 1 : 2);
      assert.equal(Deno.readTextFileSync(fetches), "fetched\n");
      for (const pid of pids) assert.throws(() => process.kill(pid, 0));
      if (mode === "running") {
        assert.ok(stdout.includes("native child stopped"));
      }
      assert.equal(
        Deno.statSync(join(directory, "upstream.env")).mode! & 0o777,
        0o600,
      );
    } finally {
      if (child) {
        try {
          child.kill("SIGTERM");
        } catch { /* Already stopped. */ }
        await child.status;
      }
      Deno.removeSync(directory, { recursive: true });
    }
  });
}
