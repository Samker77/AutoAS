# Web UI & Monitoring

Every interactive Arbor run exposes two live views of the same study: a **terminal
dashboard** and a **browser monitor (Web UI)**. Both read from the same event stream, so
they always agree.

## The terminal dashboard

When you start a run in a terminal, Arbor renders a live dashboard showing the current
cycle, the Idea Tree, costs, and the agent's thinking/tool stream. You interact with it via
[slash commands](cli.md#interactive-slash-commands) — `/status`, `/tree`, `/evidence`,
`/steer`, `/pause`, and so on.

Disable live terminal input with `--no-dashboard-input` (prompts and review gates then
auto-continue after a timeout — useful for unattended runs).

## The browser monitor (Web UI)

For interactive runs, Arbor also starts a small web server that mirrors the run to your
browser. It renders a snapshot of the run state plus the live thinking/tool stream over
Server-Sent Events, so you can watch progress on a second screen or share a link with
collaborators on the same network.

The URL is printed in the dashboard header once the server binds, e.g.
`http://127.0.0.1:8765`.

### Ports

| Behaviour | Detail |
| --- | --- |
| **Default port** | `8765`. |
| **Auto-roll** | If `8765` is busy, Arbor walks forward up to 10 ports (`8765`–`8774`) until one binds. |
| **Pick a port** | `--webui-port N` (or `ui.webui_port` in config) sets an explicit port. An explicit port is tried exactly once — a busy port is surfaced rather than silently moved. |
| **Disable** | `--no-webui` skips the browser monitor entirely. |

```bash
arbor --webui-port 9000      # serve the monitor on :9000
arbor --no-webui             # no browser monitor at all
```

### Read-only vs. interactive

By default the Web UI is **read-only** — the browser only observes. In an interactive run
(a TTY, without `--no-dashboard-input`) the monitor also becomes **interactive**, letting
you from the browser:

- **Ask** the read-only companion a question about the run,
- **Steer** the research agent by injecting a message, and
- **Answer** human-in-the-loop gates (e.g. approve/edit ideas in `review` mode).

Interactive browser actions are protected by a per-run token in the URL, so only someone
with the printed link can drive the run. If you want a purely passive monitor, start the
run with `--no-dashboard-input` (or simply use `--no-webui`).

!!! note "Headless / scripted runs"
    Non-interactive runs (no TTY, or launched with `--yes`) don't need a browser monitor.
    Use `--no-webui` to skip it, and rely on `REPORT.md` and the session logs instead.

## Which view should I use?

| You want to… | Use |
| --- | --- |
| Drive the run, type commands, approve ideas | Terminal dashboard (slash commands) |
| Watch progress on a second screen / share a link | Web UI |
| Run unattended in CI or a script | `--no-webui --no-dashboard-input` |

Both views are optional conveniences layered on top of the same durable artifacts — the
Idea Tree, checkpoints, and `REPORT.md`. See [Outputs & Resume](outputs-and-resume.md).

## No-login remote experiment console

`arbor serve` puts a control plane in front of the existing WebUI. It opens directly without
an account/password prompt and can:

- create and stop experiments inside a configured workspace root;
- select both live and historical Sessions;
- use **Ask**, **Steer**, and review-node approval/edit controls on live runs; and
- inspect completed Sessions as durable read-only views.

```bash
arbor serve \
  --workspace-root /srv/arbor-workspace \
  --host 127.0.0.1 \
  --port 8765 \
  --no-open
```

The recommended remote path is an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

Then open `http://127.0.0.1:8765` locally. The console launches each child run with its existing
interactive WebUI on an ephemeral **loopback-only** port, then proxies its SSE and input
channels through the same-origin Session route. Provider API keys remain in
the child environment and are never returned to the browser or written to the console's
control metadata.

The browser automatically receives an HMAC-signed, HttpOnly, SameSite=Strict session cookie
and a CSRF token; the session expires after eight hours by default. If you bind to `0.0.0.0`,
put the console behind an HTTPS reverse proxy and add `--secure-cookie`. Anyone who knows the
URL can control experiments, so a public deployment should restrict access at the proxy,
firewall, VPN, or tunnel layer.
Only expose the console port in the server firewall; do not expose child WebUI ports.

Stopping the console itself does not stop active experiments. Use the **Stop** action in the
web page when you intend to terminate a run.
