#!/usr/bin/env python3
import argparse
import http.server
import hashlib
import json
import os
import select
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from socketserver import ThreadingMixIn


APP_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.environ.get("TELECODEXBOT_WORKSPACE_DIR", os.getcwd())).expanduser().resolve()
CONFIG_DIR = Path(
    os.environ.get(
        "TELECODEXBOT_CONFIG_DIR",
        str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "telecodexbot"),
    )
).expanduser().resolve()
STATE_BASE_DIR = Path(
    os.environ.get(
        "TELECODEXBOT_STATE_HOME",
        str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "telecodexbot"),
    )
).expanduser().resolve()
WORKSPACE_KEY = hashlib.sha256(str(WORKSPACE_ROOT).encode("utf-8")).hexdigest()[:16]
STATE_DIR = Path(os.environ.get("TELECODEXBOT_STATE_DIR", str(STATE_BASE_DIR / WORKSPACE_KEY))).expanduser().resolve()
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = STATE_DIR / "state.json"
INBOX_PATH = STATE_DIR / "inbox.jsonl"
RELAY_STATE_PATH = STATE_DIR / "relay_state.json"
WORKSPACE_META_PATH = STATE_DIR / "workspace.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_HISTORY_PATH = CODEX_HOME / "history.jsonl"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Claude Code paths
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
CLAUDE_HISTORY_PATH = CLAUDE_HOME / "history.jsonl"
CLAUDE_PROJECTS_DIR = CLAUDE_HOME / "projects"
CLAUDE_SESSIONS_DIR = CLAUDE_HOME / "sessions"

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None


def ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_META_PATH.write_text(
        json.dumps({"workspace_root": str(WORKSPACE_ROOT)}, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def lock_exclusive(fh) -> None:
    if fcntl is None:
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def unlock_file(fh) -> None:
    if fcntl is None:
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    ensure_dir()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def save_config(config) -> None:
    ensure_dir()
    existing = load_json(CONFIG_PATH, {})
    merged = dict(existing)
    merged.update(config)
    save_json(CONFIG_PATH, merged)


def append_jsonl(path: Path, data) -> None:
    ensure_dir()
    with path.open("a+", encoding="utf-8") as fh:
        lock_exclusive(fh)
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(data, ensure_ascii=True) + "\n")
        fh.flush()
        unlock_file(fh)


def load_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def save_jsonl(path: Path, rows) -> None:
    ensure_dir()
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")


def pop_jsonl(path: Path, limit: int = 1):
    ensure_dir()
    if not path.exists():
        return []
    with path.open("r+", encoding="utf-8") as fh:
        lock_exclusive(fh)
        lines = [line.strip() for line in fh.read().splitlines() if line.strip()]
        if not lines:
            unlock_file(fh)
            return []
        if limit <= 0:
            taken = lines
            remain = []
        else:
            taken = lines[:limit]
            remain = lines[limit:]
        fh.seek(0)
        fh.truncate(0)
        for line in remain:
            fh.write(line + "\n")
        fh.flush()
        unlock_file(fh)
    rows = []
    for line in taken:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def load_config():
    config = load_json(CONFIG_PATH, {})
    token = os.environ.get(
        "TELECODEXBOT_BOT_TOKEN",
        os.environ.get("CODEX_TELEGRAM_BOT_TOKEN", config.get("bot_token", "")),
    )
    chat_id = int(
        os.environ.get(
            "TELECODEXBOT_CHAT_ID",
            os.environ.get("CODEX_TELEGRAM_CHAT_ID", config.get("chat_id", 0) or 0),
        )
        or 0
    )
    user_id = int(
        os.environ.get(
            "TELECODEXBOT_USER_ID",
            os.environ.get("CODEX_TELEGRAM_USER_ID", config.get("user_id", 0) or 0),
        )
        or 0
    )
    username = os.environ.get(
        "TELECODEXBOT_USERNAME",
        os.environ.get("CODEX_TELEGRAM_USERNAME", config.get("username", "")),
    )
    if not token:
        raise SystemExit("Telegram bridge no configurado: falta bot_token")
    if not chat_id:
        raise SystemExit("Telegram bridge no configurado: falta chat_id")
    if not user_id:
        raise SystemExit("Telegram bridge no configurado: falta user_id")
    merged = dict(config)
    merged.update(
        {
            "bot_token": token,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
        }
    )
    return merged


def load_state():
    return load_json(STATE_PATH, {"update_offset": 0})


def save_state(state) -> None:
    save_json(STATE_PATH, state)


def api_request(token: str, method: str, payload=None, timeout: int = 30):
    payload = payload or {}
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"Telegram API error in {method}"))
    return result["result"]


