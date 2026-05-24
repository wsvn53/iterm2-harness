#!/usr/bin/env python3
"""
iterm2-harness — HTTP API for remote-controlling iTerm2 with auth and audit.

Endpoints:
  POST /api/v1/auth/request                      - request authorization (shows alert)
  GET  /api/v1/auth/whoami                       - validate current token
  GET  /api/v1/health                            - health check (no auth)
  POST /api/v1/reload                            - reload (restart) this script
  GET  /api/v1/windows                           - list windows / tabs / sessions
  GET  /api/v1/sessions                          - flat list of sessions (filterable)
  GET  /api/v1/sessions/{id}/screen              - read screen contents
  GET  /api/v1/sessions/{id}/metadata            - session metadata
  POST /api/v1/sessions/{id}/send-text           - send text
  POST /api/v1/sessions/{id}/send-key            - send key
  POST /api/v1/sessions/{id}/set-title           - set session title
  GET    /api/v1/files?path=...                  - read a file
  POST   /api/v1/files?path=...                  - write a file (json or multipart)
  DELETE /api/v1/files?path=...                  - delete a file
  GET    /api/v1/files/list?path=...             - list a directory

Auth:    every request except /health and /auth/request needs Authorization: Bearer <token>
Storage: ~/.iterm2-harness/tokens.json
Audit:   ~/.iterm2-harness/logs/YYYY-MM-DD.log
"""

import asyncio
import json
import os
import re
import secrets
import urllib.parse
from datetime import datetime
from pathlib import Path

import iterm2

VERSION = "0.1.0"
MAX_BODY_SIZE = 32 * 1024 * 1024  # 32 MB; covers JSON bodies and file uploads.

# realpath() so that when this script is installed as a symlink under iTerm2's
# AutoLaunch folder, config.json is still read from the actual source dir
# (the brew prefix or the cloned repo), not from AutoLaunch itself.
SCRIPT_DIR = Path(os.path.dirname(os.path.realpath(__file__)))
CONFIG_FILE = SCRIPT_DIR / "config.json"

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 6770,
    "file_access": {
        "enabled": True,
        "allowed_paths": [],
    },
}

HOME_DIR = Path(os.path.expanduser("~/.iterm2-harness"))
TOKENS_FILE = HOME_DIR / "tokens.json"
LOGS_DIR = HOME_DIR / "logs"


def load_config():
    """Load config.json, fall back to defaults; create the file if missing."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except Exception:
            pass
    else:
        try:
            CONFIG_FILE.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass
    # Env vars take precedence for ad-hoc overrides.
    host = os.environ.get("ITERM2_HARNESS_HOST", cfg.get("host", DEFAULT_CONFIG["host"]))
    port = int(os.environ.get("ITERM2_HARNESS_PORT", cfg.get("port", DEFAULT_CONFIG["port"])))
    file_access = cfg.get("file_access") or {}
    return host, port, file_access


HOST, PORT, FILE_ACCESS = load_config()

KEY_MAP = {
    "enter": "\r", "return": "\r",
    "ctrl+c": "\x03", "ctrl+d": "\x04", "ctrl+z": "\x1a", "ctrl+l": "\x0c",
    "ctrl+a": "\x01", "ctrl+e": "\x05", "ctrl+k": "\x0b", "ctrl+u": "\x15",
    "ctrl+w": "\x17", "ctrl+r": "\x12", "ctrl+p": "\x10", "ctrl+n": "\x0e",
    "tab": "\t", "escape": "\x1b", "esc": "\x1b",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F",
    "backspace": "\x7f", "delete": "\x1b[3~", "space": " ",
}

_connection = None
_auth_lock = asyncio.Lock()


# ─── Storage and audit ─────────────────────────────────────

def _ensure_dirs():
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME_DIR, 0o700)
    except Exception:
        pass


def _load_tokens():
    if not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(TOKENS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_tokens(tokens):
    _ensure_dirs()
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, TOKENS_FILE)
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except Exception:
        pass


def audit(event, **fields):
    """Append a JSON line to today's audit log."""
    _ensure_dirs()
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"{today}.log"
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── HTTP protocol ─────────────────────────────────────────

async def read_http_request(reader):
    request_line = await asyncio.wait_for(reader.readline(), timeout=30)
    if not request_line:
        return None
    parts = request_line.decode("utf-8").strip().split(" ")
    if len(parts) < 3:
        return None
    method = parts[0].upper()
    parsed = urllib.parse.urlparse(parts[1])
    path = parsed.path
    query_params = dict(urllib.parse.parse_qsl(parsed.query))

    headers = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        line = line.decode("utf-8").strip()
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    body = b""
    content_length = int(headers.get("content-length", 0))
    if content_length > MAX_BODY_SIZE:
        raise ValueError("Request body too large")
    if content_length > 0:
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=30)

    return method, path, query_params, headers, body


