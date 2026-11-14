"""Run inside a container on the bot's network: it reaches the egress proxy and nothing else."""

import socket
import sys
import urllib.error
import urllib.request

PROXY = ("media-egress", 3128)
host_address, host_port, expect = sys.argv[1], int(sys.argv[2]), sys.argv[3]


def opens(address: str, port: int) -> bool:
    try:
        socket.create_connection((address, port), 4).close()
    except OSError:
        return False
    return True


def proxy_status(request: str) -> int:
    with socket.create_connection(PROXY, 20) as connection:
        connection.sendall(f"{request}\r\n\r\n".encode())
        return int(connection.recv(64).split()[1])


# The control container sits on an ordinary network and must reach the host
# listener. Otherwise a blocked listener would prove nothing about the bot.
if expect == "open":
    sys.exit(0 if opens(host_address, host_port) else "control could not reach the host")

failures = []
if opens(host_address, host_port):
    failures.append("the host is reachable at its own address")
if opens("172.30.40.1", host_port):
    failures.append("the host is reachable at the bridge address")
if opens("1.1.1.1", 443):
    failures.append("the internet is reachable without the proxy")
if opens("media-gateway", 18083):
    failures.append("the dashboard, which has no login, is reachable from the bot's network")
try:
    socket.getaddrinfo("example.com", 443)
    failures.append("an outside name resolves without the proxy")
except OSError:
    pass
for request in (
    "CONNECT example.com:443 HTTP/1.1",
    "CONNECT api.telegram.org:80 HTTP/1.1",
    f"CONNECT {host_address}:{host_port} HTTP/1.1",
    "CONNECT 192.168.1.1:443 HTTP/1.1",
    "GET http://example.com/ HTTP/1.1",
):
    if proxy_status(request) != 403:
        failures.append(f"the proxy did not refuse: {request}")
# An allowed host must work end to end: Telegram answers 404 to an unknown token.
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": f"http://{PROXY[0]}:{PROXY[1]}"})
)
try:
    opener.open("https://api.telegram.org/botprobe/getMe", timeout=30)
    failures.append("Telegram accepted an unknown token")
except urllib.error.HTTPError as error:
    if error.code not in (401, 404):
        failures.append(f"Telegram answered {error.code} through the proxy")
except OSError as error:
    failures.append(f"Telegram is unreachable through the proxy: {type(error).__name__}")
sys.exit("; ".join(failures) if failures else 0)