def http_json_get(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def bootstrap_offset(token: str) -> int:
    updates = api_request(token, "getUpdates", {"timeout": 0}, timeout=15)
    if not updates:
        return 0
    return max(update["update_id"] for update in updates) + 1


def delete_webhook_for_token(token: str, drop_pending: bool):
    return api_request(
        token,
        "deleteWebhook",
        {"drop_pending_updates": "true" if drop_pending else "false"},
        timeout=20,
    )


def send_message(config, text: str):
    return api_request(
        config["bot_token"],
        "sendMessage",
        {
            "chat_id": str(config["chat_id"]),
            "text": text,
        },
        timeout=20,
    )


def ensure_webhook_secret(config):
    secret = config.get("webhook_secret", "")
    if secret:
        return secret
    secret = secrets.token_hex(16)
    config["webhook_secret"] = secret
    save_config(config)
    return secret


def read_stdin_line():
    if not sys.stdin or sys.stdin.closed:
        return None
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        return None
    ready, _, _ = select.select([fd], [], [], 0.0)
    if not ready:
        return None
    line = sys.stdin.readline()
    if not line:
        return None
    return line.rstrip("\r\n")


def poll_updates(config, state, long_poll_timeout: int):
    updates = api_request(
        config["bot_token"],
        "getUpdates",
        {
            "offset": str(state.get("update_offset", 0)),
            "timeout": str(long_poll_timeout),
        },
        timeout=long_poll_timeout + 10,
    )
    accepted = []
    next_offset = state.get("update_offset", 0)
    for update in updates:
        next_offset = max(next_offset, update["update_id"] + 1)
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        if int(chat.get("id", 0)) != int(config["chat_id"]):
            continue
        if int(from_user.get("id", 0)) != int(config["user_id"]):
            continue
        text = message.get("text")
        if not text:
            continue
        accepted.append(
            {
                "source": "telegram",
                "text": text,
                "chat_id": int(chat["id"]),
                "user_id": int(from_user["id"]),
                "username": from_user.get("username", ""),
                "message_id": int(message.get("message_id", 0)),
                "date": int(message.get("date", 0)),
            }
        )
    state["update_offset"] = next_offset
    save_state(state)
    return accepted


def normalize_update(config, update):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    if int(chat.get("id", 0)) != int(config["chat_id"]):
        return None
    if int(from_user.get("id", 0)) != int(config["user_id"]):
        return None
    text = message.get("text")
    if not text:
        return None
    return {
        "source": "telegram",
        "text": text,
        "chat_id": int(chat["id"]),
        "user_id": int(from_user["id"]),
        "username": from_user.get("username", ""),
        "message_id": int(message.get("message_id", 0)),
        "date": int(message.get("date", 0)),
    }


def inbox_next():
    rows = pop_jsonl(INBOX_PATH, limit=1)
    if not rows:
        return None
    return rows[0]


def webhook_enabled(config):
    return bool(config.get("webhook_url"))


def split_text_chunks(text: str, limit: int):
    msg = (text or "").strip()
    if not msg:
        return []
    chunks = []
    while len(msg) > limit:
        cut = msg.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = msg.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(msg[:cut].rstrip())
        msg = msg[cut:].lstrip()
    if msg:
        chunks.append(msg)
    return chunks


def send_chunked(config, text: str, max_chars: int):
    for chunk in split_text_chunks(text, max_chars):
        send_message(config, chunk)


def safe_send_chunked(config, text: str, max_chars: int):
    try:
        send_chunked(config, text, max_chars)
        return True
    except Exception as exc:
        print(
            json.dumps(
                {"warn": "telegram_send_failed", "error": str(exc), "ts": int(time.time())},
                ensure_ascii=True,
            ),
            flush=True,
        )
        return False


def format_tagged_message(tag: str, text: str, max_chars: int):
    payload = (text or "").strip()
    if not payload:
        return ""
    if len(payload) > max_chars:
        payload = (
            payload[:max_chars]
            + f"\n\n[relay truncado: {len(text)} chars originales, limite {max_chars}]"
        )
    if tag:
        return f"{tag}\n{payload}"
    return payload


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def latest_history_session_id(history_path: Path):
    if not history_path.exists():
        return ""
    last_session_id = ""
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            session_id = str(row.get("session_id", "")).strip()
            if session_id:
                last_session_id = session_id
    return last_session_id


def normalize_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(Path(value).expanduser())


def session_meta(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("type") != "session_meta":
                    continue
                payload = row.get("payload") or {}
                return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    return {}


def latest_workspace_session_id(workspace_root: Path):
    if not CODEX_SESSIONS_DIR.exists():
        return ""
    target_cwd = normalize_path(str(workspace_root))
    best_mtime = -1.0
    best_session_id = ""
    for session_file in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        meta = session_meta(session_file)
        cwd = normalize_path(str(meta.get("cwd", "")))
        if not cwd or cwd != target_cwd:
            continue
        session_id = str(meta.get("id", "")).strip()
        if not session_id:
            continue
        mtime = session_file.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_session_id = session_id
    return best_session_id


def find_session_file(session_id: str):
    if not session_id:
        return None
    if not CODEX_SESSIONS_DIR.exists():
        return None
    pattern = f"*{session_id}.jsonl"
    matches = list(CODEX_SESSIONS_DIR.rglob(pattern))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def read_new_jsonl_rows(path: Path, offset: int):
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        raw = fh.read()
        next_offset = fh.tell()
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows, next_offset


def extract_assistant_text(event):
    if event.get("type") != "response_item":
        return "", ""
    payload = event.get("payload") or {}
    if payload.get("type") != "message":
        return "", ""
    if payload.get("role") != "assistant":
        return "", ""
    chunks = []
    for item in payload.get("content") or []:
        kind = item.get("type", "")
        if kind in ("output_text", "input_text"):
            text = str(item.get("text", "")).strip()
            if text:
                chunks.append(text)
    if not chunks:
        return "", ""
    return "\n".join(chunks).strip(), str(payload.get("phase", "")).strip()


# ---------------------------------------------------------------------------
# Claude Code backend helpers
# ---------------------------------------------------------------------------

def claude_project_key(workspace: Path) -> str:
    """Convert a workspace path to Claude Code's project directory name.

    Claude Code encodes the absolute path replacing '/' with '-' and stripping
    the leading separator, e.g. /home/pablo -> -home-pablo.
    """
    return str(workspace).replace("/", "-")


def claude_conversation_dir(workspace: Path) -> Path:
    """Return the Claude Code project directory for *workspace*."""
    return CLAUDE_PROJECTS_DIR / claude_project_key(workspace)


def latest_claude_session_id(workspace: Path) -> str:
    """Find the most-recently-modified conversation for *workspace*."""
    conv_dir = claude_conversation_dir(workspace)
    if not conv_dir.exists():
        return ""
    best_mtime = -1.0
    best_id = ""
    for f in conv_dir.glob("*.jsonl"):
        mtime = f.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_id = f.stem  # UUID without .jsonl
    return best_id


def latest_claude_history_session_id(workspace: Path) -> str:
    """Find the latest session ID from Claude history.jsonl for *workspace*."""
    if not CLAUDE_HISTORY_PATH.exists():
        return ""
    target_project = str(workspace)
    last_session_id = ""
    with CLAUDE_HISTORY_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            project = str(row.get("project", "")).strip()
            if project != target_project:
                continue
            sid = str(row.get("sessionId", "")).strip()
            if sid:
                last_session_id = sid
    return last_session_id


def find_claude_conversation_file(session_id: str, workspace: Path):
    """Locate the conversation JSONL for a Claude Code session."""
    if not session_id:
        return None
    conv_dir = claude_conversation_dir(workspace)
    candidate = conv_dir / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Also try glob in case of subdirectories
    for match in conv_dir.rglob(f"*{session_id}.jsonl"):
        return match
    return None


def extract_claude_assistant_text(event):
    """Extract assistant text from a Claude Code conversation event."""
    if event.get("type") != "assistant":
        return "", ""
    msg = event.get("message") or {}
    if msg.get("role") != "assistant":
        return "", ""
    content = msg.get("content") or []
    chunks = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "")
        if kind == "text":
            text = str(item.get("text", "")).strip()
            if text:
                chunks.append(text)
    if not chunks:
        return "", ""
    model = str(msg.get("model", "")).strip()
    return "\n".join(chunks).strip(), model


def extract_claude_user_text(event):
    """Extract user text from a Claude Code conversation event."""
    if event.get("type") != "user":
        return ""
    msg = event.get("message") or {}
    if msg.get("role") != "user":
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    chunks.append(text)
        return "\n".join(chunks).strip()
    return ""


def run_claude_resume(session_id: str, prompt_text: str, claude_cmd: str, timeout_sec: int, detach: bool):
    """Execute a Claude Code prompt continuing an existing session."""
    cmd = [claude_cmd, "--resume", session_id, "--print"]
    if detach:
        log_path = CONFIG_DIR / "relay-claude-resume.log"
        try:
            with log_path.open("a", encoding="utf-8") as logfh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(WORKSPACE_ROOT),
                    stdin=subprocess.PIPE,
                    stdout=logfh,
                    stderr=logfh,
                    text=True,
                    start_new_session=True,
                )
                if proc.stdin is not None:
                    proc.stdin.write(prompt_text + "\n")
                    proc.stdin.close()
            return True, f"spawned pid={proc.pid}"
        except Exception as exc:
            return False, f"fallo al lanzar claude resume: {exc}"
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            input=prompt_text + "\n",
            capture_output=True,
            text=True,
            timeout=(timeout_sec if timeout_sec > 0 else None),
            check=False,
        )
    except Exception as exc:
        return False, f"fallo al ejecutar claude resume: {exc}"
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit={completed.returncode}"
        return False, detail[:2000]
    return True, ""


