---
name: iterm2-harness
description: Control iTerm2 over an authenticated HTTP API — list windows/tabs/sessions (with filters by name, command, path), read screen content with scrollback paging, send text and keystrokes. Use when the user wants to inspect or drive iTerm2 sessions remotely, find which session is running a given command (e.g. claude, ssh, vim), or pipe terminal output between sessions.
allowed-tools: Bash(curl:*), Read
---

# iTerm2 Harness

HTTP API exposed by the `iterm2-harness.py` script running inside iTerm2. Provides authenticated, audited remote control over iTerm2 windows/tabs/sessions.

## Prerequisites

- The script must be running. If `curl "$ITERM2_HARNESS_URL/api/v1/health"` succeeds, it's up. Otherwise tell the user to launch it from **iTerm2 > Scripts > AutoLaunch > iterm2-harness.py**, or to install it via `./install.sh` from the repo.

## Configuring the base URL

The base URL is **not hardcoded**. Read it from the environment variable `ITERM2_HARNESS_URL`. The host/port the server is bound to depends on the user's `config.json` and on port-fallback (6770 → 6771 → … if busy).

```bash
BASE_URL="${ITERM2_HARNESS_URL:-http://127.0.0.1:6770}"
```

If `ITERM2_HARNESS_URL` is not set, prompt the user once to export it. Suggested guidance:

```bash
# Add this to your ~/.zshrc or ~/.bashrc:
export ITERM2_HARNESS_URL=http://127.0.0.1:6770

# Or for LAN access from another machine:
export ITERM2_HARNESS_URL=http://<host-machine-ip>:6770
```

Verify the URL works before doing anything else:

```bash
curl -sf "$BASE_URL/api/v1/health" || echo "Server unreachable at $BASE_URL"
```

If the user doesn't know their port, tell them to check the macOS notification toast that appeared on iTerm2 startup ("iterm2-harness started — v0.1.0 listening on …"), or run `curl $BASE_URL/api/v1/health` (and 6771, 6772, … if it fails) until they find it.

## Authentication

Every endpoint **except** `/api/v1/health` and `/api/v1/auth/request` requires a Bearer token.

### Client-side token cache

Cache the token at `~/.iterm2-harness-client.json` so future sessions reuse it instead of asking the user to approve again. **Always check the cache first**; only call `/auth/request` if no valid token exists there.

Cache file format (mode 0600):

```json
{
  "host": "127.0.0.1",
  "port": 6770,
  "token": "...",
  "device_name": "claude-cli"
}
```

### Bootstrap flow

```bash
CACHE="$HOME/.iterm2-harness-client.json"

# 1. Try cached token; validate with /auth/whoami.
if [ -f "$CACHE" ]; then
  TOKEN=$(python3 -c "import json;print(json.load(open('$CACHE'))['token'])")
  if curl -sf -H "Authorization: Bearer $TOKEN" \
       $BASE_URL/api/v1/auth/whoami >/dev/null; then
    echo "Reusing cached token"
  else
    TOKEN=""
  fi
fi

# 2. No valid cached token -> request one. iTerm2 pops a confirmation alert.
if [ -z "$TOKEN" ]; then
  RESP=$(curl -s -X POST $BASE_URL/api/v1/auth/request \
    -H 'Content-Type: application/json' \
    -d '{"device_name":"claude-cli"}')
  TOKEN=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
  python3 -c "import json,os; \
    json.dump({'host':'127.0.0.1','port':6770,'token':'$TOKEN','device_name':'claude-cli'}, \
              open('$CACHE','w')); os.chmod('$CACHE', 0o600)"
fi

# 3. Use the token.
curl -s -H "Authorization: Bearer $TOKEN" $BASE_URL/api/v1/sessions
```

## Discovery

Hit the root path to get the full API directory in JSON. **All error responses also include this directory under `api`**, so you never need to guess endpoints.

```bash
curl -s $BASE_URL/
```

## Common operations

### List all sessions

```bash
curl -s $BASE_URL/api/v1/sessions \
  -H "Authorization: Bearer $TOKEN"
```

### Find sessions by what they're running (recommended)

`name` is the visible session label; `command` matches the actual `commandLine`. Use `command` for stable matches, since some tools rename their process (Claude Code reports its version like `2.1.132` for `jobName`).

```bash
# Sessions running claude
curl -s "$BASE_URL/api/v1/sessions?command=claude" \
  -H "Authorization: Bearer $TOKEN"

# Sessions in a specific repo
curl -s "$BASE_URL/api/v1/sessions?path=MyProject" \
  -H "Authorization: Bearer $TOKEN"

# Combine (AND) — claude sessions in MyProject
curl -s "$BASE_URL/api/v1/sessions?command=claude&path=MyProject" \
  -H "Authorization: Bearer $TOKEN"

# Regex
curl -s "$BASE_URL/api/v1/sessions?regex=true&name=^Claude/" \
  -H "Authorization: Bearer $TOKEN"
```

