# nodpi-supervisor

A small local CONNECT proxy that puts a **live switch** in front of [NoDPI], so you can
flip between *direct* and *through NoDPI* without restarting your client — and it
supervises NoDPI for you: starts it, watches it, restarts it, and keeps its logs.

No dependencies. Python standard library only.

```
client ──(HTTP(S)_PROXY → 127.0.0.1:8881)──► switch ──┬──► direct ──────────────► internet
                                                      │
                                                      └──► NoDPI :8883 ─────────► internet
                                                           (fragments the TLS
                                                            Client Hello)
```

## The problem it solves

Two separate annoyances, one tool:

1. **DPI resetting TLS 1.3 handshakes.** Some networks drop connections whose
   Client Hello carries post-quantum key shares (`X25519MLKEM768`). NoDPI works around it
   by fragmenting the Client Hello into pieces the filter cannot match. But the default
   tuning often is not enough — see [Tuning](#tuning-that-actually-mattered).
2. **Clients read proxy settings once.** Most apps resolve `HTTP_PROXY` at startup, so
   changing the proxy means a restart. Here the client keeps pointing at one fixed port and
   the *switch* decides per connection, reading `state.json` every time.

## Quick start

```bash
git clone <this repo> nodpi-supervisor
cd nodpi-supervisor

# NoDPI is a separate GPLv3 program — download its release and put the binary here:
#   ./nodpi/nodpi_switch.exe      (Windows)
#   ./nodpi/nodpi_switch          (Linux/macOS)

python net.py check      # what is in place, what is missing
python net.py start      # switch on :8881, NoDPI on :8883
python net.py status
```

Then point your client at the switch:

```bash
export HTTPS_PROXY=http://127.0.0.1:8881
export HTTP_PROXY=http://127.0.0.1:8881
```

Windows: set the same two variables in *System → Environment*, or point your app at
`127.0.0.1:8881` in its own proxy setting.

Switch traffic mode at any time — no restart of the client:

```bash
python net.py mode direct
python net.py mode nodpi
python net.py test            # CONNECT test through the current mode
```

## Commands

| Command | What it does |
|---|---|
| `net.py status` | components, mode, uptime, restarts, routes |
| `net.py start` | start the switch if needed and bring NoDPI up |
| `net.py stop` | stop NoDPI and the switch (turns the watchdog off) |
| `net.py restart` | stop, wait for ports, start again |
| `net.py mode nodpi\|direct` | switch the traffic mode |
| `net.py logs access\|error\|switch\|boot` | tails of the four log files |
| `net.py test [host]` | CONNECT test through the current mode |
| `net.py routes` | per-host exceptions |
| `net.py check` | environment self-check (exit 1 on problems) |

## Configuration

Everything has sane defaults; override with flags or environment variables.

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--port` | `SWITCH_PORT` | `8881` | proxy port your client points at |
| `--control-port` | `SWITCH_CONTROL_PORT` | `8891` | control API (loopback only) |
| `--nodpi-exe` | `NODPI_EXE` | `./nodpi/nodpi_switch*` | NoDPI binary |
| `--nodpi-port` | `NODPI_PORT` | `8883` | where NoDPI listens |
| `--fragment-size` | `NODPI_FRAGMENT_SIZE` | `32` | passed to NoDPI |
| `--fragment-offset` | `NODPI_FRAGMENT_OFFSET` | `100` | bytes left intact before fragmenting |
| — | `NODPI_ARGS` | — | full NoDPI command line, overrides all of the above |
| — | `NODPI_SWITCH_HOME` | directory of `net.py` | where state and logs live |

## Tuning that actually mattered

On a real RU ISP line the difference between "handshake works" and "the session hangs"
was the offset, not the fragment size:

| Config | Result |
|---|---|
| no fragmentation | handshake reset within ~1 s |
| `--fragment-size 1` (from the first byte) | handshake *passes*, then the session stalls; `curl` returns 200 while a streaming client hangs |
| `--fragment-size 10` | same, intermittent |
| **`--fragment-size 32 --fragment-offset 100`** | full session, streaming works |

Leaving the first 100 bytes intact keeps the TLS record headers and the SNI readable, so
middleboxes see a normal Client Hello; only the key-share part is split. Fragmenting from
byte zero trips anomaly heuristics.

Diagnosis is reproducible: if TLS 1.2 works and TLS 1.3 fails on the same host, the filter
is matching the PQ key share, not the SNI.

## Logs

| File | Content |
|---|---|
| `logs/switch.log` | connections, mode changes, supervision events (`adopt`, `crash`, `start`) |
| `logs/nodpi-access.log` | one line per closed connection: `time ip method host bytes_in bytes_out` (written by NoDPI via `--log-access`) |
| `logs/nodpi-error.log` | NoDPI tracebacks (`--log-error`) |
| `logs/nodpi.log` | startup banner and messages; the ANSI statistics panel is filtered out |

All files are capped at 2 MB and trimmed to their last 512 KB.

## Supervision rules

- NoDPI dies → the switch notices within ~3 s and restarts it (backoff 1→2→5→10 s).
- The switch dies → `lifecycle.Stack.start_watchdog()` brings it back while the caller is
  alive (the CLI does not watch; the optional panel does).
- **Three crashes in 60 s → stop retrying**, state `failed`, last error kept. A retry loop
  would hide the cause.
- A port held by a foreign process is never stolen: the start is refused with the port named.
- `desired.json` stores intent, so "Stop" also turns the watchdog off.

## Per-host routes

Some hosts need a different path than the global mode (for example through a Tor-backed
HTTP proxy). Copy `routes.example.json` to `routes.json`:

```json
{
  "example.com": "direct",
  "internal.example.org": "http://127.0.0.1:8118"
}
```

Keys match the host or its parent domain; the file is re-read on every connection, so no
restart is needed. A route that fails falls back to the normal path and is logged as
`route miss`.

## Optional: Hermes desktop panel

`integrations/hermes/` contains a drop-in panel for the Hermes desktop app: mode cards with
the current choice marked, component health, restart buttons and the three logs. It is a
thin facade over `lifecycle.Stack`, so it behaves exactly like the CLI.

See [integrations/hermes/README.md](integrations/hermes/README.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| client hangs on requests, `net.py test` fails | switch not running | `net.py start` |
| `handshake failed` / resets in `nodpi` mode but not `direct` | NoDPI down or wrong tuning | `net.py status`, `net.py logs error` |
| `port 8881 is held by another process` | an old proxy still bound | stop it, then `net.py start` |
| mode switch has no effect | client points somewhere else | check `HTTP(S)_PROXY` points at the switch port |
| `failed` state after restarts | crash loop, cause in the log | `net.py logs error`, fix, then `net.py start` |

## Requirements

- Python 3.9+ (no packages to install)
- NoDPI released binary (GPLv3). Not bundled: download it from its own project.

## License

MIT — see [LICENSE](LICENSE). NoDPI itself is GPLv3 and is a separate program: this project
only starts it as a child process and never links against or redistributes it.

[NoDPI]: https://github.com/GVCoder09/NoDPI
