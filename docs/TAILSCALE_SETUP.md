# Tailscale Serve setup

The bridge listens on plain HTTP at `127.0.0.1:8000` only. **Tailscale Serve**
terminates TLS with an automatically issued certificate and proxies requests
from your tailnet to that loopback port.

```text
iPhone (Tailscale on)
   │ HTTPS  https://<machine>.<tailnet>.ts.net:10000
   ▼
Tailscale Serve  (TLS, tailnet only)
   │ HTTP
   ▼
127.0.0.1:8000   git-bridge
```

**Never use Tailscale Funnel for this service.** Funnel publishes to the
public Internet.

Commands below were verified against Tailscale **1.102** (`tailscale version`).
The Serve CLI changed in 1.52; on older versions upgrade first.

## 1. Install and sign in

- Windows / macOS: install from <https://tailscale.com/download> and sign in.
- Linux: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
- iPhone: install *Tailscale* from the App Store and sign in to the **same**
  tailnet.

## 2. Machine name

The machine name becomes part of the HTTPS hostname
(`<machine>.<tailnet>.ts.net`). Pick a non-sensitive name. Rename in the admin
console (*Machines → … → Edit machine name*) or with:

```sh
tailscale set --hostname=git-bridge
```

The reference deployment uses the existing name `cave`:
`https://cave.tail364856.ts.net:10000`.

## 3. Enable HTTPS certificates (once per tailnet)

Admin console → **DNS** → enable **MagicDNS** and **HTTPS Certificates**.

Certificate and DNS implications:

- Certificates are issued by Let's Encrypt for `<machine>.<tailnet>.ts.net`.
  **Certificate Transparency logs are public**, so the machine name and tailnet
  name become publicly visible. That is why the name should be non-sensitive.
  It does not make the service reachable: the name resolves only to a
  `100.x.y.z` tailnet address.
- Renaming the machine changes the hostname; update the Shortcuts afterwards.

## 4. Proxy the bridge

Start the bridge first (see [DEPLOYMENT.md](DEPLOYMENT.md)), then:

```sh
tailscale serve --bg --https=10000 http://127.0.0.1:8000
```

- `--bg` makes the configuration persistent (survives reboots).
- `--https=10000` gives the bridge its own HTTPS port, leaving any existing
  Serve entries (such as port 443) untouched. Port 443 works too if nothing
  else uses it: `tailscale serve --bg http://127.0.0.1:8000`.
- On Linux, run with `sudo` or once `sudo tailscale set --operator=$USER`.

Check:

```sh
tailscale serve status
```

Expected (other entries may also be listed):

```text
https://cave.tail364856.ts.net:10000 (tailnet only)
|-- / proxy http://127.0.0.1:8000
```

**`(tailnet only)` must appear on every line.** If any entry says
`(Funnel on)`, run `tailscale funnel reset` immediately.

## 5. Verify

| Check | Command / action | Expected |
|---|---|---|
| Local HTTP | `curl http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| HTTPS in tailnet | `curl https://cave.tail364856.ts.net:10000/health` from any tailnet device | `{"status":"ok"}` |
| Auth enforced | `curl https://…:10000/repos/rot3k/status` without token | HTTP 401 |
| Not on LAN | from another LAN device: `curl http://<LAN-IP>:8000/health` | connection refused |
| Not on tailnet IP | `curl http://100.x.y.z:8000/health` | connection refused |
| Not public | `tailscale funnel status` | every entry `(tailnet only)` |
| Not public (phone) | iPhone: turn Tailscale **off**, open the HTTPS URL on mobile data | fails to connect |
| From iPhone | Tailscale **on**, Safari → `https://cave.tail364856.ts.net:10000/health` | `{"status":"ok"}` |

Also confirm:

- No router port forwarding exists for 8000 or 10000.
- The listen address in the configuration is `127.0.0.1` (the bridge refuses
  anything else and `git-bridge check` reports it).

Results on the reference machine (2026-09-28): local and tailnet HTTPS health
OK; 401 without token; `192.168.68.106:8000` and `100.93.58.16:8000` refused;
Serve entries `(tailnet only)`.

## 6. Stop or remove

```sh
tailscale serve --https=10000 off     # remove only the bridge entry
tailscale serve status                # confirm
```

`tailscale serve reset` removes **all** Serve entries on the machine, including
unrelated ones; prefer the targeted `off`.

## Optional hardening: tailnet ACLs

Restrict who may reach port 10000 on the bridge machine, e.g. only your own
devices:

```json
{
  "grants": [
    {"src": ["autogroup:member"], "dst": ["tag:git-bridge"], "ip": ["tcp:10000"]}
  ]
}
```

(tag the machine `tag:git-bridge`). For a single-user tailnet the default
policy is usually sufficient.

## Future: identity instead of a bearer token

Tailscale Serve adds `Tailscale-User-Login` and related headers to proxied
requests from tailnet users. The bridge's `Authenticator` interface is where an
identity check based on those headers could replace the bearer token; it is
not enabled today because only Serve (not arbitrary local processes) can be
trusted to set them.