def detect_backend() -> str:
    """Auto-detect whether to use 'codex' or 'claude' backend."""
    import shutil
    if shutil.which("codex"):
        return "codex"
    if shutil.which("claude"):
        return "claude"
    return "codex"


def run_codex_resume(session_id: str, prompt_text: str, codex_cmd: str, timeout_sec: int, full_auto: bool, detach: bool):
    stamp = int(time.time() * 1000)
    out_path = CONFIG_DIR / f"relay-last-assistant-{stamp}.txt"
    cmd = [codex_cmd, "exec", "resume", session_id, "-", "--output-last-message", str(out_path)]
    if full_auto:
        cmd.append("--full-auto")
    if detach:
        log_path = CONFIG_DIR / "relay-codex-resume.log"
        try:
            with log_path.open("a", encoding="utf-8") as logfh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(WORKSPACE_ROOT),
                    stdin=subprocess.PIPE,
                    stdout=logfh,
                    stderr=logfh,
                    text=True,
                    start_new_session=True,
                )
                if proc.stdin is not None:
                    proc.stdin.write(prompt_text + "\n")
                    proc.stdin.close()
            return True, f"spawned pid={proc.pid}"
        except Exception as exc:
            return False, f"fallo al lanzar codex resume: {exc}"
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            input=prompt_text + "\n",
            capture_output=True,
            text=True,
            timeout=(timeout_sec if timeout_sec > 0 else None),
            check=False,
        )
    except Exception as exc:
        return False, f"fallo al ejecutar codex resume: {exc}"
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit={completed.returncode}"
        return False, detail[:2000]
    return True, ""


def list_tmux_panes():
    cmd = ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_active}\t#{pane_current_command}\t#{session_name}:#{window_index}.#{pane_index}"]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [], f"tmux list-panes fallo: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return [], f"tmux list-panes exit={completed.returncode}: {detail[:400]}"
    panes = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pane_id, pane_active, pane_command, pane_name = parts
        panes.append(
            {
                "pane_id": pane_id.strip(),
                "active": pane_active.strip() == "1",
                "command": pane_command.strip(),
                "name": pane_name.strip(),
            }
        )
    return panes, ""


def list_tmux_clients():
    cmd = ["tmux", "list-clients", "-F", "#{client_session}\t#{client_tty}\t#{client_active}"]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [], f"tmux list-clients fallo: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return [], f"tmux list-clients exit={completed.returncode}: {detail[:400]}"
    clients = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        session_name, client_tty, client_active = parts
        clients.append(
            {
                "session": session_name.strip(),
                "tty": client_tty.strip(),
                "active": client_active.strip() == "1",
            }
        )
    return clients, ""


