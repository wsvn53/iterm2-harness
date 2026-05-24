# iTerm2 Harness

An **AI-friendly control surface for iTerm2**, built on iTerm2's official Python API. It exposes a small, self-describing HTTP API that lets an AI agent (Claude Code, GPT-based tools, custom scripts, …) automate your iTerm2 workspace — list windows / tabs / sessions, read screen contents with scrollback, send text and keystrokes, rename sessions, and so on.

Includes **simple but real authorization**: every new device must be approved via an iTerm2 modal alert; the resulting Bearer token is reused on subsequent requests. All actions are written to a daily JSON-line audit log so you can see exactly what your agent did.

Designed to be installed into iTerm2's `AutoLaunch` directory so the service starts with iTerm2.

```
   ┌──────────────────────────────────────────────────────────────┐
   │            Driver Agent  (OpenClaw / HermesAgent /           │
   │              Minis / your own orchestrator)                  │
   └──────────────┬───────────────────────────────────────────────┘
                  │  HTTP + Bearer token
                  ▼
        ┌────────────────────┐
        │  iterm2-harness    │   auth · audit · API directory
        │   (HTTP server)    │
        └─────────┬──────────┘
                  │  iTerm2 Python API
                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                          iTerm2.app                          │
   │ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────┐ │
   │ │ pane: claude │ │ pane: codex  │ │ pane: gemini │ │  …    │ │
   │ │  (Claude Code)│ │              │ │              │ │       │ │
   │ └──────────────┘ └──────────────┘ └──────────────┘ └───────┘ │
   │  list / read screen / send keys / rename · per native CLI    │
   └──────────────────────────────────────────────────────────────┘
```

The driver agent picks which pane runs which coding agent (Claude Code, Codex, Gemini CLI, OpenCode, …), sends prompts and keystrokes to each one, reads the screen back to track progress, and orchestrates handoffs between them — all without leaving the user's real iTerm2 workspace.

## Features