def make_response(status_code, data):
    data["_version"] = VERSION
    status_text = {
        200: "OK", 201: "Created", 400: "Bad Request",
        401: "Unauthorized", 403: "Forbidden",
        404: "Not Found", 405: "Method Not Allowed",
        409: "Conflict", 500: "Internal Server Error",
    }
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    header = (
        f"HTTP/1.1 {status_code} {status_text.get(status_code, 'Unknown')}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    )
    return header.encode("utf-8") + body


# ─── API description (for progressive disclosure) ──────────

API_ENDPOINTS = [
    {
        "method": "GET", "path": "/api/v1/health",
        "auth": False,
        "summary": "Health check; returns service status.",
    },
    {
        "method": "POST", "path": "/api/v1/auth/request",
        "auth": False,
        "summary": "Request authorization for a new device. iTerm2 shows a confirmation alert; on approval a token is returned.",
        "body": {"device_name": "string, client device name"},
        "example": {"device_name": "my-laptop"},
        "response": {"token": "auth token", "device_name": "string"},
    },
    {
        "method": "POST", "path": "/api/v1/reload",
        "auth": True,
        "summary": "Reload (restart) this script. The server disconnects and re-launches itself.",
    },
    {
        "method": "GET", "path": "/api/v1/auth/whoami",
        "auth": True,
        "summary": "Validate the current token and return the owning device info.",
    },
    {
        "method": "GET", "path": "/api/v1/windows",
        "auth": True,
        "summary": "Hierarchical listing of windows -> tabs -> sessions.",
    },
    {
        "method": "GET", "path": "/api/v1/sessions",
        "auth": True,
        "summary": "Flat list of sessions (with parent window_id / tab_id). Filterable by name, job, command, path.",
        "query": {
            "name": "Filter by session name substring (case-insensitive).",
            "job": "Filter by jobName. Note: some tools rename their process (e.g. claude reports its version like '2.1.132').",
            "command": "Filter by full commandLine. Recommended for stable matching, e.g. command=claude.",
            "path": "Filter by working directory substring.",
            "regex": "When true, treat the above queries as regex patterns instead of substrings.",
        },
    },
    {
        "method": "GET", "path": "/api/v1/sessions/{session_id}/screen",
        "auth": True,
        "summary": "Read terminal screen contents (including scrollback).",
        "query": {
            "limit": "Number of lines to return (from the bottom). Default 500.",
            "offset": "Skip this many lines from the bottom before taking limit; use to page through history. Default 0.",
            "strip": "When true, collapse runs of horizontal whitespace (spaces/tabs) into a single space and drop empty lines; line breaks are preserved. Default false.",
        },
    },
    {
        "method": "GET", "path": "/api/v1/sessions/{session_id}/metadata",
        "auth": True,
        "summary": "Get session metadata (working directory, command line, job name, grid size).",
    },
    {
        "method": "POST", "path": "/api/v1/sessions/{session_id}/send-text",
        "auth": True,
        "summary": "Send raw text to a session. Set enter=true to append \\r (simulate pressing Enter); default false.",
        "body": {
            "text": "string, raw text to send",
            "enter": "bool, append \\r after text (default false)",
        },
        "example": {"text": "ls -la", "enter": True},
    },
    {
        "method": "POST", "path": "/api/v1/sessions/{session_id}/set-title",
        "auth": True,
        "summary": "Set the session title (shown in the tab/window). Pass an empty string to reset to the default.",
        "body": {"title": "string, the new session title"},
        "example": {"title": "My Build Job"},
    },
    {
        "method": "POST", "path": "/api/v1/sessions/{session_id}/send-key",
        "auth": True,
        "summary": "Send a special key or key combination.",
        "body": {"key": "enter|tab|escape|up|down|left|right|home|end|backspace|delete|space|ctrl+{a-z}"},
        "example": {"key": "ctrl+c"},
    },
    {
        "method": "GET", "path": "/api/v1/files",
        "auth": True,
        "summary": "Read a file. Gated by file_access in config.json.",
        "query": {
            "path": "Absolute path of the file to read.",
            "base64": "When true, return base64-encoded bytes instead of utf-8 text. Default false.",
        },
        "errors": {
            "403": "file_access disabled or path outside allowed_paths",
            "404": "file not found",
            "415": "file is binary; retry with base64=true",
        },
    },
    {
        "method": "POST", "path": "/api/v1/files",
        "auth": True,
        "summary": "Write a file. Two modes: JSON body or multipart upload.",
        "query": {"path": "Absolute path of the file to write."},
        "body": {
            "content": "string, the content to write (required for JSON mode)",
            "encoding": "utf-8 (default) or base64",
            "mkdir": "bool, create missing parent directories (default false)",
            "append": "bool, append instead of overwrite (default false)",
        },
        "example": {"content": "hello\n", "mkdir": True},
        "notes": "Or send Content-Type: multipart/form-data with a 'file' field for raw bytes; "
                 "additional 'mkdir' / 'append' form fields are accepted.",
    },
    {
        "method": "DELETE", "path": "/api/v1/files",
        "auth": True,
        "summary": "Delete a file (not a directory).",
        "query": {"path": "Absolute path of the file to delete."},
    },
    {
        "method": "GET", "path": "/api/v1/files/list",
        "auth": True,
        "summary": "List directory contents.",
        "query": {
            "path": "Absolute path of the directory.",
            "recursive": "When true, walk the directory tree. Default false.",
        },
    },
]


def api_directory():
    """Return the full API directory used for error hints and discovery."""
    return {
        "service": "iterm2-harness",
        "version": VERSION,
        "listen": {"host": HOST, "port": PORT},
        "auth": {
            "scheme": "Bearer token in Authorization header",
            "obtain_token": "POST /api/v1/auth/request  (iTerm2 will show a confirmation alert)",
            "header_example": "Authorization: Bearer <token>",
        },
        "endpoints": API_ENDPOINTS,
    }


def error_payload(error_msg, hint=None, **extra):
    """Standard error payload with a hint and the full API directory for discovery."""
    payload = {"error": error_msg}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    payload["api"] = api_directory()
    return payload


# ─── Auth ──────────────────────────────────────────────────

PUBLIC_PATHS = {"/api/v1/health", "/api/v1/auth/request", "/", "/api", "/api/v1"}


def _extract_token(headers):
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _check_token(token):
    if not token:
        return None
    tokens = _load_tokens()
    return tokens.get(token)


async def _prompt_user_authorize(device_name, client_addr):
    """Show an iTerm2 alert and wait for the user's decision."""
    title = "iterm2-harness authorization request"
    body = (
        f"Device: {device_name}\n"
        f"Origin: {client_addr}\n\n"
        f"Allow this device to control iTerm2?"
    )
    alert = iterm2.Alert(title, body)
    alert.add_button("Allow")
    alert.add_button("Deny")
    selection = await alert.async_run(_connection)
    # First add_button -> 1000, second -> 1001.
    return selection == 1000


# ─── Routes ────────────────────────────────────────────────

ROUTES = [
    ("GET",  r"/$",                                         "handle_index"),
    ("GET",  r"/api$",                                      "handle_index"),
    ("GET",  r"/api/v1$",                                   "handle_index"),
    ("GET",  r"/api/v1/health$",                            "handle_health"),
    ("POST", r"/api/v1/reload$",                            "handle_reload"),
    ("POST", r"/api/v1/auth/request$",                      "handle_auth_request"),
    ("GET",  r"/api/v1/auth/whoami$",                       "handle_whoami"),
    ("GET",  r"/api/v1/windows$",                           "handle_list_windows"),
    ("GET",  r"/api/v1/sessions$",                          "handle_list_sessions"),
    ("GET",  r"/api/v1/sessions/(?P<sid>[^/]+)/screen$",    "handle_get_screen"),
    ("GET",  r"/api/v1/sessions/(?P<sid>[^/]+)/metadata$",  "handle_get_metadata"),
    ("POST", r"/api/v1/sessions/(?P<sid>[^/]+)/send-text$", "handle_send_text"),
    ("POST", r"/api/v1/sessions/(?P<sid>[^/]+)/send-key$",  "handle_send_key"),
    ("POST", r"/api/v1/sessions/(?P<sid>[^/]+)/set-title$", "handle_set_title"),
    ("GET",  r"/api/v1/files$",                             "handle_file_read"),
    ("POST", r"/api/v1/files$",                             "handle_file_write"),
    ("DELETE", r"/api/v1/files$",                           "handle_file_delete"),
    ("GET",  r"/api/v1/files/list$",                        "handle_file_list"),
]


def match_route(method, path):
    for route_method, pattern, handler_name in ROUTES:
        if method != route_method:
            continue
        m = re.match(pattern, path)
        if m:
            return handler_name, m.groupdict()
    return None, None


# ─── Handlers ──────────────────────────────────────────────

async def _get_app():
    return await iterm2.async_get_app(_connection)


async def _find_session(sid):
    app = await _get_app()
    return app.get_session_by_id(sid)


async def handle_index(**_):
    d = api_directory()
    d["status"] = "ok"
    d["hint"] = "iterm2-harness API directory. All endpoints except health and auth/request require a Bearer token."
    return 200, d


async def handle_health(**_):
    return 200, {"status": "ok", "server": "iterm2-harness",
                 "host": HOST, "port": PORT}


async def handle_reload(**_):
    """Trigger a restart asynchronously: respond first, then exec self."""
    audit("server.reload")

    async def _do_restart():
        # Give the HTTP response a moment to flush.
        await asyncio.sleep(0.3)
        import sys
        script = os.path.abspath(__file__)
        # iTerm2 Python scripts run as standalone processes; exec replaces us.
        os.execv(sys.executable, [sys.executable, script])

    asyncio.create_task(_do_restart())
    return 200, {"ok": True, "message": "Reloading script…"}


async def handle_auth_request(body=None, client_addr=None, **_):
    body = body or {}
    device_name = (body.get("device_name") or "").strip() or "unknown-device"

    # Serialize auth flow to avoid stacked alerts.
    async with _auth_lock:
        audit("auth.request", device_name=device_name, client=client_addr)
        approved = await _prompt_user_authorize(device_name, client_addr or "?")
        if not approved:
            audit("auth.denied", device_name=device_name, client=client_addr)
            return 403, {"error": "Authorization denied by user"}

        token = secrets.token_urlsafe(32)
        tokens = _load_tokens()
        tokens[token] = {
            "device_name": device_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "client_addr": client_addr or "",
        }
        _save_tokens(tokens)
        audit("auth.granted", device_name=device_name, client=client_addr)
        return 201, {"token": token, "device_name": device_name}


async def handle_whoami(token_info=None, **_):
    return 200, {"device_name": token_info.get("device_name"),
                 "created_at": token_info.get("created_at")}


async def handle_list_windows(**_):
    app = await _get_app()
    windows = []
    for window in app.windows:
        tabs = []
        for tab in window.tabs:
            sessions = []
            for s in tab.sessions:
                sessions.append({
                    "session_id": s.session_id, "name": s.name,
                    "columns": s.grid_size.width, "rows": s.grid_size.height,
                })
            tabs.append({"tab_id": tab.tab_id, "sessions": sessions})
        windows.append({"window_id": window.window_id, "tabs": tabs})
    return 200, {"windows": windows}


async def handle_list_sessions(query_params=None, **_):
    """Filters (all AND'd; omit for full list):
        name=<substring>     case-insensitive substring of session name
        job=<substring>      jobName (running process name)
        command=<substring>  full commandLine (recommended, more stable)
        path=<substring>     working directory
        regex=true           treat the above as regex instead of substring
    """
    app = await _get_app()
    params = query_params or {}
    name_q = params.get("name", "")
    job_q = params.get("job", "")
    cmd_q = params.get("command", "")
    path_q = params.get("path", "")
    use_regex = params.get("regex", "false").lower() == "true"
    need_meta = bool(job_q or cmd_q or path_q)

    def matches(haystack, needle):
        if not needle:
            return True
        if not haystack:
            return False
        if use_regex:
            try:
                return re.search(needle, haystack) is not None
            except re.error:
                return False
        return needle.lower() in haystack.lower()

    sessions = []
    for window in app.windows:
        for tab in window.tabs:
            for s in tab.sessions:
                if not matches(s.name, name_q):
                    continue

                job = command = path = ""
                if need_meta:
                    async def _v(name):
                        try:
                            return (await s.async_get_variable(name)) or ""
                        except Exception:
                            return ""
                    job = await _v("jobName")
                    command = await _v("commandLine")
                    path = await _v("path")
                    if not matches(job, job_q):
                        continue
                    if not matches(command, cmd_q):
                        continue
                    if not matches(path, path_q):
                        continue

                entry = {
                    "session_id": s.session_id, "name": s.name,
                    "window_id": window.window_id, "tab_id": tab.tab_id,
                    "columns": s.grid_size.width, "rows": s.grid_size.height,
                }
                if need_meta:
                    entry["job_name"] = job
                    entry["command_line"] = command
                    entry["path"] = path
                sessions.append(entry)

    return 200, {
        "sessions": sessions,
        "filter": {"name": name_q, "job": job_q, "command": cmd_q,
                   "path": path_q, "regex": use_regex},
    }


async def _get_screen_contents(session, limit):
    request = iterm2.rpc._alloc_request()
    request.get_buffer_request.session = session.session_id
    request.get_buffer_request.include_styles = False
    request.get_buffer_request.line_range.trailing_lines = limit
    result = await iterm2.rpc._async_call(_connection, request)
    return iterm2.screen.ScreenContents(result.get_buffer_response)


async def handle_get_screen(sid, query_params=None, **_):
    session = await _find_session(sid)
    if not session:
        return 404, {
            "error": f"Session '{sid}' not found",
            "hint": "Call GET /api/v1/sessions to list current session_ids.",
        }

    params = query_params or {}
    limit = int(params.get("limit", "500"))
    offset = int(params.get("offset", "0"))
    strip = params.get("strip", "false").lower() == "true"

    fetch_lines = limit + offset
    screen = await _get_screen_contents(session, fetch_lines)
    # \x00 marks empty/continuation cells in the iTerm2 grid (e.g. trailing half
    # of a wide character). Replace with a space rather than dropping it, or
    # natural inter-word whitespace gets eaten and words run together.
    all_lines = [screen.line(i).string.replace("\x00", " ")
                 for i in range(screen.number_of_lines)]
    fetched = screen.number_of_lines
    has_more = fetched >= fetch_lines

    if offset > 0:
        lines = all_lines[:-offset] if offset < len(all_lines) else []
    else:
        lines = all_lines
    if limit > 0 and len(lines) > limit:
        lines = lines[-limit:]
    if strip:
        # Collapse runs of horizontal whitespace (spaces, tabs) only; preserve
        # \r/\n if any, so the caller can still see the original line breaks
        # if a single grid line happened to contain embedded newlines.
        lines = [re.sub(r"[ \t]+", " ", l).strip(" \t") for l in lines]
        lines = [l for l in lines if l]

    return 200, {
        "session_id": sid, "lines": lines,
        "fetched_lines": fetched, "returned_lines": len(lines),
        "offset": offset, "has_more": has_more,
    }


async def handle_get_metadata(sid, **_):
    session = await _find_session(sid)
    if not session:
        return 404, {
            "error": f"Session '{sid}' not found",
            "hint": "Call GET /api/v1/sessions to list current session_ids.",
        }

    async def safe(name):
        try:
            return (await session.async_get_variable(name)) or ""
        except Exception:
            return ""

    return 200, {
        "session_id": sid, "name": session.name,
        "columns": session.grid_size.width, "rows": session.grid_size.height,
        "path": await safe("path"),
        "command_line": await safe("commandLine"),
        "job_name": await safe("jobName"),
        # Extra title-related variables for diagnosing which one matches the
        # tab/pane label the user actually sees.
        "auto_name": await safe("autoName"),
        "terminal_icon_name": await safe("terminalIconName"),
        "terminal_window_name": await safe("terminalWindowName"),
        "user_name": await safe("name"),
        "session_name": await safe("session.name"),
        "tab_title_override": await safe("tab.titleOverride"),
    }


async def handle_send_text(sid, body=None, **_):
    if not body:
        return 400, {
            "error": "Missing request body",
            "hint": "Send a JSON body, e.g. {\"text\": \"ls -la\\r\"}. Use \\r for Enter.",
        }
    session = await _find_session(sid)
    if not session:
        return 404, {
            "error": f"Session '{sid}' not found",
            "hint": "Call GET /api/v1/sessions to list current session_ids.",
        }
    text = body.get("text", "")
    if not text:
        return 400, {
            "error": "Missing 'text' field",
            "hint": "Example: {\"text\": \"ls -la\", \"enter\": true}",
        }
    enter = bool(body.get("enter", False))
    payload = text + ("\r" if enter else "")
    await session.async_send_text(payload)
    return 200, {"ok": True, "sent": payload, "enter": enter}


async def handle_set_title(sid, body=None, **_):
    if body is None:
        return 400, {
            "error": "Missing request body",
            "hint": "Send a JSON body, e.g. {\"title\": \"My Build\"}.",
        }
    session = await _find_session(sid)
    if not session:
        return 404, {
            "error": f"Session '{sid}' not found",
            "hint": "Call GET /api/v1/sessions to list current session_ids.",
        }
    if "title" not in body:
        return 400, {
            "error": "Missing 'title' field",
            "hint": "Example: {\"title\": \"My Build\"}. Use an empty string to reset.",
        }
    title = str(body["title"])
    await session.async_set_name(title)
    return 200, {"ok": True, "session_id": sid, "title": title}


async def handle_send_key(sid, body=None, **_):
    if not body:
        return 400, {
            "error": "Missing request body",
            "hint": "Send a JSON body, e.g. {\"key\": \"ctrl+c\"}.",
            "available_keys": sorted(KEY_MAP.keys()) + ["ctrl+{a-z}"],
        }
    session = await _find_session(sid)
    if not session:
        return 404, {
            "error": f"Session '{sid}' not found",
            "hint": "Call GET /api/v1/sessions to list current session_ids.",
        }
    key = body.get("key", "").lower().strip()
    if not key:
        return 400, {
            "error": "Missing 'key' field",
            "hint": "Example: {\"key\": \"enter\"} or {\"key\": \"ctrl+c\"}",
            "available_keys": sorted(KEY_MAP.keys()) + ["ctrl+{a-z}"],
        }

    sequence = KEY_MAP.get(key)
    if not sequence:
        m = re.match(r"^ctrl\+([a-z])$", key)
        if m:
            sequence = chr(ord(m.group(1)) - ord("a") + 1)
        else:
            return 400, {
                "error": f"Unknown key: '{key}'",
                "hint": "See available_keys, or use ctrl+{a-z} form.",
                "available_keys": sorted(KEY_MAP.keys()) + ["ctrl+{a-z}"],
            }
    await session.async_send_text(sequence)
    return 200, {"ok": True, "key": key}


# ─── File access ───────────────────────────────────────────

def _file_access_check(abs_path):
    """Return (ok, status, payload) for an absolute path under the access policy.

    On success returns (True, 0, resolved_real_path). On failure returns
    (False, status_code, error_body_dict).
    """
    if not FILE_ACCESS.get("enabled", False):
        return False, 403, {
            "error": "File access is disabled",
            "hint": "Set file_access.enabled=true in config.json next to the script, "
                    "then POST /api/v1/reload.",
        }
    if not abs_path:
        return False, 400, {
            "error": "Missing 'path' query parameter",
            "hint": "Provide an absolute path, e.g. ?path=/Users/me/notes.txt",
        }
    if not os.path.isabs(abs_path):
        return False, 400, {
            "error": "Path must be absolute",
            "hint": f"Got {abs_path!r}; pass a path starting with '/'.",
        }
    real = os.path.realpath(abs_path)
    allowed = FILE_ACCESS.get("allowed_paths") or []
    if allowed:
        # A request path is permitted if its realpath sits under any allowed
        # prefix (also realpath'd to handle symlinks in the policy itself).
        ok = False
        for prefix in allowed:
            p = os.path.realpath(os.path.expanduser(prefix))
            if real == p or real.startswith(p.rstrip("/") + "/"):
                ok = True
                break
        if not ok:
            return False, 403, {
                "error": "Path is outside allowed_paths",
                "hint": "Edit file_access.allowed_paths in config.json to grant access.",
                "resolved_path": real,
                "allowed_paths": allowed,
            }
    return True, 0, real


def _is_probably_binary(data):
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


async def handle_file_read(query_params=None, **_):
    params = query_params or {}
    path = params.get("path", "")
    want_base64 = params.get("base64", "false").lower() == "true"

    ok, status, info = _file_access_check(path)
    if not ok:
        return status, info
    real = info

    if not os.path.exists(real):
        return 404, {"error": f"File not found: {real}"}
    if os.path.isdir(real):
        return 400, {
            "error": f"Path is a directory: {real}",
            "hint": "Use GET /api/v1/files/list?path=... to list a directory.",
        }
    try:
        with open(real, "rb") as f:
            data = f.read()
    except OSError as e:
        return 500, {"error": f"Read failed: {e}"}

    size = len(data)
    if want_base64:
        import base64
        return 200, {
            "path": real, "size": size,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }
    if _is_probably_binary(data):
        return 415, {
            "error": "File appears to be binary",
            "hint": "Retry with ?base64=true to receive base64-encoded bytes.",
            "path": real, "size": size,
        }
    return 200, {
        "path": real, "size": size,
        "encoding": "utf-8",
        "content": data.decode("utf-8"),
    }


def _parse_multipart(raw, content_type):
    """Minimal multipart/form-data parser. Returns dict of field_name -> bytes."""
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return None
    boundary = m.group(1).strip().strip('"').encode()
    delim = b"--" + boundary
    parts = raw.split(delim)
    out = {}
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers_raw = part[:header_end].decode("utf-8", "replace")
        body = part[header_end + 4:]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        name_match = re.search(r'name="([^"]+)"', headers_raw)
        if name_match:
            out[name_match.group(1)] = body
    return out


async def handle_file_write(query_params=None, body=None, raw_body=None, headers=None, **_):
    params = query_params or {}
    path = params.get("path", "")
    ok, status, info = _file_access_check(path)
    if not ok:
        return status, info
    real = info

    content_type = (headers or {}).get("content-type", "")
    data = None
    mkdir = False
    append = False

    if content_type.startswith("multipart/form-data"):
        parts = _parse_multipart(raw_body or b"", content_type)
        if not parts or "file" not in parts:
            return 400, {
                "error": "Missing 'file' field in multipart body",
                "hint": "Use a form field named 'file' to upload raw bytes.",
            }
        data = parts["file"]
        mkdir = parts.get("mkdir", b"").decode("utf-8", "replace").lower() == "true"
        append = parts.get("append", b"").decode("utf-8", "replace").lower() == "true"
    else:
        if body is None:
            return 400, {
                "error": "Missing JSON body",
                "hint": "Send {\"content\": \"...\", \"encoding\": \"utf-8\"} or use multipart upload.",
            }
        mkdir = bool(body.get("mkdir", False))
        append = bool(body.get("append", False))
        content = body.get("content", None)
        if content is None:
            return 400, {
                "error": "Missing 'content' field",
                "hint": "Provide the string to write, e.g. {\"content\": \"hello\"}",
            }
        encoding = (body.get("encoding") or "utf-8").lower()
        if encoding == "base64":
            import base64
            try:
                data = base64.b64decode(content)
            except Exception as e:
                return 400, {"error": f"Invalid base64 content: {e}"}
        else:
            data = content.encode(encoding, "strict") if isinstance(content, str) else content

    parent = os.path.dirname(real)
    existed = os.path.exists(real)
    if parent and not os.path.isdir(parent):
        if mkdir:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return 500, {"error": f"mkdir failed: {e}"}
        else:
            return 409, {
                "error": f"Parent directory does not exist: {parent}",
                "hint": "Pass mkdir=true to create it automatically.",
            }

    mode = "ab" if append else "wb"
    try:
        with open(real, mode) as f:
            f.write(data)
    except OSError as e:
        return 500, {"error": f"Write failed: {e}"}

    audit("file.write", path=real, size=len(data),
          append=append, created=(not existed))
    return 200, {
        "path": real, "size": len(data),
        "created": not existed, "append": append,
    }


async def handle_file_delete(query_params=None, **_):
    params = query_params or {}
    path = params.get("path", "")
    ok, status, info = _file_access_check(path)
    if not ok:
        return status, info
    real = info

    if not os.path.exists(real):
        return 404, {"error": f"File not found: {real}"}
    if os.path.isdir(real):
        return 400, {
            "error": f"Path is a directory: {real}",
            "hint": "Directory deletion is not supported by this endpoint.",
        }
    try:
        os.remove(real)
    except OSError as e:
        return 500, {"error": f"Delete failed: {e}"}
    audit("file.delete", path=real)
    return 200, {"path": real, "deleted": True}


async def handle_file_list(query_params=None, **_):
    params = query_params or {}
    path = params.get("path", "")
    recursive = params.get("recursive", "false").lower() == "true"
    ok, status, info = _file_access_check(path)
    if not ok:
        return status, info
    real = info

    if not os.path.exists(real):
        return 404, {"error": f"Directory not found: {real}"}
    if not os.path.isdir(real):
        return 400, {
            "error": f"Path is not a directory: {real}",
            "hint": "Use GET /api/v1/files?path=... to read a file.",
        }

    entries = []
    try:
        if recursive:
            for root, dirs, files in os.walk(real):
                for name in dirs:
                    entries.append(_describe_entry(os.path.join(root, name), real))
                for name in files:
                    entries.append(_describe_entry(os.path.join(root, name), real))
        else:
            for name in sorted(os.listdir(real)):
                entries.append(_describe_entry(os.path.join(real, name), real))
    except OSError as e:
        return 500, {"error": f"List failed: {e}"}

    return 200, {"path": real, "recursive": recursive, "entries": entries}


def _describe_entry(full, root):
    try:
        st = os.lstat(full)
    except OSError:
        return {"name": os.path.relpath(full, root), "type": "unknown"}
    if os.path.isdir(full):
        typ = "dir"
    elif os.path.islink(full):
        typ = "link"
    else:
        typ = "file"
    return {
        "name": os.path.relpath(full, root),
        "type": typ,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    }


HANDLERS = {
    "handle_index": handle_index,
    "handle_health": handle_health,
    "handle_reload": handle_reload,
    "handle_auth_request": handle_auth_request,
    "handle_whoami": handle_whoami,
    "handle_list_windows": handle_list_windows,
    "handle_list_sessions": handle_list_sessions,
    "handle_get_screen": handle_get_screen,
    "handle_get_metadata": handle_get_metadata,
    "handle_send_text": handle_send_text,
    "handle_send_key": handle_send_key,
    "handle_set_title": handle_set_title,
    "handle_file_read": handle_file_read,
    "handle_file_write": handle_file_write,
    "handle_file_delete": handle_file_delete,
    "handle_file_list": handle_file_list,
}


# ─── Request handling ──────────────────────────────────────

async def handle_client(reader, writer):
    peer = writer.get_extra_info("peername")
    client_addr = f"{peer[0]}:{peer[1]}" if peer else "?"
    method = path = "-"
    status = 0
    try:
        req = await read_http_request(reader)
        if not req:
            writer.close()
            return
        method, path, query_params, headers, raw_body = req

        body = None
        ctype = (headers or {}).get("content-type", "").lower()
        if raw_body and not ctype.startswith("multipart/"):
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                status = 400
                writer.write(make_response(400, error_payload(
                    "Invalid JSON body",
                    hint="Request body must be valid JSON. See endpoints[].example for the expected fields."
                )))
                await writer.drain()
                return

        handler_name, path_params = match_route(method, path)
        if not handler_name:
            allowed_methods = []
            for rm, pattern, _ in ROUTES:
                if re.match(pattern, path):
                    allowed_methods.append(rm)
            if allowed_methods:
                status = 405
                writer.write(make_response(405, error_payload(
                    f"Method {method} not allowed for {path}",
                    hint=f"Allowed methods on this path: {', '.join(sorted(set(allowed_methods)))}",
                    allowed_methods=sorted(set(allowed_methods)),
                )))
                await writer.drain()
                return
            status = 404
            writer.write(make_response(404, error_payload(
                f"Not found: {method} {path}",
                hint="Unknown path. See api.endpoints below for all available endpoints. "
                     "Hint: GET / or GET /api/v1 also returns this directory.",
            )))
            await writer.drain()
            return

        # Auth check.
        token_info = None
        if path not in PUBLIC_PATHS:
            token = _extract_token(headers)
            token_info = _check_token(token)
            if not token_info:
                status = 401
                audit("auth.reject", method=method, path=path, client=client_addr)
                writer.write(make_response(401, error_payload(
                    "Missing or invalid token",
                    hint="Call POST /api/v1/auth/request first (iTerm2 will show a confirmation alert). "
                         "Then pass the returned token via 'Authorization: Bearer <token>'.",
                )))
                await writer.drain()
                return

        handler = HANDLERS[handler_name]
        status, data = await handler(
            **path_params,
            query_params=query_params,
            body=body,
            raw_body=raw_body,
            headers=headers,
            client_addr=client_addr,
            token_info=token_info,
        )

        # Audit everything except health.
        if path != "/api/v1/health":
            audit(
                "request",
                method=method, path=path, status=status,
                client=client_addr,
                device=(token_info or {}).get("device_name"),
                params=query_params or None,
            )

        writer.write(make_response(status, data))
        await writer.drain()

    except Exception as e:
        audit("error", method=method, path=path, client=client_addr, error=str(e))
        try:
            writer.write(make_response(500, {"error": str(e)}))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ─── Entry point ───────────────────────────────────────────

PORT_FALLBACK_RANGE = 50  # Maximum number of port offsets to try.


async def _start_server_with_fallback(host, start_port, max_tries=PORT_FALLBACK_RANGE):
    """Start the server, incrementing the port if it's busy. Returns (server, port)."""
    last_err = None
    for offset in range(max_tries):
        port = start_port + offset
        try:
            server = await asyncio.start_server(handle_client, host, port)
            if offset > 0:
                audit("server.port_fallback",
                      requested=start_port, actual=port, offset=offset)
            return server, port
        except OSError as e:
            last_err = e
            audit("server.port_busy", host=host, port=port, error=str(e))
            continue
    raise RuntimeError(
        f"No free port in {host}:{start_port}..{start_port + max_tries - 1}: {last_err}"
    )


def notify_user(title, message):
    """Push a non-blocking macOS notification-center toast via osascript."""
    def _esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
    try:
        import subprocess
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        audit("notify.failed", error=str(e))


async def main(connection):
    global _connection, PORT
    _connection = connection
    _ensure_dirs()

    server, actual_port = await _start_server_with_fallback(HOST, PORT)
    PORT = actual_port
    audit("server.start", host=HOST, port=actual_port, version=VERSION)
    notify_user(
        "iterm2-harness started",
        f"v{VERSION} listening on {HOST}:{actual_port}",
    )

    async with server:
        await server.serve_forever()


iterm2.run_until_complete(main)