def pane_sort_key(pane):
    return (0 if pane.get("active") else 1, pane.get("name", ""))


def pane_session_name(pane):
    pane_name = str(pane.get("name", ""))
    return pane_name.split(":", 1)[0]


def resolve_tmux_target(explicit_target: str, tmux_command: str):
    target = (explicit_target or "").strip()
    if target:
        return target, ""
    panes, err = list_tmux_panes()
    if err:
        return "", err
    if not panes:
        return "", "no hay panes tmux disponibles"

    command = (tmux_command or "").strip()
    if command:
        exact = [pane for pane in panes if pane.get("command", "") == command]
        if exact:
            exact.sort(key=pane_sort_key)
            return str(exact[0].get("pane_id", "")), ""

    clients, client_err = list_tmux_clients()
    if not client_err and clients:
        clients.sort(key=lambda c: (0 if c.get("active") else 1, c.get("session", "")))
        for client in clients:
            session = str(client.get("session", ""))
            if not session:
                continue
            session_panes = [pane for pane in panes if pane_session_name(pane) == session]
            if not session_panes:
                continue
            session_panes.sort(key=pane_sort_key)
            return str(session_panes[0].get("pane_id", "")), ""

    if len(panes) == 1:
        return str(panes[0].get("pane_id", "")), ""

    active = [pane for pane in panes if pane.get("active")]
    if active:
        active.sort(key=pane_sort_key)
        return str(active[0].get("pane_id", "")), ""

    detail = f"no hay pane tmux con command={command}" if command else "no hay pane tmux elegible"
    return "", detail