- HTTP REST API (no extra deps; uses iTerm2's bundled Python runtime).
- Bearer-token auth; new devices must be approved via an iTerm2 modal.
- Daily JSON-line audit logs at `~/.iterm2-harness/logs/`.
- Auto port fallback when the configured port is busy.
- macOS notification-center toast when the server starts.
- Progressive disclosure: every error response embeds the full API directory so clients can self-discover endpoints.
- `POST /api/v1/reload` to restart the script in place.

## Install

### Homebrew (recommended)

This repo ships its own formula under `Formula/iterm2-harness.rb`, so it can be installed via `brew tap` directly:

```bash
brew tap wsvn53/iterm2-harness https://github.com/wsvn53/iterm2-harness
brew install iterm2-harness
```

The formula automatically symlinks the script into iTerm2's `AutoLaunch` folder during `post_install`, so iTerm2 launches the service on its next start. Re-run or undo this any time with:

```bash
iterm2-harness-install              # (re-)create the AutoLaunch symlink
iterm2-harness-install --uninstall  # remove the symlink (keep the formula)
```

Upgrade later with `brew update && brew upgrade iterm2-harness`.

### From source

```bash
./install.sh
```

This creates a symlink at
`~/Library/Application Support/iTerm2/Scripts/AutoLaunch/iterm2-harness.py`
pointing at this repo's `iterm2-harness.py`. iTerm2 auto-runs scripts in
`AutoLaunch/` on launch.

To run immediately without restarting iTerm2: open the iTerm2 menu
**Scripts > AutoLaunch > iterm2-harness.py**.

### Other install options

```bash
./install.sh --copy                       # copy instead of symlink
./install.sh --source /path/to/file.py    # custom source (used by brew)
./install.sh --target /custom/dir         # custom AutoLaunch target
./install.sh --uninstall                  # remove from AutoLaunch
```

### Homebrew compatibility

The installer is brew-friendly. In a formula:

```ruby
def install
  prefix.install "iterm2-harness.py", "config.json", "install.sh"
end

def post_install
  system "#{prefix}/install.sh", "--source", "#{prefix}/iterm2-harness.py"
end
```

## Configuration

`config.json` next to the script (auto-created on first run):

```json
{
  "host": "0.0.0.0",
  "port": 6770,
  "file_access": {
    "enabled": false,
    "allowed_paths": []
  }
}
```

### File-access config

The file endpoints (`/api/v1/files*`) are **opt-in**:

- `file_access.enabled` — master switch (default `false`). When `false`, all file endpoints return `403`.
- `file_access.allowed_paths` — list of absolute path prefixes. A request path must `realpath()` under one of these. Empty list means *no restriction* (allow anywhere) once `enabled=true`. Always set realistic prefixes (e.g. `["/Users/me/Src", "/tmp"]`); the realpath check defeats `..` traversal.

After editing config.json, call `POST /api/v1/reload` to pick up changes.

Environment variables override the file:

| Variable | Default |
|---|---|
| `ITERM2_HARNESS_HOST` | `0.0.0.0` |
| `ITERM2_HARNESS_PORT` | `6770` |

If the port is already in use, the server scans up to 50 ports forward (6770 → 6771 → …). The actual bound port is reported in `/api/v1/health` and the API directory.

## Quick start

```bash
# 1. Health check (no auth)
curl -s http://127.0.0.1:6770/api/v1/health

# 2. Request authorization — iTerm2 will pop up a confirmation alert.
TOKEN=$(curl -s -X POST http://127.0.0.1:6770/api/v1/auth/request \
  -H 'Content-Type: application/json' \
  -d '{"device_name":"my-laptop"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# 3. Use the token from now on.
curl -s http://127.0.0.1:6770/api/v1/sessions \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

## Authorization

- All endpoints **except** `/api/v1/health` and `/api/v1/auth/request` require `Authorization: Bearer <token>`.
- Tokens are stored in `~/.iterm2-harness/tokens.json` (mode 0600). Surviving restarts.
- Every approval/denial/reject is recorded in the audit log.

## API

Base URL: `http://<host>:<port>` (default `0.0.0.0:6770`).

Calling any unknown path or wrong method returns the full API directory in the error body, so a client just needs to hit `/` to discover everything.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` `/api` `/api/v1` | – | Returns the API directory. |
| GET | `/api/v1/health` | – | `{status, host, port, server, _version}`. |
| POST | `/api/v1/auth/request` | – | Request a token (shows an alert). Body: `{"device_name": "..."}`. |
| GET | `/api/v1/auth/whoami` | ✓ | Validate the current token. |
| POST | `/api/v1/reload` | ✓ | Restart this script in place. |
| GET | `/api/v1/windows` | ✓ | Hierarchical windows → tabs → sessions. |
| GET | `/api/v1/sessions` | ✓ | Flat session list. Filterable. |
| GET | `/api/v1/sessions/{id}/screen` | ✓ | Screen contents, with paging. |
| GET | `/api/v1/sessions/{id}/metadata` | ✓ | Working dir, command line, job name, size. |
| POST | `/api/v1/sessions/{id}/send-text` | ✓ | Send text. Body: `{"text": "ls", "enter": true}`. `enter` defaults to false; when true, appends `\r`. |
| GET    | `/api/v1/files?path=...`        | ✓ | Read a file. Add `base64=true` for binary. Requires `file_access` in config. |
| POST   | `/api/v1/files?path=...`        | ✓ | Write a file. JSON body `{content,encoding,mkdir,append}` or `multipart/form-data` with field `file`. |
| DELETE | `/api/v1/files?path=...`        | ✓ | Delete a file (not a directory). |
| GET    | `/api/v1/files/list?path=...`   | ✓ | List a directory. Add `recursive=true` to walk. |
| POST | `/api/v1/sessions/{id}/send-key` | ✓ | Send a special key. Body: `{"key": "ctrl+c"}`. |

### `/api/v1/sessions` query filters

All filters are AND'd. Omit for the full list.

| Param | Description |
|---|---|
| `name` | Substring of session name (case-insensitive). |
| `job` | `jobName` (running process name). Note: some tools rename their process — e.g. Claude Code reports `2.1.132` instead of `claude`. |
| `command` | Substring of full `commandLine`. **Recommended for stable matching**, e.g. `command=claude`. |
| `path` | Working directory substring. |
| `regex=true` | Treat all four queries as regex patterns. |

Example: list every session currently running `claude`:

```bash
curl -s "http://127.0.0.1:6770/api/v1/sessions?command=claude" \
  -H "Authorization: Bearer $TOKEN"
```

### `/api/v1/sessions/{id}/screen` query

| Param | Default | Description |
|---|---|---|
| `limit` | `500` | Number of lines from the bottom. |
| `offset` | `0` | Skip this many lines from the bottom (for paging history). |
| `strip` | `false` | Collapse whitespace runs to a single space; drop empty lines. |

`has_more: true` in the response means more history exists — bump `offset` to read further back.

### Send-key supported keys

`enter` `return` `tab` `escape`/`esc` `space` `backspace` `delete`
`up` `down` `left` `right` `home` `end`
`ctrl+c` `ctrl+d` `ctrl+z` `ctrl+l` `ctrl+a` `ctrl+e` `ctrl+k` `ctrl+u` `ctrl+w` `ctrl+r` `ctrl+p` `ctrl+n`
plus any `ctrl+{a-z}` combination.

### Error responses

```json
{
  "error": "Not found: GET /api/v1/sesions",
  "hint": "Unknown path. See api.endpoints below for all available endpoints. ...",
  "api": { "service": "iterm2-harness", "version": "...", "endpoints": [ ... ] },
  "_version": "0.1.0"
}
```

## Files and data layout

```
<repo>/
  iterm2-harness.py        # the script (single file, no deps)
  config.json              # {host, port}; auto-created
  install.sh               # AutoLaunch installer

~/Library/Application Support/iTerm2/Scripts/AutoLaunch/
  iterm2-harness.py        # symlink (or copy) to the script

~/.iterm2-harness/
  tokens.json              # device → token map (mode 0600)
  logs/
    YYYY-MM-DD.log         # audit log, one JSON record per line
```

Audit events: `server.start`, `server.reload`, `server.port_busy`, `server.port_fallback`, `auth.request`, `auth.granted`, `auth.denied`, `auth.reject`, `request`, `error`, `notify.failed`.

## Security notes

- Default bind is `0.0.0.0` for LAN access. Set `host` to `127.0.0.1` in `config.json` to restrict to the local machine.
- Tokens are 256-bit URL-safe random strings.
- macOS Local Network permission may be requested on first use; allow it for iTerm2 if you want LAN clients.

## License

Apache License 2.0. See [LICENSE](LICENSE).
