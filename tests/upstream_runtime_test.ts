// Run in the pinned managed upstream image; no external packages or network.
import assert from "node:assert/strict";
import { join } from "node:path";
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
    assert.throws(() => projectUpstream({ ...fetched(), TOOL_PROFILE: "minimal" }, path));
    assert.equal(Array.from(Deno.readDirSync(directory)).length, 0);
    const { TMDB_API_KEY: _omitted, ...withoutTmdb } = fetched();
    projectUpstream(withoutTmdb, path);
    assert.ok(!Deno.readTextFileSync(path).includes("TMDB_API_KEY"));
  } finally {
    Deno.removeSync(directory, { recursive: true });
  }
});
