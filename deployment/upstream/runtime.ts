import { closeSync, constants, fstatSync, openSync, readSync } from "node:fs";
import { Buffer } from "node:buffer";
import process from "node:process";

export const requiredKeys = [
  "MCP_AUTH_TOKEN",
  "RADARR_URL",
  "RADARR_API_KEY",
  "SONARR_URL",
  "SONARR_API_KEY",
  "PLEX_URL",
  "PLEX_API_KEY",
  "TMDB_API_KEY",
  "TOOL_PROFILE",
];
export const appKeys = new Set([
  ...requiredKeys,
  "TOOL_BRANCHES",
  "TOOL_EXCLUDE",
  "TOOL_INCLUDE",
]);
const forbiddenKeys = new Set([
  "MEDIA_GATEWAY_TOKEN",
  "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH",
  "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY",
  "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN",
  "TELEGRAM_BOT_TOKEN",
  "CRBL_MEDIA_GATEWAY_TOKEN",
]);

export function readBootstrap(
  path: string,
): { environment_id: string; service_account_token: string } {
  // Deno 2.6's Node fs adapter does not enforce O_NOFOLLOW. Compare the
  // opened descriptor with a non-following stat of the protected mount.
  const before = Deno.lstatSync(path);
  if (!before.isFile || before.isSymlink) {
    throw new Error("Invalid bootstrap file");
  }
  const descriptor = openSync(
    path,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  let content: string;
  try {
    const info = fstatSync(descriptor);
    if (
      !info.isFile() || info.ino !== before.ino || info.dev !== before.dev ||
      info.uid !== process.getuid!() || (info.mode & 0o077)
    ) {
      throw new Error("Invalid bootstrap permissions");
    }
    const buffer = Buffer.alloc(16385);
    const length = readSync(descriptor, buffer, 0, buffer.length, 0);
    if (length > 16384) throw new Error("Oversized bootstrap");
    content = buffer.subarray(0, length).toString("utf8");
  } finally {
    closeSync(descriptor);
  }
  const data = JSON.parse(content);
  if (
    !data || typeof data !== "object" || Array.isArray(data) ||
    Object.keys(data).sort().join(",") !==
      "environment_id,service_account_token" ||
    typeof data.environment_id !== "string" ||
    !/^[a-z2-7]{26}$/.test(data.environment_id) ||
    typeof data.service_account_token !== "string" ||
    !data.service_account_token ||
    /[\s\x00-\x1f]/.test(data.service_account_token)
  ) {
    throw new Error("Invalid bootstrap fields");
  }
  return data;
}

export function retrievalEnvironment(
  source: Record<string, string>,
  token: string,
): Record<string, string> {
  const environment = Object.fromEntries(
    Object.entries(source).filter(([key]) =>
      !key.startsWith("OP_") && !appKeys.has(key) && !forbiddenKeys.has(key)
    ),
  );
  environment.OP_SERVICE_ACCOUNT_TOKEN = token;
  environment.OP_CONFIG_DIR = "/tmp/onepassword";
  return environment;
}

export function projectUpstream(
  source: Record<string, string>,
  path: string,
): Record<string, string> {
  if (
    Object.keys(source).some((key) => forbiddenKeys.has(key)) ||
    requiredKeys.some((key) => !source[key]?.trim()) ||
    source.MCP_AUTH_TOKEN.length < 32 ||
    source.TOOL_PROFILE !== "full"
  ) {
    throw new Error("Incomplete upstream runtime fields");
  }
  const entries = Object.entries(source).filter(([key]) => appKeys.has(key));
  if (
    entries.some(([, value]) => /[\r\n\0]/.test(value) || value.includes("${"))
  ) {
    throw new Error("Invalid upstream runtime value");
  }
  const text = entries.map(([key, value]) =>
    `${key}=${JSON.stringify(value).replaceAll("$", "\\$")}\n`
  ).join("");
  Deno.writeTextFileSync(path, text, { mode: 0o600, createNew: true });
  return Object.fromEntries(
    Object.entries(source).filter(([key]) =>
      !key.startsWith("OP_") && !appKeys.has(key)
    ),
  );
}