Filter params (all AND'd):
- `name` — session name substring (case-insensitive)
- `job` — jobName substring (process name)
- `command` — full commandLine substring (most stable)
- `path` — working directory substring
- `regex=true` — treat the above as regex

When any of `job`/`command`/`path` is set, the response entries also include `job_name`, `command_line`, `path` for confirmation.

### Read screen contents

```bash
# Latest 200 lines, with whitespace collapsed
curl -s "$BASE_URL/api/v1/sessions/$SID/screen?limit=200&strip=true" \
  -H "Authorization: Bearer $TOKEN"
```

Response includes `lines`, `fetched_lines`, `returned_lines`, `offset`, `has_more`. Page through history by increasing `offset` while `has_more=true`:

```bash
# Page back further
curl -s "$BASE_URL/api/v1/sessions/$SID/screen?limit=200&offset=200" \
  -H "Authorization: Bearer $TOKEN"
```

Tips:
- Default `strip=false` preserves layout (good for TUI dumps).
- `strip=true` collapses runs of whitespace into a single space and drops blank lines (good for grepping prose).

### Get session metadata

```bash
curl -s "$BASE_URL/api/v1/sessions/$SID/metadata" \
  -H "Authorization: Bearer $TOKEN"
# -> path, command_line, job_name, name, columns, rows
```

### Send text / commands

`text` is sent as-is. The optional **`enter`** flag (default `false`) appends `\r` to trigger execution — use it when you want the command to run immediately. Leave it off when you only want to type into a prompt without submitting.

```bash
# Type without executing (e.g. fill an input but let the user review)
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-text" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"echo hello"}'

# Type AND execute immediately — typical for shell automation
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-text" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"echo hello","enter":true}'
```

If you sent text without `enter` and decide to submit afterwards, fire a separate Enter via send-key:

```bash
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-key" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"key":"enter"}'
```

Guidelines:
- Running a shell command (`ls`, `git status`, `npm run build`) → set `enter:true`.
- Filling a TUI/REPL prompt where the user might still want to edit → leave `enter` off, then send Enter (or another key like `tab`/`escape`) explicitly when ready.
- Never embed a literal `\r` inside `text` — use `enter:true` instead.

### Send a special key

```bash
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-key" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"key":"ctrl+c"}'
```

Supported keys: `enter`/`return`, `tab`, `escape`/`esc`, `space`, `backspace`, `delete`, `up`, `down`, `left`, `right`, `home`, `end`, `ctrl+c`, `ctrl+d`, `ctrl+z`, `ctrl+l`, `ctrl+a`, `ctrl+e`, `ctrl+k`, `ctrl+u`, `ctrl+w`, `ctrl+r`, `ctrl+p`, `ctrl+n`, plus any `ctrl+{a-z}`.

### List windows hierarchically

```bash
curl -s $BASE_URL/api/v1/windows \
  -H "Authorization: Bearer $TOKEN"
```

### Reload (restart) the service

```bash
curl -s -X POST $BASE_URL/api/v1/reload \
  -H "Authorization: Bearer $TOKEN"
# Service is back online ~1-2s later.
```

## Workflow recipes

### "What is Claude doing in the other terminal right now?"

```bash
# 1. Find the claude session(s)
SID=$(curl -s "$BASE_URL/api/v1/sessions?command=claude" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['sessions'][0]['session_id'])")

# 2. Read the recent screen
curl -s "$BASE_URL/api/v1/sessions/$SID/screen?limit=80&strip=true" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys;[print(l) for l in json.load(sys.stdin)['lines']]"
```

### "Cancel a runaway command in another session"

```bash
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-key" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"key":"ctrl+c"}'
```

### "Run a command and capture output"

```bash
# Send the command (enter:true so it runs immediately)
curl -s -X POST "$BASE_URL/api/v1/sessions/$SID/send-text" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text":"my-long-command","enter":true}'

# Wait, then read
sleep 5
curl -s "$BASE_URL/api/v1/sessions/$SID/screen?limit=200" \
  -H "Authorization: Bearer $TOKEN"
```

## Error handling

Errors return JSON with `error`, `hint`, and a full `api` directory describing every endpoint. If you hit a 404 or 405, parse the `api.endpoints` field to discover the correct route.

```json
{
  "error": "Not found: GET /api/v1/sesions",
  "hint": "Unknown path. See api.endpoints below ...",
  "api": { "endpoints": [ { "method":"GET", "path":"/api/v1/sessions", ... } ] }
}
```