def tmux_inject_message(target: str, text: str, press_enter: bool):
    payload = text or ""
    if not payload:
        return False, "mensaje vacio para tmux"
    lines = payload.splitlines() or [payload]
    for idx, line in enumerate(lines):
        literal_result = subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", "--", line],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if literal_result.returncode != 0:
            detail = (literal_result.stderr or literal_result.stdout or "").strip()
            return False, f"tmux send-keys -l exit={literal_result.returncode}: {detail[:400]}"
        if idx < len(lines) - 1:
            newline_result = subprocess.run(
                ["tmux", "send-keys", "-t", target, "C-j"],
                cwd=str(WORKSPACE_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if newline_result.returncode != 0:
                detail = (newline_result.stderr or newline_result.stdout or "").strip()
                return False, f"tmux send-keys C-j exit={newline_result.returncode}: {detail[:400]}"

    if press_enter:
        enter_result = subprocess.run(
            ["tmux", "send-keys", "-t", target, "C-m"],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if enter_result.returncode != 0:
            detail = (enter_result.stderr or enter_result.stdout or "").strip()
            return False, f"tmux send-keys exit={enter_result.returncode}: {detail[:400]}"

    return True, ""


def command_relay_daemon(args):
    config = load_config()
    state = load_state()
    ensure_dir()

    backend = (args.backend or "").strip()
    if backend == "auto":
        backend = detect_backend()
    is_claude = backend == "claude"

    # Resolve defaults based on backend
    if not args.codex_cmd:
        args.codex_cmd = "claude" if is_claude else "codex"
    if not args.tmux_command:
        args.tmux_command = "claude" if is_claude else "codex"
    if not args.tag_assistant:
        args.tag_assistant = "[Claude]" if is_claude else "[Codex]"

    if is_claude:
        # Claude Code: conversation file lives under ~/.claude/projects/<key>/
        history_path = Path(args.history_path).expanduser() if args.history_path != str(CODEX_HISTORY_PATH) else CLAUDE_HISTORY_PATH
        session_id = (args.session_id or "").strip()
        if not session_id:
            session_id = latest_claude_session_id(WORKSPACE_ROOT)
        if not session_id:
            session_id = latest_claude_history_session_id(WORKSPACE_ROOT)
        if not session_id:
            raise SystemExit("relay-daemon: no se pudo detectar session_id de Claude Code (usar --session-id)")
        session_file = find_claude_conversation_file(session_id, WORKSPACE_ROOT)
        if session_file is None:
            raise SystemExit(f"relay-daemon: no se encontro conversation file de Claude Code para {session_id}")
    else:
        # Codex backend (original)
        history_path = Path(args.history_path).expanduser()
        session_id = (args.session_id or "").strip()
        if not session_id:
            session_id = latest_workspace_session_id(WORKSPACE_ROOT)
        if not session_id:
            session_id = latest_history_session_id(history_path)
        if not session_id:
            raise SystemExit("relay-daemon: no se pudo detectar session_id (usar --session-id)")
        session_file = find_session_file(session_id)
        if session_file is None:
            raise SystemExit(f"relay-daemon: no se encontro session file para {session_id}")

    relay_state = load_json(RELAY_STATE_PATH, {})
    history_offset = (
        history_path.stat().st_size
        if args.from_now and history_path.exists()
        else int(relay_state.get("history_offset", 0) or 0)
    )
    session_offset = (
        session_file.stat().st_size
        if args.from_now and session_file.exists()
        else int(relay_state.get("session_offset", 0) or 0)
    )

    if args.from_now and not webhook_enabled(config):
        state["update_offset"] = bootstrap_offset(config["bot_token"])
        save_state(state)

    pending_telegram_hash_counts = {}

    print(
        json.dumps(
            {
                "ok": True,
                "mode": "relay-daemon",
                "backend": backend,
                "session_id": session_id,
                "session_file": str(session_file),
                "history_path": str(history_path),
                "webhook_enabled": webhook_enabled(config),
                "codex_resume": args.codex_resume,
                "tmux_inject": args.tmux_inject,
                "tmux_target": args.tmux_target,
                "tmux_command": args.tmux_command,
                "workspace_root": str(WORKSPACE_ROOT),
                "state_dir": str(STATE_DIR),
            },
            ensure_ascii=True,
        ),
        flush=True,
    )

    while True:
        # Refresh session file in case it changed
        if is_claude:
            current_session_file = find_claude_conversation_file(session_id, WORKSPACE_ROOT)
        else:
            current_session_file = find_session_file(session_id)
        if current_session_file is not None and current_session_file != session_file:
            session_file = current_session_file
            session_offset = 0

        # --- Mirror CLI user input to Telegram ---
        if args.cli_mirror:
            history_rows, history_offset = read_new_jsonl_rows(history_path, history_offset)
            for row in history_rows:
                if is_claude:
                    # Claude history.jsonl uses "sessionId" and "display"
                    if str(row.get("sessionId", "")) != session_id:
                        continue
                    text = str(row.get("display", "")).strip()
                else:
                    if str(row.get("session_id", "")) != session_id:
                        continue
                    text = str(row.get("text", "")).strip()
                if not text:
                    continue
                digest = text_hash(text)
                pending = int(pending_telegram_hash_counts.get(digest, 0))
                if pending > 0:
                    pending_telegram_hash_counts[digest] = pending - 1
                    continue
                message = format_tagged_message(args.tag_cli, text, args.max_forward_chars)
                if message:
                    safe_send_chunked(config, message, args.max_telegram_chars)

        # --- Mirror assistant responses to Telegram ---
        if args.assistant_mirror:
            session_rows, session_offset = read_new_jsonl_rows(session_file, session_offset)
            for row in session_rows:
                if is_claude:
                    text, model_info = extract_claude_assistant_text(row)
                    if not text:
                        continue
                    phase_suffix = f" ({model_info})" if model_info else ""
                    tag = f"{args.tag_assistant}{phase_suffix}"
                else:
                    text, phase = extract_assistant_text(row)
                    if not text:
                        continue
                    phase_suffix = f" ({phase})" if phase else ""
                    tag = f"{args.tag_assistant}{phase_suffix}"
                message = format_tagged_message(tag, text, args.max_forward_chars)
                if message:
                    safe_send_chunked(config, message, args.max_telegram_chars)

        # --- Receive incoming Telegram messages ---
        incoming = []
        while True:
            queued = inbox_next()
            if queued is None:
                break
            incoming.append(queued)
            if args.max_inbox_batch > 0 and len(incoming) >= args.max_inbox_batch:
                break

        if not incoming and not webhook_enabled(config):
            incoming = poll_updates(config, state, min(args.long_poll, 25))

        # --- Dispatch incoming to CLI/resume ---
        for msg in incoming:
            text = str(msg.get("text", "")).strip()
            if not text:
                continue
            if args.ack_text:
                ack_message = format_tagged_message("", args.ack_text, args.max_forward_chars)
                if ack_message:
                    safe_send_chunked(config, ack_message, args.max_telegram_chars)

            prompt = text
            if args.telegram_prompt_prefix:
                prompt = f"{args.telegram_prompt_prefix}\n{text}"
            digest = text_hash(text)
            pending_telegram_hash_counts[digest] = int(pending_telegram_hash_counts.get(digest, 0)) + 1
            pending_telegram_hash_counts[text_hash(prompt)] = int(
                pending_telegram_hash_counts.get(text_hash(prompt), 0)
            ) + 1

            dispatched = False

            if args.tmux_inject:
                tmux_target, tmux_target_error = resolve_tmux_target(args.tmux_target, args.tmux_command)
                if tmux_target:
                    ok, detail = tmux_inject_message(tmux_target, prompt, press_enter=args.tmux_enter)
                else:
                    ok, detail = False, tmux_target_error
                print(
                    json.dumps(
                        {
                            "ok": ok,
                            "event": "tmux_dispatch",
                            "tmux_target": tmux_target,
                            "detail": detail,
                            "text_hash": digest,
                            "ts": int(time.time()),
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                dispatched = ok
                if not ok:
                    err_message = format_tagged_message(args.tag_error, detail, args.max_forward_chars)
                    if err_message:
                        safe_send_chunked(config, err_message, args.max_telegram_chars)
                    if not args.codex_resume_fallback:
                        continue

            if args.codex_resume and (not dispatched or args.codex_resume_always):
                if is_claude:
                    ok, detail = run_claude_resume(
                        session_id=session_id,
                        prompt_text=prompt,
                        claude_cmd=args.codex_cmd,
                        timeout_sec=args.codex_timeout,
                        detach=args.codex_detach,
                    )
                    event_name = "claude_resume_dispatch"
                else:
                    ok, detail = run_codex_resume(
                        session_id=session_id,
                        prompt_text=prompt,
                        codex_cmd=args.codex_cmd,
                        timeout_sec=args.codex_timeout,
                        full_auto=args.codex_full_auto,
                        detach=args.codex_detach,
                    )
                    event_name = "codex_resume_dispatch"
                print(
                    json.dumps(
                        {
                            "ok": ok,
                            "event": event_name,
                            "session_id": session_id,
                            "detail": detail,
                            "text_hash": digest,
                            "ts": int(time.time()),
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                if not ok:
                    err_message = format_tagged_message(args.tag_error, detail, args.max_forward_chars)
                    if err_message:
                        safe_send_chunked(config, err_message, args.max_telegram_chars)
                    continue
                dispatched = True

            if not dispatched:
                err_message = format_tagged_message(
                    args.tag_error,
                    "mensaje de Telegram recibido pero sin destino (activar --tmux-inject o --codex-resume)",
                    args.max_forward_chars,
                )
                if err_message:
                    safe_send_chunked(config, err_message, args.max_telegram_chars)

        save_json(
            RELAY_STATE_PATH,
            {
                "backend": backend,
                "session_id": session_id,
                "session_file": str(session_file),
                "history_path": str(history_path),
                "history_offset": history_offset,
                "session_offset": session_offset,
                "updated_at": int(time.time()),
            },
        )

        if webhook_enabled(config) or incoming:
            time.sleep(max(0.05, args.poll_interval))


def command_init_config(args):
    ensure_dir()
    config = {
        "bot_token": args.bot_token,
        "chat_id": int(args.chat_id),
        "user_id": int(args.user_id),
        "username": args.username or "",
    }
    save_config(config)
    state = {"update_offset": 0}
    try:
        state["update_offset"] = bootstrap_offset(args.bot_token)
    except Exception as exc:
        state["bootstrap_error"] = str(exc)
    save_state(state)
    print(
        json.dumps(
            {
                "ok": True,
                "config_path": str(CONFIG_PATH),
                "state_path": str(STATE_PATH),
                "chat_id": config["chat_id"],
                "user_id": config["user_id"],
                "bootstrap_offset": state.get("update_offset", 0),
                "bootstrap_error": state.get("bootstrap_error", ""),
            }
        )
    )


def command_bot_info(args):
    me = api_request(args.bot_token, "getMe", timeout=20)
    print(
        json.dumps(
            {
                "ok": True,
                "bot_id": me.get("id", 0),
                "bot_username": me.get("username", ""),
                "bot_first_name": me.get("first_name", ""),
                "bot": me,
            },
            ensure_ascii=True,
        )
    )


def command_await_private_chat(args):
    bot = api_request(args.bot_token, "getMe", timeout=20)
    if args.clear_webhook:
        delete_webhook_for_token(args.bot_token, args.drop_pending)

    state = {"update_offset": 0}
    if args.from_now:
        state["update_offset"] = bootstrap_offset(args.bot_token)

    match_text = (args.match_text or "").strip()
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    while True:
        timeout_sec = min(args.long_poll, 25)
        if deadline is not None:
            remaining = int(max(1, deadline - time.time()))
            timeout_sec = min(timeout_sec, remaining)

        updates = api_request(
            args.bot_token,
            "getUpdates",
            {
                "offset": str(state.get("update_offset", 0)),
                "timeout": str(timeout_sec),
            },
            timeout=timeout_sec + 10,
        )
        next_offset = state.get("update_offset", 0)
        for update in updates:
            next_offset = max(next_offset, update["update_id"] + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            from_user = message.get("from") or {}
            text = str(message.get("text") or "")
            if str(chat.get("type", "")) != "private":
                continue
            if match_text and match_text not in text:
                continue
            state["update_offset"] = next_offset
            print(
                json.dumps(
                    {
                        "ok": True,
                        "bot_id": bot.get("id", 0),
                        "bot_username": bot.get("username", ""),
                        "bot_first_name": bot.get("first_name", ""),
                        "chat_id": int(chat.get("id", 0) or 0),
                        "user_id": int(from_user.get("id", 0) or 0),
                        "username": from_user.get("username", ""),
                        "first_name": from_user.get("first_name", ""),
                        "last_name": from_user.get("last_name", ""),
                        "chat_type": chat.get("type", ""),
                        "message_text": text,
                        "update_offset": state["update_offset"],
                    },
                    ensure_ascii=True,
                )
            )
            return
        state["update_offset"] = next_offset
        if deadline is not None and time.time() >= deadline:
            raise SystemExit(124)


def command_doctor(_args):
    config = load_config()
    state = load_state()
    me = api_request(config["bot_token"], "getMe", timeout=20)
    inbox_size = len(load_jsonl(INBOX_PATH))
    print(
        json.dumps(
            {
                "ok": True,
                "bot_username": me.get("username", ""),
                "bot_id": me.get("id", 0),
                "chat_id": config["chat_id"],
                "user_id": config["user_id"],
                "state_offset": state.get("update_offset", 0),
                "webhook_url": config.get("webhook_url", ""),
                "inbox_size": inbox_size,
            }
        )
    )


def command_sync_offset(_args):
    config = load_config()
    state = load_state()
    state["update_offset"] = bootstrap_offset(config["bot_token"])
    save_state(state)
    print(json.dumps({"ok": True, "update_offset": state["update_offset"]}))


def command_send(args):
    config = load_config()
    text = args.text
    if args.stdin:
        text = sys.stdin.read()
    if not text:
        raise SystemExit("send requiere --text o --stdin")
    result = send_message(config, text)
    print(
        json.dumps(
            {
                "ok": True,
                "source": "telegram",
                "chat_id": config["chat_id"],
                "message_id": result.get("message_id", 0),
            }
        )
    )


def command_poll(args):
    config = load_config()
    state = load_state()
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    while True:
        queued = inbox_next()
        if queued is not None:
            print(json.dumps(queued, ensure_ascii=True))
            return
        if webhook_enabled(config):
            if deadline is not None and time.time() >= deadline:
                raise SystemExit(124)
            time.sleep(1.0)
            continue
        messages = poll_updates(config, state, min(args.long_poll, 25))
        if messages:
            print(json.dumps(messages[0], ensure_ascii=True))
            return
        if deadline is not None and time.time() >= deadline:
            raise SystemExit(124)


def command_ask(args):
    config = load_config()
    state = load_state()
    if args.text:
        send_message(config, args.text)

    deadline = time.time() + args.timeout if args.timeout > 0 else None
    while True:
        line = read_stdin_line()
        if line is not None:
            print(json.dumps({"source": "stdin", "text": line}, ensure_ascii=True))
            return

        queued = inbox_next()
        if queued is not None:
            print(json.dumps(queued, ensure_ascii=True))
            return

        if webhook_enabled(config):
            if deadline is not None and time.time() >= deadline:
                raise SystemExit(124)
            time.sleep(1.0)
            continue

        long_poll = min(args.long_poll, 5)
        messages = poll_updates(config, state, long_poll)
        if messages:
            print(json.dumps(messages[0], ensure_ascii=True))
            return

        if deadline is not None and time.time() >= deadline:
            raise SystemExit(124)


def command_listen(args):
    config = load_config()
    state = load_state()
    if args.from_now:
      state["update_offset"] = bootstrap_offset(config["bot_token"])
      save_state(state)
    if args.text:
        send_message(config, args.text)

    while True:
        messages = poll_updates(config, state, min(args.long_poll, 25))
        for message in messages:
            append_jsonl(INBOX_PATH, message)
            if args.ack_text:
                ack = args.ack_text.format(
                    text=message.get("text", ""),
                    source=message.get("source", "telegram"),
                    username=message.get("username", ""),
                )
                if ack:
                    send_message(config, ack)
        if args.once and messages:
            return


def command_inbox_next(_args):
    message = inbox_next()
    if message is None:
        raise SystemExit(124)
    print(json.dumps(message, ensure_ascii=True))


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def build_webhook_handler(config, ack_text: str):
    secret = config.get("webhook_secret", "")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/telegram":
                self.send_response(404)
                self.end_headers()
                return
            if secret:
                header_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if header_secret != secret:
                    self.send_response(403)
                    self.end_headers()
                    return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length)
            try:
                update = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            message = normalize_update(config, update)
            if message is not None:
                append_jsonl(INBOX_PATH, message)
                if ack_text:
                    ack = ack_text.format(
                        text=message.get("text", ""),
                        source=message.get("source", "telegram"),
                        username=message.get("username", ""),
                    )
                    if ack:
                        try:
                            send_message(config, ack)
                        except Exception:
                            pass
            body = b"{\"ok\":true}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def command_webhook_serve(args):
    config = load_config()
    ensure_webhook_secret(config)
    server = ThreadedHTTPServer((args.host, args.port), build_webhook_handler(config, args.ack_text))
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "webhook-server",
                "host": args.host,
                "port": args.port,
                "health_url": f"http://{args.host}:{args.port}/health",
            },
            ensure_ascii=True,
        )
    )
    server.serve_forever()


def command_set_webhook(args):
    config = load_config()
    secret = ensure_webhook_secret(config)
    result = api_request(
        config["bot_token"],
        "setWebhook",
        {
            "url": args.url,
            "secret_token": secret,
            "drop_pending_updates": "true" if args.drop_pending else "false",
        },
        timeout=20,
    )
    config["webhook_url"] = args.url
    save_config(config)
    print(json.dumps({"ok": True, "result": result, "webhook_url": args.url}, ensure_ascii=True))


def command_webhook_info(_args):
    config = load_config()
    info = api_request(config["bot_token"], "getWebhookInfo", timeout=20)
    print(json.dumps({"ok": True, "info": info}, ensure_ascii=True))


def command_delete_webhook(args):
    config = load_config()
    result = delete_webhook_for_token(config["bot_token"], args.drop_pending)
    config.pop("webhook_url", None)
    save_config(config)
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))


def command_ngrok_url(args):
    payload = http_json_get(args.api_url, timeout=10)
    tunnels = payload.get("tunnels", [])
    for tunnel in tunnels:
        public_url = tunnel.get("public_url", "")
        cfg = tunnel.get("config", {})
        addr = str(cfg.get("addr", ""))
        if args.port and str(args.port) not in addr:
            continue
        if public_url.startswith("https://"):
            print(json.dumps({"ok": True, "public_url": public_url, "addr": addr}, ensure_ascii=True))
            return
    raise SystemExit(124)


def build_parser():
    parser = argparse.ArgumentParser(description="TelecodexBot bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init-config", help="Create local ignored Telegram config")
    init_cmd.add_argument("--bot-token", required=True)
    init_cmd.add_argument("--chat-id", required=True, type=int)
    init_cmd.add_argument("--user-id", required=True, type=int)
    init_cmd.add_argument("--username", default="")
    init_cmd.set_defaults(func=command_init_config)

    bot_info_cmd = sub.add_parser("bot-info", help="Validate a Telegram bot token and show bot metadata")
    bot_info_cmd.add_argument("--bot-token", required=True)
    bot_info_cmd.set_defaults(func=command_bot_info)

    await_private_chat_cmd = sub.add_parser(
        "await-private-chat",
        help="Wait for a matching private Telegram message using a bot token",
    )
    await_private_chat_cmd.add_argument("--bot-token", required=True)
    await_private_chat_cmd.add_argument("--match-text", default="")
    await_private_chat_cmd.add_argument("--timeout", type=int, default=180)
    await_private_chat_cmd.add_argument("--long-poll", type=int, default=10)
    await_private_chat_cmd.add_argument("--from-now", action="store_true")
    await_private_chat_cmd.add_argument("--clear-webhook", action="store_true")
    await_private_chat_cmd.add_argument("--drop-pending", action="store_true")
    await_private_chat_cmd.set_defaults(func=command_await_private_chat)

    doctor_cmd = sub.add_parser("doctor", help="Check bot connectivity")
    doctor_cmd.set_defaults(func=command_doctor)

    sync_cmd = sub.add_parser("sync-offset", help="Advance Telegram offset to latest update")
    sync_cmd.set_defaults(func=command_sync_offset)

    send_cmd = sub.add_parser("send", help="Send a Telegram message")
    send_cmd.add_argument("--text", default="")
    send_cmd.add_argument("--stdin", action="store_true")
    send_cmd.set_defaults(func=command_send)

    poll_cmd = sub.add_parser("poll", help="Wait for the next Telegram reply")
    poll_cmd.add_argument("--timeout", type=int, default=0)
    poll_cmd.add_argument("--long-poll", type=int, default=10)
    poll_cmd.set_defaults(func=command_poll)

    ask_cmd = sub.add_parser("ask", help="Send a question and wait for Telegram or stdin reply")
    ask_cmd.add_argument("--text", default="")
    ask_cmd.add_argument("--timeout", type=int, default=0)
    ask_cmd.add_argument("--long-poll", type=int, default=5)
    ask_cmd.set_defaults(func=command_ask)

    listen_cmd = sub.add_parser("listen", help="Run a background long-poll listener and append messages to inbox")
    listen_cmd.add_argument("--text", default="")
    listen_cmd.add_argument("--ack-text", default="")
    listen_cmd.add_argument("--long-poll", type=int, default=20)
    listen_cmd.add_argument("--from-now", action="store_true")
    listen_cmd.add_argument("--once", action="store_true")
    listen_cmd.set_defaults(func=command_listen)

    inbox_cmd = sub.add_parser("inbox-next", help="Pop the next queued Telegram message from inbox")
    inbox_cmd.set_defaults(func=command_inbox_next)

    webhook_serve_cmd = sub.add_parser("webhook-serve", help="Run local Telegram webhook receiver")
    webhook_serve_cmd.add_argument("--host", default="127.0.0.1")
    webhook_serve_cmd.add_argument("--port", type=int, default=8765)
    webhook_serve_cmd.add_argument("--ack-text", default="")
    webhook_serve_cmd.set_defaults(func=command_webhook_serve)

    set_webhook_cmd = sub.add_parser("set-webhook", help="Register Telegram webhook URL")
    set_webhook_cmd.add_argument("--url", required=True)
    set_webhook_cmd.add_argument("--drop-pending", action="store_true")
    set_webhook_cmd.set_defaults(func=command_set_webhook)

    webhook_info_cmd = sub.add_parser("webhook-info", help="Get Telegram webhook info")
    webhook_info_cmd.set_defaults(func=command_webhook_info)

    delete_webhook_cmd = sub.add_parser("delete-webhook", help="Delete Telegram webhook")
    delete_webhook_cmd.add_argument("--drop-pending", action="store_true")
    delete_webhook_cmd.set_defaults(func=command_delete_webhook)

    ngrok_url_cmd = sub.add_parser("ngrok-url", help="Read current ngrok public URL from local API")
    ngrok_url_cmd.add_argument("--port", type=int, default=0)
    ngrok_url_cmd.add_argument(
        "--api-url",
        default=os.environ.get("TELECODEXBOT_NGROK_API_URL", "http://127.0.0.1:4040/api/tunnels"),
    )
    ngrok_url_cmd.set_defaults(func=command_ngrok_url)

    relay_cmd = sub.add_parser(
        "relay-daemon",
        help="Mirror Codex/Claude CLI session messages to Telegram and inject Telegram replies",
    )
    relay_cmd.add_argument("--backend", default="auto", choices=["codex", "claude", "auto"],
                           help="Backend CLI to use (default: auto-detect)")
    relay_cmd.add_argument("--session-id", default="")
    relay_cmd.add_argument("--history-path", default=str(CODEX_HISTORY_PATH))
    relay_cmd.add_argument("--from-now", action="store_true")
    relay_cmd.add_argument("--long-poll", type=int, default=12)
    relay_cmd.add_argument("--poll-interval", type=float, default=0.8)
    relay_cmd.add_argument("--max-inbox-batch", type=int, default=20)
    relay_cmd.add_argument("--max-telegram-chars", type=int, default=3900)
    relay_cmd.add_argument("--max-forward-chars", type=int, default=12000)
    relay_cmd.add_argument("--codex-cmd", default="")
    relay_cmd.add_argument("--codex-timeout", type=int, default=0)
    relay_cmd.add_argument("--telegram-prompt-prefix", default="")
    relay_cmd.add_argument("--ack-text", default="Recibido por TelecodexBot. Lo envio al asistente.")
    relay_cmd.add_argument("--tmux-target", default="")
    relay_cmd.add_argument("--tmux-command", default="")
    relay_cmd.add_argument("--tmux-inject", action="store_true")
    relay_cmd.add_argument("--no-tmux-enter", dest="tmux_enter", action="store_false")
    relay_cmd.add_argument("--codex-resume-fallback", action="store_true")
    relay_cmd.add_argument("--codex-resume-always", action="store_true")
    relay_cmd.add_argument("--tag-cli", default="[Usuario via CLI]")
    relay_cmd.add_argument("--tag-assistant", default="")
    relay_cmd.add_argument("--tag-error", default="[TelecodexBot error]")
    relay_cmd.add_argument("--no-cli-mirror", dest="cli_mirror", action="store_false")
    relay_cmd.add_argument("--no-assistant-mirror", dest="assistant_mirror", action="store_false")
    relay_cmd.add_argument("--no-codex-resume", dest="codex_resume", action="store_false")
    relay_cmd.add_argument("--no-codex-full-auto", dest="codex_full_auto", action="store_false")
    relay_cmd.add_argument("--no-codex-detach", dest="codex_detach", action="store_false")
    relay_cmd.set_defaults(
        func=command_relay_daemon,
        cli_mirror=True,
        assistant_mirror=True,
        codex_resume=True,
        codex_full_auto=True,
        codex_detach=True,
        tmux_enter=True,
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Telegram HTTP error: {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Telegram URL error: {exc.reason}")
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
