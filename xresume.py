#!/usr/bin/env python3
"""xresume - resume a coding-agent conversation, in the CLI it came from or in another one.

Run `xresume` with no arguments. It opens a full-screen list of your Claude Code, Codex,
Antigravity (agy), OpenCode and Grok conversations, newest first.

  Up/Down, PgUp/PgDn, Home/End   move
  /                              search (tool, title, folder, id); Esc cancels the search
  Enter                          resume the conversation in the CLI it came from
  Right                          open the conversation in another CLI: a menu lists them, Up/Down
                                 choose, Enter opens, Esc closes the menu
  Space or p                     show or hide a preview of the selected conversation's last messages
                                 (Ctrl+P while searching)
  Tab                            turn skip-permissions on or off for the launch. The footer shows the
                                 exact flags: Claude Code and agy get --dangerously-skip-permissions,
                                 Codex gets --yolo --no-daemon, OpenCode gets --auto, Grok gets
                                 --always-approve. Off every time xresume starts.
  Ctrl+C twice, or Esc           exit
  Mouse                          hover marks a row, click selects, double-click opens, wheel scrolls.
                                 Hold Shift while dragging to select text the normal way.

When you quit the CLI you are back in the list, so you can open the same conversation in
another CLI, or a different one, without starting xresume again.

Moving a conversation to another CLI is a real import: xresume writes a session in that CLI's
own format (messages, tool calls and their results) and runs that CLI's own resume, so the
history is the conversation itself, not a pasted summary. A conversation keeps one session per
CLI: move it back later and only the new turns are added to the session it already has there,
and the list shows each conversation once, as its most recent copy. Before adding to an
existing session, xresume saves a copy of it in ~/.xresume/backups (the last five per session).
OpenCode is the exception: it cannot take new turns into an existing session, so a
conversation moved back to it becomes a new OpenCode session and the earlier one is left alone.
Conversations with no title are not listed.

Standard library only; Python 3.8+.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The release date in UTC, as YEAR.MONTH.DAY; a second release on the same day adds .1, and so on.
# The release workflow sets it when it publishes; there is no need to change it by hand.
__version__ = "2026.11.16"

HOME = Path.home()
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# Text the harnesses inject as "user" messages; not something the user typed.
INJECTED = re.compile(
    r"^\s*<(environment_context|user_instructions|skills_instructions|turn_aborted|system-reminder|"
    r"collaboration_mode|permissions|apps_instructions|plugins_instructions|subagent_notification|"
    r"user_shell_command|command-name|command-message|local-command-stdout|local-command-stderr|"
    r"local-command-caveat|task-notification)\b"
)


# ---------- helpers ----------

def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def loads(s):
    try:
        return json.loads(s)
    except ValueError:
        return None


def get(o, k):
    return o.get(k) if isinstance(o, dict) else None


def iso_now():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def to_dt(ts):
    # Older Pythons only parse fractions of exactly 3 or 6 digits, so normalise to 6.
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), str(ts).replace("Z", "+00:00"), count=1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def uuid7():
    t = "%012x" % int(time.time() * 1000)
    h = os.urandom(10).hex()
    return "%s-%s-7%s-%s%s-%s" % (t[:8], t[8:], h[:3], "89ab"[int(h[19], 16) % 4], h[4:7], h[7:19])


def clip(s, limit):
    s = "" if s is None else str(s)
    if limit <= 0 or len(s) <= limit:
        return s
    half = limit // 2
    return "%s\n... [%d characters trimmed by xresume] ...\n%s" % (s[:half], len(s) - 2 * half, s[-half:])


def one_line(s, limit):
    t = " ".join(str(s or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def flatten(c):
    """Message / tool-result content can be a string, a list of blocks, or an object wrapping one."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        for k in ("text", "content", "output"):
            if c.get(k) is not None:
                return flatten(c[k])
        return dumps(c)
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, str):
                parts.append(b)
            elif get(b, "type") in ("text", "input_text", "output_text"):
                parts.append(str(b.get("text") or ""))
            elif get(b, "type") in ("image", "input_image"):
                parts.append("[image]")
        return "\n".join(parts)
    return str(c)


def read_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line.rstrip("\n")


def read_tail(path, n):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read().decode("utf-8", errors="replace")


def tail_lines(path, n):
    """Lines from the last n bytes of a file, without the partial line the cut lands in."""
    lines = read_tail(path, n).split("\n")
    return lines if os.path.getsize(path) <= n else lines[1:]


STATE_DIR = Path(os.environ.get("XRESUME_HOME") or HOME / ".xresume")


def backup(path, copy=shutil.copy2, name=None):
    """Save a copy of a session in ~/.xresume/backups before changing it. The newest five
    copies of each session are kept. `name` replaces the file's own name in the backup, for
    files whose names are the same in every session."""
    d, p = STATE_DIR / "backups", Path(path)
    stem = name or p.stem
    d.mkdir(parents=True, exist_ok=True)
    # Named by UTC time down to the microsecond, so a second backup in the same second cannot
    # overwrite the first and a clock change cannot reorder them.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    copy(str(p), str(d / ("%s.%s%s" % (stem, stamp, p.suffix))))
    # Backups named by an earlier version (local time, 20261116-120000) are all older than these.
    for old in sorted(d.glob(stem + ".*" + p.suffix), key=lambda f: (f.name[len(stem) + 9:len(stem) + 10] == "T", f.name))[:-5]:
        try:
            old.unlink()
        except OSError:
            pass


def long_path(p):
    """The path as an absolute one that, on Windows, is not held to the 260-character limit
    (deep project folders give long session paths, and not every PC lifts that limit)."""
    s = os.path.abspath(str(p))
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return Path(s)
    return Path("\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s)


_B3_IV = (0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19)
_B3_PERM = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)


def _b3_compress(cv, block, length, counter, flags):
    m = list(struct.unpack("<16I", block.ljust(64, b"\0")))
    s = list(cv) + list(_B3_IV[:4]) + [counter & 0xFFFFFFFF, counter >> 32, length, flags]

    def g(a, b, c, d, x, y):
        s[a] = (s[a] + s[b] + x) & 0xFFFFFFFF; s[d] = ((s[d] ^ s[a]) >> 16 | (s[d] ^ s[a]) << 16) & 0xFFFFFFFF
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] = ((s[b] ^ s[c]) >> 12 | (s[b] ^ s[c]) << 20) & 0xFFFFFFFF
        s[a] = (s[a] + s[b] + y) & 0xFFFFFFFF; s[d] = ((s[d] ^ s[a]) >> 8 | (s[d] ^ s[a]) << 24) & 0xFFFFFFFF
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] = ((s[b] ^ s[c]) >> 7 | (s[b] ^ s[c]) << 25) & 0xFFFFFFFF
    for r in range(7):
        g(0, 4, 8, 12, m[0], m[1]); g(1, 5, 9, 13, m[2], m[3]); g(2, 6, 10, 14, m[4], m[5]); g(3, 7, 11, 15, m[6], m[7])
        g(0, 5, 10, 15, m[8], m[9]); g(1, 6, 11, 12, m[10], m[11]); g(2, 7, 8, 13, m[12], m[13]); g(3, 4, 9, 14, m[14], m[15])
        m = [m[i] for i in _B3_PERM]
    return [s[i] ^ s[i + 8] for i in range(8)]


def blake3_hex(data):
    """BLAKE3 of `data`, as 64 hex digits (Grok names some folders with it; Python has no BLAKE3)."""
    def chunk(start, counter):
        # A 1 KB chunk run up to its last block; returns what the last compression takes.
        part = data[start:start + 1024]
        blocks = [part[i:i + 64] for i in range(0, len(part), 64)] or [b""]
        cv = _B3_IV
        for i, b in enumerate(blocks[:-1]):
            cv = _b3_compress(cv, b, 64, counter, 1 if i == 0 else 0)
        return cv, blocks[-1], len(blocks[-1]), counter, (1 if len(blocks) == 1 else 0) | 2

    def node(start, n):
        if n == 1:
            return chunk(start * 1024, start)
        left = 1 << ((n - 1).bit_length() - 1)              # the largest power of two below n
        cvs = [_b3_compress(*node(start + k, size)) for k, size in ((0, left), (left, n - left))]
        return _B3_IV, struct.pack("<16I", *(cvs[0] + cvs[1])), 64, 0, 4

    cv, block, length, counter, flags = node(0, max(1, -(-len(data) // 1024)))
    return struct.pack("<8I", *_b3_compress(cv, block, length, counter, flags | 8)).hex()


def write_file(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def typed(text):
    """Is this user-role text something a person typed, rather than injected by a harness or xresume?"""
    return bool(text.strip()) and not INJECTED.match(text) and not text.startswith("[xresume]")


def append_text(path, lines):
    """Append lines to a file, starting on a fresh line even if the file does not end with one."""
    ends_clean = True
    if os.path.exists(path):
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            ends_clean = f.tell() == 0 or (f.seek(-1, os.SEEK_END) is not None and f.read(1) == b"\n")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(("" if ends_clean else "\n") + "\n".join(lines) + "\n")


def append_lines(path, lines):
    """Append records to a session file another tool owns, after backing it up."""
    if lines:
        backup(path)
        append_text(path, lines)


def safe_name(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name or "")[:64] or "tool"


def child_env():
    """The environment for a CLI started from here. The standalone build of xresume points the
    library search path at its own bundled files, which must not leak into other programs."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            original = env.pop(var + "_ORIG", None)
            env.pop(var, None)
            if original is not None:
                env[var] = original
        for var in [v for v in env if v.startswith("_PYI_") or v == "_MEIPASS2"]:
            del env[var]
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            except Exception:
                pass
    return env


def cli_version(exe, default):
    try:
        out = subprocess.run([shutil.which(exe) or exe, "--version"], capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=child_env()).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        return m.group(0) if m else default
    except Exception:
        return default


def join_calls(events, max_output):
    """Fold each result into its call so every call is followed by exactly one output.
    Both APIs reject a history where that pairing is broken."""
    results = {}
    for e in events:
        if e["k"] == "result" and e.get("id") and e["id"] not in results:
            results[e["id"]] = e.get("text")
    out, used = [], set()
    for e in events:
        if e["k"] == "result":
            continue
        if e["k"] == "call":
            if not e.get("id") or e["id"] in used:
                continue
            used.add(e["id"])
            o = results.get(e["id"], "(no output was recorded for this call)")
            e["output"] = clip(o if str(o or "").strip() else "(no output)", max_output)
        out.append(e)
    return out


def note(src_label):
    return ("[xresume] This conversation was started in %s and imported here. Tool calls earlier in the "
            "history were made with that tool's toolset, which is not available now; use your own tools "
            "from here on." % src_label)


# ---------- adapters ----------
# An adapter lists a tool's sessions, reads one into neutral events, and writes events back out as a
# native session.  event: {"k": "user"|"assistant"|"call"|"result", text, id, name, input, ts}

class Session:
    def __init__(self, tool, sid, path, cwd, title, mtime):
        self.tool, self.id, self.path, self.cwd, self.title, self.mtime = tool, sid, path, cwd, title, mtime


class Adapter:
    """What the picker and the hand-off need from a CLI. `sessions()` lists conversations,
    `read()` turns one into events, `write()` creates one from events, and `append()` adds
    events to one that exists."""
    can_append = True        # False when the CLI has no way to add turns to an existing session
    free_flags = []          # the flags Tab switches on: run without permission prompts

    def exists(self, path, sid):
        return os.path.isfile(str(path))


def claude_project_dir(cwd):
    """The folder name Claude Code keeps a project's sessions under, made the way Claude Code
    makes it: every character other than an ASCII letter or digit becomes "-" (one per UTF-16
    unit, so an emoji becomes "--"), and a name over 200 characters is cut and given a hash of
    the full path."""
    units = cwd.encode("utf-16-le")
    codes = [units[i] | units[i + 1] << 8 for i in range(0, len(units), 2)]
    name = "".join(chr(c) if c < 128 and chr(c).isalnum() else "-" for c in codes)
    if len(name) <= 200:
        return name
    h = 0
    for c in codes:
        h = (h * 31 + c) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >> 31 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return name[:200] + "-" + digits


class Claude(Adapter):
    name = "claude"
    label = "Claude Code"
    exe = "claude"

    def __init__(self):
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        self.root = long_path((Path(base) if base else HOME / ".claude") / "projects")

    def sessions(self):
        out = []
        if not self.root.is_dir():
            return out
        for f in self.root.glob("*/*.jsonl"):
            if re.fullmatch(UUID_RE, f.stem):
                try:
                    s = self._session(f)
                except OSError:
                    continue                             # unreadable or deleted while listing
                if s:
                    out.append(s)
        return out

    def _session(self, f):
        cwd = first = None
        for i, line in enumerate(read_lines(f)):
            if i >= 400:
                break
            if not line.startswith('{"parentUuid"') or len(line) > 400000:
                continue
            o = loads(line)
            cwd = cwd or get(o, "cwd")
            if not first and get(o, "type") == "user" and not get(o, "isMeta"):
                t = flatten(get(get(o, "message"), "content"))
                if typed(t):
                    first = t
            if cwd and first:
                break
        if not cwd:
            return None
        tail = read_tail(f, 131072)

        def last(key):
            found = re.findall(r'"%s":("(?:[^"\\]|\\.)*")' % key, tail)
            return loads(found[-1]) if found else None
        # A name the user gave the session wins over the generated one.
        title = last("customTitle") or last("aiTitle") or first
        return Session(self.name, f.stem, f, cwd, one_line(title, 70), f.stat().st_mtime)

    def read(self, path, tail=0, sid=None):
        """The conversation as events. With `tail`, only what the last `tail` bytes hold."""
        nodes, last = {}, None
        for line in (tail_lines(path, tail) if tail else read_lines(path)):
            if not line.startswith('{"parentUuid"'):
                if line.startswith('{"type":"api-request') or '"uuid":"' not in line:
                    continue
            o = loads(line)
            u = get(o, "uuid")
            if not u:
                continue
            if o.get("type") in ("user", "assistant"):
                nodes[u] = (o.get("parentUuid"), o)
                if not o.get("isSidechain"):
                    last = u
            else:
                nodes[u] = (o.get("parentUuid"), None)

        # The file is a tree (rewinds, forks). Walk from the newest message back to the root,
        # which is either the start or the last compaction boundary.
        chain, seen, u = [], set(), last
        while u and u in nodes and u not in seen:
            seen.add(u)
            parent, o = nodes[u]
            if o:
                chain.append(o)
            u = parent
        chain.reverse()

        ev = []
        for o in chain:
            if o.get("isMeta"):
                continue
            ts = o.get("timestamp") or ""
            content = get(o.get("message"), "content")
            if o["type"] == "user":
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                for b in content or []:
                    if get(b, "type") == "text":
                        t = str(b.get("text") or "")
                        if t.strip() and not INJECTED.match(t):
                            ev.append({"k": "user", "text": t, "ts": ts})
                    elif get(b, "type") == "tool_result":
                        ev.append({"k": "result", "id": str(b.get("tool_use_id") or ""), "text": flatten(b.get("content")), "ts": ts})
            else:
                for b in content or []:
                    if get(b, "type") == "text":
                        t = str(b.get("text") or "")
                        if t.strip():
                            ev.append({"k": "assistant", "text": t, "ts": ts})
                    elif get(b, "type") == "tool_use":
                        ev.append({"k": "call", "id": str(b.get("id") or ""), "name": str(b.get("name") or ""), "input": b.get("input") or {}, "ts": ts})
        return ev

    def _emit(self, ev, sid, cwd, parent):
        """Turn events into transcript lines chained onto `parent` (the uuid they follow)."""
        ver = cli_version(self.exe, "2.1.0")
        lines, prev = [], [parent]

        def add(kind, message, ts):
            u = str(uuid.uuid4())
            lines.append(dumps({
                "parentUuid": prev[0], "isSidechain": False, "type": kind, "message": message, "uuid": u,
                "timestamp": ts, "userType": "external", "entrypoint": "cli", "cwd": cwd, "sessionId": sid, "version": ver,
            }))
            prev[0] = u

        def asst(block, stop):
            return {"id": "msg_xr" + uuid.uuid4().hex[:22], "type": "message", "role": "assistant", "model": "xresume-import",
                    "content": [block], "stop_reason": stop, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}

        for e in ev:
            ts = e.get("ts") or iso_now()
            if e["k"] == "user":
                add("user", {"role": "user", "content": e["text"]}, ts)
            elif e["k"] == "assistant":
                add("assistant", asst({"type": "text", "text": e["text"]}, "end_turn"), ts)
            elif e["k"] == "call":
                cid = re.sub(r"[^a-zA-Z0-9_-]", "_", e["id"])
                inp = e["input"] if isinstance(e["input"], dict) else {"input": e["input"]}
                add("assistant", asst({"type": "tool_use", "id": cid, "name": safe_name(e["name"]), "input": inp}, "tool_use"), ts)
                add("user", {"role": "user", "content": [{"tool_use_id": cid, "type": "tool_result", "content": e["output"]}]}, ts)
        return lines

    def write(self, ev, cwd, title, src_label):
        """Create a new session holding these events."""
        sid = str(uuid.uuid4())
        d = self.root / claude_project_dir(cwd)
        d.mkdir(parents=True, exist_ok=True)
        first = {"k": "user", "text": note(src_label), "ts": (ev[0].get("ts") if ev else "") or iso_now()}
        lines = self._emit([first] + ev, sid, cwd, None)
        lines.append(dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid}))
        path = d / (sid + ".jsonl")
        write_file(path, "\n".join(lines) + "\n")
        return sid, path

    def append(self, path, ev):
        """Add events to the end of an existing session, continuing from its newest entry."""
        # The history is a chain of entries; the end of it can be a non-message entry (an
        # attachment, a system note), so continue from the last chained entry of any kind.
        leaf = cwd = None
        for line in read_lines(path):
            if not line.startswith('{"parentUuid"'):
                continue
            o = loads(line)
            if get(o, "uuid") and not o.get("isSidechain"):
                leaf, cwd = o["uuid"], o.get("cwd") or cwd
        if not leaf:
            raise ValueError("no messages found in %s" % path)
        sid = Path(path).stem
        lines = self._emit(ev, sid, cwd or os.getcwd(), leaf)
        if lines:
            # Claude Code records where the conversation currently ends; point that at the new end.
            asked = [e["text"] for e in ev if e["k"] == "user"]
            lines.append(dumps({"type": "last-prompt", "lastPrompt": one_line(asked[-1], 200) if asked else "",
                                "leafUuid": loads(lines[-1])["uuid"], "sessionId": sid}))
        append_lines(path, lines)

    # Flags Tab switches on in the picker: run without permission prompts.
    free_flags = ["--dangerously-skip-permissions"]

    def resume_cmd(self, sid, flags=()):
        return [self.exe] + list(flags) + ["--resume", sid]


def is_subagent(meta):
    """Is this Codex session a helper thread another conversation spawned? Codex cannot open one
    on its own, and it starts with a copy of its parent's history, so listing it would show a
    duplicate of the parent that fails to open. Every kind of helper counts (spawned agents,
    reviews, compaction): Codex's own resume list hides them all too."""
    source = meta.get("source")
    return (meta.get("thread_source") == "subagent" or bool(meta.get("parent_thread_id"))
            or (isinstance(source, dict) and "subagent" in source))


def codex_meta(line):
    """From a rollout's first line: (its folder, whether it is a sub-agent). The line can be tens
    of KB of instructions, so it is only parsed in full when it mentions a sub-agent at all."""
    if '"subagent"' in line or re.search(r'"parent_thread_id":\s*"', line):
        meta = get(loads(line), "payload")
        if isinstance(meta, dict) and is_subagent(meta):
            return None, True
    c = re.search(r'"cwd":\s*("(?:[^"\\]|\\.)*")', line)
    cwd = loads(c.group(1)) if c else None
    return (cwd if isinstance(cwd, str) else None), False


class Codex(Adapter):
    name = "codex"
    label = "the Codex CLI"
    exe = "codex"

    def __init__(self):
        self.home = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
        self.root = self.home / "sessions"
        self.index = self.home / "session_index.jsonl"

    def _files(self):
        return self.root.rglob("rollout-*.jsonl") if self.root.is_dir() else []

    def exists(self, path, sid):
        # A sub-agent linked before xresume skipped them cannot be resumed, so it is not a usable copy.
        if not os.path.isfile(str(path)):
            return False
        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as f:
                first = f.readline()
        except OSError:
            return False
        return not codex_meta(first)[1]

    def sessions(self):
        names = {}
        if self.index.is_file():
            for line in read_lines(self.index):
                o = loads(line)
                if get(o, "id"):
                    names[o["id"]] = o.get("thread_name")
        out = []
        for f in self._files():
            m = re.search("(" + UUID_RE + r")\.jsonl$", f.name)
            if not m:
                continue
            try:
                s = self._session(f, m.group(1), names.get(m.group(1)))
            except OSError:
                continue                                 # unreadable or deleted while listing
            if s:
                out.append(s)
        return out

    def _session(self, f, sid, title):
        cwd = None
        for i, line in enumerate(read_lines(f)):
            if i == 0:
                cwd, sub = codex_meta(line)
                if sub:
                    return None
                continue
            if title or i > 80:
                break
            if '"role":"user"' not in line or len(line) > 400000:
                continue
            p = get(loads(line), "payload")
            if get(p, "type") == "message" and get(p, "role") == "user":
                t = flatten(p.get("content"))
                if typed(t):
                    title = t
        return Session(self.name, sid, f, cwd, one_line(title, 70), f.stat().st_mtime) if cwd else None

    def read(self, path, tail=0, sid=None):
        """The conversation as events. With `tail`, only what the last `tail` bytes hold."""
        ev = []
        for line in (tail_lines(path, tail) if tail else read_lines(path)):
            if '"type":"response_item"' not in line and '"type":"compacted"' not in line:
                continue
            o = loads(line)
            top, ts, p = get(o, "type"), get(o, "timestamp") or "", get(o, "payload")
            if top == "compacted":
                # Everything before this point was replaced by a summary.
                ev = []
                s = str(get(p, "message") or "")
                if s.strip():
                    ev.append({"k": "user", "text": "[Summary of the earlier conversation]\n" + s, "ts": ts})
                continue
            if top != "response_item":
                continue
            kind = get(p, "type")
            if kind == "message":
                t = flatten(p.get("content"))
                if not t.strip():
                    continue
                if p.get("role") == "user":
                    if not INJECTED.match(t):
                        ev.append({"k": "user", "text": t, "ts": ts})
                elif p.get("role") == "assistant":
                    ev.append({"k": "assistant", "text": t, "ts": ts})
            elif kind == "function_call":
                a = str(p.get("arguments") or "")
                inp = loads(a)
                if not isinstance(inp, dict):
                    inp = {"input": a}
                ev.append({"k": "call", "id": str(p.get("call_id") or ""), "name": str(p.get("name") or ""), "input": inp, "ts": ts})
            elif kind == "custom_tool_call":
                ev.append({"k": "call", "id": str(p.get("call_id") or ""), "name": str(p.get("name") or ""), "input": {"input": str(p.get("input") or "")}, "ts": ts})
            elif kind in ("function_call_output", "custom_tool_call_output"):
                ev.append({"k": "result", "id": str(p.get("call_id") or ""), "text": flatten(p.get("output")), "ts": ts})
        return ev

    def _meta_template(self):
        """Borrow account / provider / base-instruction fields from the newest real interactive session."""
        for f in sorted(self._files(), key=lambda p: p.name, reverse=True):
            head = next(read_lines(f), "")
            if '"source":"cli"' not in head or '"imported_by"' in head:
                continue
            p = get(loads(head), "payload")
            if isinstance(p, dict):
                return p
        return {}

    def _emit(self, ev, tid, first_ordinal, lead=None):
        """Turn events into rollout records. `lead` is an opening user message that shares the
        first turn with the first real user message."""
        lines = []
        st = {"turn": None, "start": None, "agent": None, "last": iso_now()}

        def add(kind, payload, ts):
            lines.append(dumps({"timestamp": ts, "ordinal": first_ordinal + len(lines), "type": kind, "payload": payload}))

        def close():
            if st["turn"]:
                s, c = int(to_dt(st["start"]).timestamp()), int(to_dt(st["last"]).timestamp())
                add("event_msg", {"type": "task_complete", "root_turn_id": st["turn"], "turn_id": st["turn"],
                                  "last_agent_message": st["agent"], "started_at": s, "completed_at": c,
                                  "duration_ms": (c - s) * 1000}, st["last"])

        def open_turn(ts):
            close()
            st.update(turn=uuid7(), start=ts, agent=None)
            add("event_msg", {"type": "task_started", "turn_id": st["turn"], "root_turn_id": st["turn"],
                              "started_at": int(to_dt(ts).timestamp()), "collaboration_mode_kind": "default"}, ts)

        def user_msg(text, ts):
            add("response_item", {"type": "message", "id": "msg_" + uuid7(), "role": "user",
                                  "content": [{"type": "input_text", "text": text}]}, ts)
            # The user_message / agent_message events are what Codex redraws the conversation
            # from when a session is resumed; without them the screen starts empty.
            add("event_msg", {"type": "user_message", "client_id": str(uuid.uuid4()), "message": text, "images": [],
                              "local_images": [], "audio": [], "local_audio": [], "text_elements": []}, ts)

        fresh = False
        if lead:
            t0 = (ev[0].get("ts") if ev else "") or iso_now()
            open_turn(t0)
            user_msg(lead, t0)
            fresh = True
        for e in ev:
            ts = e.get("ts") or iso_now()
            st["last"] = ts
            if e["k"] == "user":
                if not fresh:
                    open_turn(ts)
                user_msg(e["text"], ts)
            elif st["turn"] is None:
                open_turn(ts)
            if e["k"] == "assistant":
                mid = "msg_" + uuid.uuid4().hex
                add("event_msg", {"type": "agent_message", "message": e["text"], "phase": "final_answer",
                                  "memory_citation": None}, ts)
                add("response_item", {"type": "message", "status": "completed", "id": mid, "role": "assistant",
                                      "content": [{"type": "output_text", "text": e["text"], "annotations": [], "logprobs": []}]}, ts)
                st["agent"] = e["text"]
            elif e["k"] == "call":
                name, inp = e["name"], e["input"]
                if name in ("Bash", "PowerShell") and get(inp, "command"):
                    name, inp = "exec_command", {"cmd": str(inp["command"])}
                add("response_item", {"type": "function_call", "status": "completed", "id": "fc_" + uuid.uuid4().hex,
                                      "name": safe_name(name), "arguments": dumps(inp), "call_id": e["id"]}, ts)
                add("response_item", {"type": "function_call_output", "id": "fco_" + uuid7(), "call_id": e["id"], "output": e["output"]}, ts)
            fresh = False
        close()
        return lines

    def write(self, ev, cwd, title, src_label):
        """Create a new session holding these events."""
        tid = uuid7()
        now = datetime.now()
        d = self.root / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        d.mkdir(parents=True, exist_ok=True)
        tpl = self._meta_template()
        meta = {k: tpl[k] for k in ("creator_user_id", "creator_account_id", "model_provider", "base_instructions") if k in tpl}
        meta.update({
            "session_id": tid, "id": tid, "timestamp": iso_now(), "cwd": cwd, "runtime_workspace_roots": [cwd],
            "originator": "codex-tui", "cli_version": cli_version(self.exe, "0.0.0"), "source": "cli",
            "thread_source": "user", "imported_by": "xresume",
        })
        lines = [dumps({"timestamp": iso_now(), "ordinal": 0, "type": "session_meta", "payload": meta})]
        lines += self._emit(ev, tid, 1, lead=note(src_label))
        path = d / ("rollout-%s-%s.jsonl" % (now.strftime("%Y-%m-%dT%H-%M-%S"), tid))
        write_file(path, "\n".join(lines) + "\n")
        try:
            append_text(self.index, [dumps({"id": tid, "thread_name": title,
                                            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})])
        except OSError:
            pass
        return tid, path

    def append(self, path, ev):
        """Add events to the end of an existing session as new turns."""
        m = re.search("(" + UUID_RE + r")\.jsonl$", str(path))
        if not m:
            raise ValueError("cannot tell the session id of %s" % path)
        # Continue the record numbering from the file's last record.
        count, last = 0, ""
        for line in read_lines(path):
            count += 1
            last = line if line.strip() else last
        n = get(loads(last), "ordinal")
        append_lines(path, self._emit(ev, m.group(1), n + 1 if isinstance(n, int) else count))

    # Flags Tab switches on in the picker: no approval prompts and no sandbox, and its own
    # process rather than the shared background server.
    free_flags = ["--yolo", "--no-daemon"]

    def resume_cmd(self, sid, flags=()):
        return [self.exe, "resume"] + list(flags) + [sid]


# ---------- protobuf, just enough to read and write agy's records without a schema ----------

def pb_varint(n):
    out = bytearray()
    n &= 0xFFFFFFFFFFFFFFFF                              # negatives are 64-bit two's complement
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_enc(fields):
    """Encode [(field number, value)]; a value is an int, str, bytes, or a nested list of fields."""
    out = bytearray()
    for f, v in fields:
        if isinstance(v, int):
            out += pb_varint(f << 3) + pb_varint(v)
        else:
            if isinstance(v, list):
                v = pb_enc(v)
            elif isinstance(v, str):
                v = v.encode("utf-8")
            out += pb_varint(f << 3 | 2) + pb_varint(len(v)) + v
    return bytes(out)


def pb_parse(b):
    """Decode one message level into [(field number, value)]; nested messages stay as bytes.
    Returns [] if the bytes are not a well-formed message."""
    out, i, n = [], 0, len(b or b"")
    try:
        while i < n:
            key = shift = 0
            while True:
                c = b[i]
                i += 1
                key |= (c & 0x7F) << shift
                shift += 7
                if not c & 0x80:
                    break
            f, w = key >> 3, key & 7
            if w == 0:
                v = shift = 0
                while True:
                    c = b[i]
                    i += 1
                    v |= (c & 0x7F) << shift
                    shift += 7
                    if not c & 0x80:
                        break
            elif w == 2:
                size = shift = 0
                while True:
                    c = b[i]
                    i += 1
                    size |= (c & 0x7F) << shift
                    shift += 7
                    if not c & 0x80:
                        break
                if i + size > n:
                    return []
                v = b[i:i + size]
                i += size
            elif w in (1, 5):
                size = 8 if w == 1 else 4
                v = b[i:i + size]
                i += size
            else:
                return []
            out.append((f, v))
    except IndexError:
        return []
    return out


def pb_get(b, *path):
    """First value at a path of field numbers, or None."""
    for f in path:
        if not isinstance(b, (bytes, bytearray)):
            return None
        b = next((v for k, v in pb_parse(b) if k == f), None)
    return b


def pb_all(b, f):
    return [v for k, v in pb_parse(b) if k == f] if isinstance(b, (bytes, bytearray)) else []


def pb_text(b, *path):
    v = pb_get(b, *path)
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ""


class Agy(Adapter):
    """Google Antigravity CLI. Each conversation is an SQLite database of steps whose records are
    protobuf. The layout is undocumented; this follows what agy 1.5 writes:

      step 14   user message          text at 19.2
      step 15   model response        text at 20.1, tool calls at 20.7 (1 id, 2 name, 3 arguments as JSON)
      step 132  tool call and result  the call at 5.4, the result text at 140.2.1
    """
    name = "agy"
    label = "Antigravity"
    exe = "agy"
    # Flag Tab switches on in the picker: auto-approve every tool permission request.
    free_flags = ["--dangerously-skip-permissions"]

    DDL = (
        "CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`));"
        "CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));"
        "CREATE INDEX `idx_steps_status` ON `steps`(`status`);"
        "CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`);"
        "CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));"
        "CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));"
        "CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));"
        "CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT \"main\",`data` blob,PRIMARY KEY (`id`));"
        "CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));"
    )

    def __init__(self):
        self.home = HOME / ".gemini" / "antigravity-cli"
        self.root = self.home / "conversations"
        self.summaries = self.home / "conversation_summaries.db"

    @staticmethod
    def _open(path, write=False):
        import sqlite3
        if write:
            return sqlite3.connect(str(path), timeout=15)
        return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)

    @staticmethod
    def _uri(path):
        from urllib.parse import quote
        return "file:///" + quote(str(path).replace("\\", "/").lstrip("/"), safe="/:")

    @staticmethod
    def _from_uri(uri):
        from urllib.parse import unquote
        rest = unquote(uri[len("file://"):]) if uri.startswith("file://") else uri
        if re.match(r"^/[A-Za-z]:", rest):
            return rest[1:].replace("/", "\\")
        return rest

    @staticmethod
    def _stamp(ts):
        t = to_dt(ts).timestamp()
        return [(1, int(t)), (2, int((t % 1) * 1e9))]

    def sessions(self):
        out = []
        if not self.root.is_dir():
            return out
        titles = {}
        try:
            con = self._open(self.summaries)
            for cid, title, preview in con.execute("select conversation_id, title, preview from conversation_summaries"):
                titles[cid] = title or preview
            con.close()
        except Exception:
            pass
        for f in self.root.glob("*.db"):
            if not re.fullmatch(UUID_RE, f.stem):
                continue
            try:
                con = self._open(f)
                blob = con.execute("select data from trajectory_metadata_blob limit 1").fetchone()
                asked = con.execute("select step_payload from steps where step_type = 14 order by idx limit 3").fetchall()
                con.close()
            except Exception:
                continue
            if not asked:
                continue                                 # an empty conversation
            cwd = self._from_uri(pb_text(blob[0], 7) or pb_text(blob[0], 1, 1)) if blob else ""
            title = titles.get(f.stem) or ""
            if not typed(title):                         # no title, or agy titled it after xresume's note
                title = next((t for t in (pb_text(p[0], 19, 2) for p in asked) if typed(t)), "")
            mtime = max(os.path.getmtime(str(q)) for q in (f, Path(str(f) + "-wal")) if q.exists())
            if cwd:
                out.append(Session(self.name, f.stem, f, cwd, one_line(title, 70), mtime))
        return out

    def read(self, path, tail=0, sid=None):
        """The conversation as events. With `tail`, only its last 80 steps."""
        ev = []
        con = self._open(path)
        rows = con.execute("select step_type, step_payload from (select idx, step_type, step_payload from steps "
                           "order by idx desc limit ?) order by idx", (80 if tail else -1,)).fetchall()
        con.close()
        for stype, payload in rows:
            meta = pb_get(payload, 5) or b""
            t = pb_get(meta, 1) or b""
            sec = pb_get(t, 1)
            ts = datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if isinstance(sec, int) else ""
            call = pb_get(meta, 4)
            if stype == 14:
                text = pb_text(payload, 19, 2)
                if text.strip():
                    ev.append({"k": "user", "text": text, "ts": ts})
            elif stype == 15:
                body = pb_get(payload, 20) or b""
                text = pb_text(body, 1)
                if text.strip():
                    ev.append({"k": "assistant", "text": text, "ts": ts})
                for c in pb_all(body, 7):
                    args = loads(pb_text(c, 3))
                    ev.append({"k": "call", "id": pb_text(c, 1), "name": pb_text(c, 2),
                               "input": args if isinstance(args, dict) else {"input": pb_text(c, 3)}, "ts": ts})
            elif call:
                # A tool step: its details sit in the one top-level field that is not 1, 4 or 5.
                detail = next((v for k, v in pb_parse(payload) if k not in (1, 4, 5) and isinstance(v, (bytes, bytearray))), b"")
                ev.append({"k": "result", "id": pb_text(call, 1), "text": pb_text(detail, 2, 1), "ts": ts})
        return ev

    def _steps(self, ev, cid, traj, first_idx):
        """Turn events into rows for the steps table: (idx, step_type, metadata, payload)."""
        rows, run = [], str(uuid.uuid4())

        def meta(i, ts, model, call=None):
            where = [(1, traj)] + ([(2, i)] if i else []) + [(4, cid)]
            m = [(1, self._stamp(ts)), (3, 2 if model else 4)]
            if call:
                m.append((4, call))
            if model:
                m += [(6, self._stamp(ts)), (7, self._stamp(ts)), (8, self._stamp(ts))]
            m += [(12, run), (20, where), (26, [(1, [(1, 3), (2, self._stamp(ts))])])]
            if model:
                m.append((32, self._stamp(ts)))
            return m + [(35, uuid.uuid4().hex)]

        def add(stype, m, body_field, body):
            rows.append((first_idx + len(rows), stype, pb_enc(m), pb_enc([(1, stype), (4, 3), (5, m), (body_field, body)])))

        for e in ev:
            ts, i = e.get("ts") or iso_now(), first_idx + len(rows)
            if e["k"] == "user":
                add(14, meta(i, ts, False), 19, [(2, e["text"]), (3, [(1, e["text"])])])
            elif e["k"] == "assistant":
                add(15, meta(i, ts, True), 20, [(1, e["text"]), (6, "bot-" + str(uuid.uuid4())), (8, e["text"]), (12, 2)])
            elif e["k"] == "call":
                inp = e["input"] if isinstance(e["input"], dict) else {"input": e["input"]}
                call = [(1, e["id"]), (2, safe_name(e["name"])), (3, dumps(inp))]
                add(15, meta(i, ts, True), 20, [(6, "bot-" + str(uuid.uuid4())), (7, call), (12, 2)])
                args = [(1, [(1, str(k)), (2, v if isinstance(v, str) else dumps(v))]) for k, v in inp.items()]
                add(132, meta(i + 1, ts, True, call), 140, args + [(2, [(1, e["output"])])])
        return rows

    def write(self, ev, cwd, title, src_label):
        """Create a new conversation holding these events, and list it in agy's own history."""
        cid, traj = str(uuid.uuid4()), str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (cid + ".db")
        first_ts = (ev[0].get("ts") if ev else "") or iso_now()
        rows = self._steps([{"k": "user", "text": note(src_label), "ts": first_ts}] + ev, cid, traj, 0)
        info = [(1, [(1, self._uri(cwd)), (3, b"")]), (2, self._stamp(first_ts)), (3, str(uuid.uuid4())), (6, cid),
                (7, self._uri(cwd)), (18, "default-cli-project")]
        con = self._open(path, write=True)
        try:
            con.executescript(self.DDL)
            con.execute("pragma user_version = 1")
            con.execute("insert into trajectory_meta values (?,?,4,17)", (traj, cid))
            con.execute("insert into trajectory_metadata_blob values ('main', ?)", (pb_enc(info),))
            con.executemany("insert into steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) "
                            "values (?,?,3,0,?,?,0)", rows)
            con.commit()
        except Exception:
            con.close()
            for ext in ("", "-wal", "-shm", "-journal"):     # do not leave a half-written conversation behind
                if os.path.exists(str(path) + ext):
                    os.remove(str(path) + ext)
            raise
        con.close()
        try:
            self._summarise(cid, traj, cwd, title, ev, len(rows), info)
        except Exception:
            pass          # agy rebuilds its history entry the first time the conversation is opened
        return cid, path

    def _summarise(self, cid, traj, cwd, title, ev, steps, info):
        if not self.summaries.is_file():
            return                                       # agy has not made its history index yet; leave that to agy

        def when(ts):
            d = to_dt(ts).astimezone(timezone.utc)
            return d.strftime("%Y-%m-%d %H:%M:%S") + ".%07d+00:00" % (d.microsecond * 10)
        first = (ev[0].get("ts") if ev else "") or iso_now()
        last = (ev[-1].get("ts") if ev else "") or iso_now()
        asked = [e.get("ts") for e in ev if e["k"] == "user"]
        last_user = (asked[-1] if asked else "") or first
        raw = pb_enc([(1, title), (2, steps), (3, self._stamp(last)), (4, traj), (5, 1), (7, self._stamp(first)),
                      (9, [(1, self._uri(cwd)), (3, b"")]), (10, self._stamp(last_user)), (15, [(1, title)]), (16, 0),
                      (17, info), (22, steps), (28, steps)])
        con = self._open(self.summaries, write=True)
        con.execute("insert or ignore into conversation_summaries (conversation_id, title, preview, step_count, last_modified_time, "
                    "workspace_uris, status, project_id, last_user_input_time, last_user_input_step_index, app_data_dir, raw_summary) "
                    "values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, title, title, steps, when(last), json.dumps([self._uri(cwd)]), "CASCADE_RUN_STATUS_IDLE",
                     "default-cli-project", when(last_user), -1, "antigravity-cli", raw))
        con.commit()
        con.close()

    def append(self, path, ev):
        """Add events to the end of an existing conversation, after backing the database up."""
        if not ev:
            return
        import sqlite3
        con = self._open(path, write=True)

        def snapshot(_src, dst):
            # SQLite's own backup, so the copy includes anything still in the write-ahead log.
            out = sqlite3.connect(dst)
            try:
                con.backup(out)
            finally:
                out.close()

        try:
            backup(path, snapshot)
            traj, cid = con.execute("select trajectory_id, cascade_id from trajectory_meta limit 1").fetchone()
            nxt = (con.execute("select max(idx) from steps").fetchone()[0] or 0) + 1
            con.executemany("insert into steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) "
                            "values (?,?,3,0,?,?,0)", self._steps(ev, cid, traj, nxt))
            con.commit()
        finally:
            con.close()

    def resume_cmd(self, sid, flags=()):
        return [self.exe] + list(flags) + ["--conversation", sid]


class OpenCode(Adapter):
    """OpenCode keeps every conversation in one SQLite database. xresume only reads that
    database; conversations go in through OpenCode's own `session import` command."""
    name = "opencode"
    label = "OpenCode"
    exe = "opencode"
    free_flags = ["--auto"]
    # `session import` creates a session and refuses an id that exists, so turns cannot be
    # added to one; a conversation moved back here becomes a new session.
    can_append = False

    def __init__(self):
        data = os.environ.get("XDG_DATA_HOME")
        self.db = (Path(data) if data else HOME / ".local" / "share") / "opencode" / "opencode.db"

    def _open(self):
        import sqlite3
        return sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=5)

    def exists(self, path, sid):
        try:
            con = self._open()
            try:
                return con.execute("select 1 from session_v2 where id = ?", (sid,)).fetchone() is not None
            finally:
                con.close()
        except Exception:
            return False

    def sessions(self):
        if not self.db.is_file():
            return []
        con = self._open()
        try:
            rows = con.execute("select id, directory, title, time_updated from session_v2 "
                               "where parent_id is null and time_archived is null").fetchall()
        finally:
            con.close()
        out = []
        for sid, directory, title, updated in rows:
            if directory:
                out.append(Session(self.name, sid, self.db, os.path.normpath(directory),
                                   one_line("" if (title or "").startswith("[xresume]") else title, 70), (updated or 0) / 1000.0))
        return out

    def read(self, path, tail=0, sid=None):
        """The conversation as events. With `tail`, only its last 80 messages."""
        con = self._open()
        try:
            rows = con.execute("select type, data from (select seq, type, data from session_message where session_id = ? "
                               "order by seq desc limit ?) order by seq", (sid, 80 if tail else -1)).fetchall()
        finally:
            con.close()
        ev = []
        for kind, data in rows:
            m = loads(data)
            ms = get(get(m, "time"), "created")
            ts = datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if isinstance(ms, (int, float)) else ""
            if kind == "user":
                text = str(get(m, "text") or "")
                if text.strip():
                    ev.append({"k": "user", "text": text, "ts": ts})
            elif kind == "assistant":
                for part in get(m, "content") or []:
                    if get(part, "type") == "text" and str(part.get("text") or "").strip():
                        ev.append({"k": "assistant", "text": str(part["text"]), "ts": ts})
                    elif get(part, "type") == "tool":
                        state = get(part, "state") or {}
                        inp = state.get("input")
                        ev.append({"k": "call", "id": str(part.get("id") or ""), "name": str(part.get("name") or ""),
                                   "input": inp if isinstance(inp, dict) else {"input": inp}, "ts": ts})
                        ev.append({"k": "result", "id": str(part.get("id") or ""),
                                   "text": flatten(state.get("content")) or str(state.get("error") or ""), "ts": ts})
        return ev

    def write(self, ev, cwd, title, src_label):
        """Create a new session holding these events, through `opencode session import`."""
        import hashlib
        import tempfile
        exe = shutil.which(self.exe)
        if not exe:
            raise RuntimeError("'%s' is not on PATH." % self.exe)
        rnd = lambda: uuid.uuid4().hex[:14]
        base = int(time.time() * 1000)
        stamp = lambda e, i: int(to_dt(e["ts"]).timestamp() * 1000) if e.get("ts") else base + i
        first = stamp(ev[0], 0) if ev else base
        model = {"id": "imported", "providerID": "xresume", "variant": "default"}
        msgs = [{"type": "user", "time": {"created": first}, "text": note(src_label), "files": []}]
        for i, e in enumerate(ev):
            t = stamp(e, i)
            if e["k"] == "user":
                msgs.append({"type": "user", "time": {"created": t}, "text": e["text"], "files": []})
                continue
            if e["k"] == "assistant" or msgs[-1]["type"] != "assistant":
                # A reply and the tool calls that follow it are one assistant message.
                msgs.append({"type": "assistant", "time": {"created": t, "completed": t}, "agent": "build", "model": model,
                             "content": [], "finish": "stop"})
            cur = msgs[-1]
            if e["k"] == "assistant":
                cur["content"].append({"type": "text", "text": e["text"]})
            elif e["k"] == "call":
                inp = e["input"] if isinstance(e["input"], dict) else {"input": e["input"]}
                cur["content"].append({"type": "tool", "id": e["id"], "name": safe_name(e["name"]), "executed": False,
                                       "state": {"status": "completed", "input": inp,
                                                 "content": [{"type": "text", "text": e["output"]}], "metadata": {"truncated": False}},
                                       "time": {"created": t, "ran": t, "completed": t}})
                cur["finish"] = "tool-calls"
        last = max([first] + [m["time"]["created"] for m in msgs])
        msgs.append({"type": "idle", "time": {"created": last}, "outcome": "succeeded"})
        for i, m in enumerate(msgs):        # ids sort in time order, as OpenCode's own do
            m["id"] = "msg_%012x%s" % ((base << 12 | i) & 0xFFFFFFFFFFFF, rnd())
        sid = "ses_%012x%s" % ((base << 12) & 0xFFFFFFFFFFFF ^ 0xFFFFFFFFFFFF, rnd())
        doc = {"info": {"id": sid, "projectID": hashlib.sha1(cwd.encode("utf-8", "replace")).hexdigest(), "cost": 0,
                        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        "time": {"created": first, "updated": last}, "title": title or "Imported conversation",
                        "location": {"directory": cwd}},
               "messages": msgs}
        fd, tmp = tempfile.mkstemp(prefix="xresume-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            r = subprocess.run([exe, "session", "import", "--directory", cwd, tmp], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120, cwd=cwd, stdin=subprocess.DEVNULL, env=child_env())
        finally:
            os.remove(tmp)
        if r.returncode != 0 or sid not in (r.stdout or ""):
            why = ((r.stderr or r.stdout or "").strip().splitlines() or ["no message"])[0]
            raise RuntimeError("OpenCode did not accept the conversation: %s" % one_line(why, 120))
        return sid, self.db

    def resume_cmd(self, sid, flags=()):
        return [self.exe] + list(flags) + ["--session", sid]


def quote_all(text):
    from urllib.parse import quote
    return quote(text, safe="")


def grok_group(cwd):
    """The folder Grok keeps a working folder's sessions in: the folder's path URL-encoded, or,
    when that is over 255 bytes, a short name from the folder's own name and a hash of the path
    (Grok then records the path in a .cwd file there)."""
    enc = quote_all(cwd)
    if len(enc.encode("utf-8")) <= 255:
        return enc
    base = os.path.basename(cwd.rstrip("\\/")) or cwd
    slug = re.sub(r"[^a-z0-9]+", "-", "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in base)).strip("-")[:40]
    return "%s-%s" % (slug or "workspace", blake3_hex(cwd.encode("utf-8"))[:16])


class Grok(Adapter):
    """Grok CLI. Each conversation is a folder, ~/.grok/sessions/<encoded folder>/<id>/, holding
    summary.json (title, times), chat_history.jsonl (what the model is sent) and updates.jsonl
    (what the screen is redrawn from on resume)."""
    name = "grok"
    label = "Grok"
    exe = "grok"
    free_flags = ["--always-approve"]

    def __init__(self):
        self.root = long_path(Path(os.environ.get("GROK_HOME") or HOME / ".grok") / "sessions")

    def exists(self, path, sid):
        return os.path.isfile(os.path.join(str(path), "chat_history.jsonl"))

    def _children(self):
        """Ids of subagent sessions. A parent session keeps a subagents/ folder that names them;
        they are parts of the parent's work, not conversations of their own."""
        kids = set()
        for meta in self.root.glob("*/*/subagents/**/*"):
            parent = meta.relative_to(self.root).parts[1]
            found = set(re.findall(UUID_RE, meta.name))
            if meta.is_file() and meta.suffix == ".json":
                try:
                    found |= set(re.findall(UUID_RE, meta.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass
            kids |= found - {parent}
        return kids

    def sessions(self):
        out = []
        if not self.root.is_dir():
            return out
        kids = self._children()
        for f in self.root.glob("*/*/summary.json"):
            if not re.fullmatch(UUID_RE, f.parent.name) or f.parent.name in kids:
                continue
            try:
                info = loads(f.read_text(encoding="utf-8")) or {}
                chat = f.parent / "chat_history.jsonl"
                mtime = max(chat.stat().st_mtime, f.stat().st_mtime)
            except OSError:
                continue
            cwd = get(get(info, "info"), "cwd")
            title = info.get("generated_title") or info.get("session_summary") or ""
            if cwd and not str(title).startswith("[xresume]"):
                out.append(Session(self.name, f.parent.name, f.parent, cwd, one_line(title, 70), mtime))
        return out

    def read(self, path, tail=0, sid=None):
        """The conversation as events. With `tail`, only what the last `tail` bytes hold."""
        f = os.path.join(str(path), "chat_history.jsonl")
        ts = datetime.fromtimestamp(os.path.getmtime(f), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ev = []
        for line in (tail_lines(f, tail) if tail else read_lines(f)):
            o = loads(line)
            kind = get(o, "type")
            if kind == "user":
                # Only prompts carry prompt_index; the rest is context Grok adds (workspace
                # details, reminders).
                if "prompt_index" not in o or o.get("synthetic_reason"):
                    continue
                text = flatten(o.get("content"))
                m = re.search(r"<user_query>\n?(.*?)\n?</user_query>", text, re.S)
                text = m.group(1) if m else text
                if typed(text) or text.startswith("[xresume]"):
                    ev.append({"k": "user", "text": text, "ts": ts})
            elif kind == "assistant":
                text = flatten(o.get("content"))
                if text.strip():
                    ev.append({"k": "assistant", "text": text, "ts": ts})
                for c in o.get("tool_calls") or []:
                    args = loads(str(get(c, "arguments") or ""))
                    ev.append({"k": "call", "id": str(get(c, "id") or ""), "name": str(get(c, "name") or ""),
                               "input": args if isinstance(args, dict) else {"input": str(get(c, "arguments") or "")}, "ts": ts})
            elif kind == "tool_result":
                ev.append({"k": "result", "id": str(o.get("tool_call_id") or ""), "text": flatten(o.get("content")), "ts": ts})
        return ev

    @staticmethod
    def _records(ev, sid, first_prompt):
        """Turn events into (chat_history lines, updates lines)."""
        chat, upd, n = [], [], first_prompt

        def update(u, ts):
            upd.append(dumps({"timestamp": int(to_dt(ts).timestamp()), "method": "session/update",
                              "params": {"sessionId": sid, "update": u}}))

        for e in ev:
            ts = e.get("ts") or iso_now()
            if e["k"] == "user":
                chat.append(dumps({"type": "user", "content": [{"type": "text", "text": e["text"]}], "prompt_index": n}))
                update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": e["text"]},
                        "_meta": {"promptIndex": n}}, ts)
                n += 1
            elif e["k"] == "assistant":
                chat.append(dumps({"type": "assistant", "content": e["text"]}))
                update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": e["text"]}}, ts)
            elif e["k"] == "call":
                inp = e["input"] if isinstance(e["input"], dict) else {"input": e["input"]}
                cid, name = re.sub(r"[^a-zA-Z0-9_-]", "_", e["id"]), safe_name(e["name"])
                chat.append(dumps({"type": "assistant", "content": "", "tool_calls": [{"id": cid, "name": name, "arguments": dumps(inp)}]}))
                chat.append(dumps({"type": "tool_result", "tool_call_id": cid, "content": e["output"]}))
                update({"sessionUpdate": "tool_call", "toolCallId": cid, "title": name, "rawInput": inp}, ts)
                update({"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "completed",
                        "content": [{"type": "content", "content": {"type": "text", "text": e["output"]}}]}, ts)
        return chat, upd

    @staticmethod
    def _stamp():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _model(self):
        """The model of the user's most recent Grok session; a session file must name one."""
        newest, model = 0.0, "grok-4.7"
        for f in self.root.glob("*/*/summary.json"):
            try:
                m = (loads(f.read_text(encoding="utf-8")) or {}).get("current_model_id")
                t = f.stat().st_mtime
            except (OSError, AttributeError):
                continue
            if isinstance(m, str) and m and t > newest:
                newest, model = t, m
        return model

    def write(self, ev, cwd, title, src_label):
        """Create a new session folder holding these events."""
        group = self.root / grok_group(cwd)
        if not group.is_dir():
            group.mkdir(parents=True)
            if group.name != quote_all(cwd):
                (group / ".cwd").write_bytes(cwd.encode("utf-8"))
        sid = uuid7()
        d = group / sid
        d.mkdir()
        first = {"k": "user", "text": note(src_label), "ts": (ev[0].get("ts") if ev else "") or iso_now()}
        chat, upd = self._records([first] + ev, sid, 0)
        now = self._stamp()
        summary = {"info": {"id": sid, "cwd": cwd}, "session_summary": title, "created_at": now, "updated_at": now,
                   "num_messages": len(upd), "num_chat_messages": len(chat), "current_model_id": self._model(),
                   "next_trace_turn": 0, "chat_format_version": 1, "session_kind": "interactive", "last_active_at": now,
                   "generated_title": title}
        write_file(d / "chat_history.jsonl", "\n".join(chat) + "\n")
        write_file(d / "updates.jsonl", "\n".join(upd) + "\n")
        write_file(d / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))
        return sid, d

    def append(self, path, ev):
        """Add events to the end of an existing session, after backing up its files."""
        d = Path(path)
        sid = d.name
        prompts = [get(loads(l), "prompt_index") for l in read_lines(d / "chat_history.jsonl")]
        nxt = max([n for n in prompts if isinstance(n, int)] + [-1]) + 1
        chat, upd = self._records(ev, sid, nxt)
        if not chat:
            return
        for f in ("chat_history.jsonl", "updates.jsonl", "summary.json"):
            if (d / f).is_file():
                backup(d / f, name="%s-%s" % (sid, Path(f).stem))
        append_text(str(d / "chat_history.jsonl"), chat)
        append_text(str(d / "updates.jsonl"), upd)
        try:
            summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            now = self._stamp()
            summary["updated_at"] = summary["last_active_at"] = now
            summary["num_chat_messages"] = int(summary.get("num_chat_messages") or 0) + len(chat)
            summary["num_messages"] = int(summary.get("num_messages") or 0) + len(upd)
            write_file(d / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))
        except (OSError, ValueError, TypeError):
            pass                                         # the history itself is in place; the counts are only for listings

    def resume_cmd(self, sid, flags=()):
        return [self.exe] + list(flags) + ["--resume", sid]


ADAPTERS = {a.name: a for a in (Claude(), Codex(), Agy(), OpenCode(), Grok())}
_installed = {}


def installed(adapter):
    if adapter.name not in _installed:
        _installed[adapter.name] = bool(shutil.which(adapter.exe))
    return _installed[adapter.name]


# ---------- links: which sessions in different tools are the same conversation ----------
# ~/.xresume/links.json maps "tool:session-id" to {"thread": id shared by every copy of one
# conversation, "path": the session file}. What each copy is missing is worked out by comparing
# the conversations themselves, so nothing else needs recording and any number of copies works.

def load_links():
    try:
        with open(STATE_DIR / "links.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and "thread" in v and "path" in v} if isinstance(data, dict) else {}


def save_links(links):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "links.json.tmp"
    tmp.write_text(json.dumps(links, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(STATE_DIR / "links.json"))


def link_key(tool, sid):
    return "%s:%s" % (tool, sid)


def fingerprint(e):
    """Identity of an event that survives a trip through any tool's format: the wording of a
    message, or the id of a tool call."""
    if e["k"] == "call":
        return "call:" + re.sub(r"[^a-zA-Z0-9_-]", "_", e.get("id") or "")
    return e["k"] + ":" + hashlib.sha1(" ".join((e.get("text") or "").split()).encode("utf-8", "replace")).hexdigest()[:20]


def own_events(ev):
    """Events without the notes xresume itself adds when it imports a conversation."""
    return [e for e in ev if not (e["k"] == "user" and e["text"].startswith("[xresume]"))]


def missing_from(ev, their_ev):
    """Compare two copies of a conversation. Returns the events the other copy lacks (possibly
    none) when it is simply behind or level, or None when the two have gone separate ways."""
    mine, theirs = own_events(ev), own_events(their_ev)
    a, b = [fingerprint(e) for e in mine], [fingerprint(e) for e in theirs]
    if a[:len(b)] == b:
        return mine[len(b):]
    if b[:len(a)] == a:
        return []                                        # the other copy is already ahead
    return None


def all_sessions():
    """One row per conversation, newest first. When a conversation has copies in several tools,
    only the most recently used copy is listed."""
    links = load_links()
    best = {}
    for a in ADAPTERS.values():
        try:
            found = a.sessions()
        except Exception:
            found = []                                   # one tool's broken store must not hide the others
        for s in found:
            if not s.title:
                continue
            link = links.get(link_key(s.tool, s.id))
            if link and link.get("old"):
                continue                                 # replaced by a newer copy in the same CLI
            key = link["thread"] if link else link_key(s.tool, s.id)
            if key not in best or s.mtime > best[key].mtime:
                best[key] = s
    return sorted(best.values(), key=lambda s: s.mtime, reverse=True)


# ---------- colour and the picker ----------

# Each tool's brand colour: (24-bit RGB, 256-colour fallback).
BRAND = {
    "claude": ((217, 119, 87), 173),   # Claude orange
    "codex": ((16, 163, 127), 36),     # OpenAI green
    "agy": ((66, 133, 244), 33),       # Google blue
    "opencode": ((250, 178, 131), 216),  # OpenCode's own accent, a light peach
    "grok": ((176, 176, 188), 250),    # xAI's monochrome, as a light grey that shows on dark and light
}
COLOR = False
TRUECOLOR = False


def init_terminal():
    """Turn on ANSI escape handling (needed on older Windows consoles) and decide how much colour to use."""
    global COLOR, TRUECOLOR
    if not sys.stdout.isatty():
        return
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)) or not k.SetConsoleMode(h, mode.value | 0x0004):
                return
        except Exception:
            return
    COLOR = not os.environ.get("NO_COLOR")
    TRUECOLOR = os.name == "nt" or os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def sgr(code, text):
    return "\x1b[%sm%s\x1b[0m" % (code, text) if COLOR else text


def paint(tool, text, bold=False):
    rgb, idx = BRAND.get(tool, ((170, 170, 170), 248))
    code = "38;2;%d;%d;%d" % rgb if TRUECOLOR else "38;5;%d" % idx
    return sgr(("1;" if bold else "") + code, text)


def ago(mtime):
    s = max(0, time.time() - mtime)
    if s < 90:
        return "just now"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    if s < 86400 * 30:
        return "%dd ago" % (s // 86400)
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def folder(s):
    return os.path.basename(s.cwd.rstrip("\\/")) or s.cwd


# Characters of "ambiguous" width (box lines, arrows, the ellipsis, Greek, Cyrillic) take two
# columns in many East Asian terminals. When this terminal is one of them, the lines and arrows
# xresume draws become plain ASCII and other such characters count as two columns.
AMBIGUOUS_WIDE = None
NARROW = {ord(a): b for a, b in zip("─│┌┐└┘→↑↓…", "-|++++>^v~")}


def char_width(ch):
    """Terminal columns a character takes: wide characters (CJK, emoji) take two, and marks that
    sit on the character before them (accents, Thai and Indic vowel signs) take none."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me") or "\u1160" <= ch <= "\u11ff":
        return 0
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in "WF" or (eaw == "A" and AMBIGUOUS_WIDE and ord(ch) not in NARROW) else 1


def measure_ambiguous(con):
    """Does this terminal draw ambiguous-width characters two columns wide? Draws one and asks
    where the cursor ended up. Anything unexpected counts as no."""
    out = sys.stdout
    try:
        out.write("\r\u2500")
        if con:
            import ctypes
            from ctypes import wintypes as wt

            class INFO(ctypes.Structure):
                _fields_ = [("size", wt._COORD), ("cursor", wt._COORD), ("attr", wt.WORD),
                            ("window", wt.SMALL_RECT), ("max", wt._COORD)]
            out.flush()
            info = INFO()
            k = ctypes.windll.kernel32
            k.GetStdHandle.restype = wt.HANDLE
            return bool(k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11), ctypes.byref(info))) and info.cursor.X == 2
        if os.name == "nt":
            return False
        import select
        fd = sys.stdin.fileno()
        out.write("\x1b[6n")                            # the terminal answers ESC [ row ; column R
        out.flush()
        reply, end = b"", time.time() + 0.5
        while not reply.endswith(b"R") and len(reply) < 32:
            wait = end - time.time()
            if wait <= 0 or not select.select([fd], [], [], wait)[0]:
                break
            reply += os.read(fd, 1)
        m = re.search(rb"\x1b\[\d+;(\d+)R", reply)
        return bool(m) and int(m.group(1)) == 3
    except Exception:
        return False
    finally:
        try:
            out.write("\r   \r")
            out.flush()
        except Exception:
            pass


def fit(s, cols, pad=False):
    """One line of text cut to `cols` terminal columns. Control characters are dropped so a
    title cannot disturb the screen."""
    chars = [(ch, char_width(ch)) for ch in " ".join(str(s or "").split()) if ch.isprintable()]
    used = sum(w for _, w in chars)
    if used > cols:
        used, keep = 1, []
        for ch, w in chars:
            if used + w > cols:
                break
            keep.append((ch, w))
            used += w
        chars = keep + [("…", 0)]
    return "".join(ch for ch, _ in chars) + (" " * (cols - used) if pad else "")


def wrap(text, cols, limit):
    """Wrap text to `cols` columns. At most `limit` lines; a cut text ends with a line of '…'."""
    out = []
    for para in str(text).replace("\t", "    ").splitlines():
        para = "".join(ch for ch in para if ch.isprintable()).rstrip()
        if not para:
            if out and out[-1]:
                out.append("")                           # runs of blank lines become one
            continue
        line, used = "", 0
        for word in re.findall(r"\s*\S+", para):
            w = sum(char_width(c) for c in word)
            if used and used + w > cols:
                out.append(line)
                line, used, word = "", 0, word.lstrip()
                w = sum(char_width(c) for c in word)
            while w > cols:                              # a word longer than the line: split it
                take, tw = "", 0
                for c in word:
                    if tw + char_width(c) > cols:
                        break
                    take, tw = take + c, tw + char_width(c)
                out.append(take)
                word, w = word[len(take):], w - tw
            line, used = line + word, used + w
        out.append(line)
        if len(out) > limit:
            break
    while out and not out[-1]:
        out.pop()
    return out if len(out) <= limit else out[:limit - 1] + ["…"]


PREVIEW_MIN_WIDTH = 90   # the preview panel needs a window at least this wide


def panel_width(width):
    """Columns the preview panel takes in a window this wide, or 0 if it does not fit."""
    return max(34, min(72, width * 45 // 100)) if width >= PREVIEW_MIN_WIDTH else 0


def load_preview(s, cache):
    """The end of a conversation, for the preview panel. Reads only the end of the session so
    moving through the list stays quick; reads further back if that held too few messages."""
    key = (s.tool, s.id)
    if key not in cache:
        ev = []
        for tail in (400000, 6000000):
            try:
                ev = own_events(ADAPTERS[s.tool].read(s.path, tail, sid=s.id))
            except Exception:
                ev = []
            if sum(1 for e in ev if e["k"] in ("user", "assistant")) >= 4:
                break
        cache[key] = ev
    return cache[key]


def preview_lines(s, events, cols, height):
    """The preview panel's lines: the title, then as many of the last messages as fit, oldest
    first. Long messages are cut short, and tool calls are only counted."""
    head = [paint(s.tool, fit(s.title, cols), True), ""]
    room = height - len(head)
    per = max(4, room // 2)                              # lines one message may take
    messages, calls = [], 0                              # (event, tool calls made just before it)
    for e in events:
        if e["k"] == "call":
            calls += 1
        elif e["k"] in ("user", "assistant"):
            messages.append((e, calls))
            calls = 0
    blocks, used = [], 0
    for e, calls in reversed(messages):
        who = sgr("1", "You") if e["k"] == "user" else paint(s.tool, s.tool, True)
        if calls and e["k"] == "assistant":
            who += sgr("2", "  after %d tool call%s" % (calls, "" if calls == 1 else "s"))
        block = [who] + wrap(e["text"], cols, per) + [""]
        if used + len(block) > room:
            if not blocks:
                blocks.append(block[:max(0, room - 1)] + ["…"])
            break
        blocks.insert(0, block)
        used += len(block)
    if not blocks:
        return head + [sgr("2", "(no messages to show)")]
    return (head + [line for b in blocks for line in b])[:height]


class WinConsole:
    """Windows: read the console's own key and mouse events.

    The C runtime's getwch() cannot be used for this: it reports arrows as two-character codes
    and resets the console mode on every read, which drops mouse input."""

    VK = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup", 0x22: "pgdn",
          0x24: "home", 0x23: "end", 0x0D: "enter", 0x1B: "esc", 0x09: "tab", 0x08: "backspace"}

    @staticmethod
    def open(restore=None):
        try:
            return WinConsole(restore)
        except Exception:
            return None

    def __init__(self, restore=None):
        """`restore` is the console mode to put back on close; by default the mode found now."""
        import ctypes
        from ctypes import wintypes as wt

        class COORD(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short)]

        class KEY(ctypes.Structure):
            _fields_ = [("down", wt.BOOL), ("repeat", wt.WORD), ("vk", wt.WORD), ("scan", wt.WORD),
                        ("char", wt.WCHAR), ("ctrl", wt.DWORD)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("pos", COORD), ("buttons", wt.DWORD), ("ctrl", wt.DWORD), ("flags", wt.DWORD)]

        class EVENT(ctypes.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("size", COORD)]

        class RECORD(ctypes.Structure):
            _fields_ = [("type", wt.WORD), ("event", EVENT)]

        self.RECORD, self.ctypes = RECORD, ctypes
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wt.HANDLE
        k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        k.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]
        k.ReadConsoleInputW.argtypes = [wt.HANDLE, ctypes.POINTER(RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD)]
        k.WriteConsoleInputW.argtypes = k.ReadConsoleInputW.argtypes
        k.CloseHandle.argtypes = [wt.HANDLE]
        # GENERIC_READ | GENERIC_WRITE, shared, OPEN_EXISTING
        self.h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if self.h in (None, wt.HANDLE(-1).value):
            raise OSError("no console")
        old = wt.DWORD()
        if not k.GetConsoleMode(self.h, ctypes.byref(old)):
            raise OSError("not a console")
        self.old = old.value if restore is None else restore
        k.FlushConsoleInputBuffer.argtypes = [wt.HANDLE]
        k.FlushConsoleInputBuffer(self.h)                # drop anything typed at the CLI that was just quit
        # + mouse, window-resize and extended flags; - quick edit (it swallows clicks), line input,
        # echo, escape-sequence input, and processed input (so Ctrl+C arrives as a key)
        if not k.SetConsoleMode(self.h, (self.old | 0x0010 | 0x0008 | 0x0080) & ~(0x0040 | 0x0001 | 0x0002 | 0x0004 | 0x0200)):
            raise OSError("cannot set console mode")
        self.buttons = 0

    def close(self):
        self.k.SetConsoleMode(self.h, self.old)
        self.k.CloseHandle(self.h)

    def read(self):
        """Block for one event; same return values as read_key(), plus 'resize'."""
        rec, n = self.RECORD(), self.ctypes.c_ulong()
        while True:
            if not self.k.ReadConsoleInputW(self.h, self.ctypes.byref(rec), 1, self.ctypes.byref(n)) or not n.value:
                return "esc"
            if rec.type == 1:                               # key
                e = rec.event.key
                if not e.down:
                    continue
                if e.vk in self.VK:
                    return self.VK[e.vk]
                if e.char and e.char != "\x00":
                    return KEYS.get(e.char, e.char)
            elif rec.type == 2:                             # mouse
                e = rec.event.mouse
                x, y = e.pos.x + 1, e.pos.y + 1
                if e.flags & 4:                             # wheel: high word is the signed distance
                    return ("mouse", 64 if (e.buttons >> 16) < 0x8000 else 65, x, y, True)
                if e.flags & 1:                             # moved
                    return ("mouse", 35, x, y, True)
                was, self.buttons = self.buttons, e.buttons & 3
                for bit, code in ((1, 0), (2, 2)):          # left, right
                    if self.buttons & bit and not was & bit:
                        return ("mouse", code, x, y, True)
            elif rec.type == 4:                             # window resized
                return "resize"


def char_source():
    """Return (getc, more): read one character, and 'is more input already waiting?'."""
    if os.name == "nt":
        import msvcrt

        def more():
            end = time.time() + 0.03
            while time.time() < end:
                if msvcrt.kbhit():
                    return True
                time.sleep(0.002)
            return False
        return msvcrt.getwch, more
    import codecs
    import select as sel
    fd = sys.stdin.fileno()
    dec = codecs.getincrementaldecoder("utf-8")("replace")

    def getc():
        while True:
            ch = dec.decode(os.read(fd, 1))
            if ch:
                return ch
    return getc, lambda: bool(sel.select([fd], [], [], 0.03)[0])


KEYS = {"\r": "enter", "\n": "enter", "\t": "tab", "\x08": "backspace", "\x7f": "backspace",
        "\x03": "ctrl-c", "\x15": "clear", "\x10": "preview"}
CSI = {"A": "up", "B": "down", "C": "right", "D": "left", "5~": "pgup", "6~": "pgdn", "H": "home", "1~": "home", "F": "end", "4~": "end"}


def read_key(getc, more):
    """Block for one event. Returns a key name ('up', 'enter', ...), a typed character,
    or ('mouse', button_bits, column, row, pressed)."""
    c = getc()
    if c in ("\x00", "\xe0") and os.name == "nt":   # getwch() two-character codes for arrows etc.
        return {"H": "up", "P": "down", "M": "right", "K": "left", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(getc(), "")
    if c != "\x1b":
        return KEYS.get(c, c)
    if not more():
        return "esc"
    c = getc()
    if c == "O":
        return CSI.get(getc(), "")
    if c != "[":
        return ""
    seq = ""
    while not seq or not "@" <= seq[-1] <= "~":
        seq += getc()
    if seq[0] == "<" and seq[-1] in "Mm":
        try:
            b, x, y = (int(v) for v in seq[1:-1].split(";"))
        except ValueError:
            return ""
        return ("mouse", b, x, y, seq[-1] == "M")
    return CSI.get(seq, "")


def targets_for(s):
    """Where a session can be opened: the tool it came from first, then the other installed tools."""
    return [ADAPTERS[s.tool]] + [a for a in ADAPTERS.values() if a.name != s.tool and installed(a)]


def fold(text):
    """Text for comparing in search: same case (ß matches ss) and same accent encoding (a folder
    name macOS stored as e + accent matches a typed é)."""
    return unicodedata.normalize("NFC", text).casefold()


def matches(sessions, query):
    words = fold(query).split()
    return [s for s in sessions if all(w in fold("%s %s %s %s" % (s.tool, s.title, s.cwd, s.id)) for w in words)]


HEADER = 3   # screen lines above the first row
DOUBLE_CLICK = 0.5   # seconds between two clicks on the same row for them to count as a double-click


def menu_layout(opts, own, at_row, width, height):
    """Where the "open in" menu goes and what it lists: (left column, top row, inner width,
    [(adapter, text)]). It opens just below the selected row, or above it near the bottom."""
    items = [(a, a.label.replace("the ", "") + ("  (current)" if a.name == own else "")) for a in opts]
    inner = max([len(t) for _, t in items] + [8]) + 5
    tall = len(items) + 2
    top = at_row + 1 if at_row + tall <= height - 2 else max(1, at_row - tall)
    return max(1, min(14, width - inner - 2)), top, inner, items


def render(rows, sel, top, query, opts, width, height, hover=None, msg="", mouse=False, free=False,
           preview=None, searching=False, menu=None):
    """Return the whole picker screen as one string. `opts` are the CLIs the selected row can
    open in, its own first. `preview` is the selected conversation's last events when the
    preview panel is open, else None. `searching` is true while the search box is taking what
    is typed. `menu` is the highlighted entry of the "open in" menu when it is open, else None."""
    body = max(1, height - 5)
    pw = panel_width(width) if preview is not None else 0
    left = width - pw - 3 if pw else width               # columns the list itself gets
    out = [sgr("1", " xresume") + sgr("2", "  " + one_line("resume a conversation, in its own CLI or another", max(1, width - 12))),
           (" " + sgr("2", "Search: ") + fit(query, max(1, width - 11)) + sgr("2", "_")) if searching
           else sgr("2", " Press / to search"), ""]
    room = max(4, left - 44) if pw else max(10, width - 47)   # columns for the title
    if not rows:
        out.append(sgr("2", "   no matching conversations"))
    for i in range(top, min(len(rows), top + body)):
        s = rows[i]
        on = i == sel
        hov = i == hover and not on
        title = fit(s.title or "(untitled)", room)
        out.append("%s %s %s %s %s" % (
            paint(s.tool, " ❯" if on else " ›" if hov else "  ", on),
            paint(s.tool, "%-8s" % s.tool, True),
            sgr("2", "%-10s" % ago(s.mtime)),
            sgr("2", fit(folder(s), 18, pad=True)),
            sgr("1", title) if on else sgr("4", title) if hov else title,
        ))
    out += [""] * (HEADER + body - len(out))
    out.append("")

    # Footer: what Enter does, then as many hints as fit on the line.
    menu = menu if rows and opts else None
    chosen = opts[menu] if menu is not None else (opts[0] if rows and opts else None)
    plain = action = ""
    if msg:
        plain, action = " " + msg, sgr("1", " " + msg)
    elif chosen:
        s = rows[sel]
        name = chosen.label.replace("the ", "")
        if chosen.name == s.tool:
            plain, action = " Enter resumes it in " + name, " Enter resumes it in " + paint(chosen.name, name, True)
        else:
            plain = " Enter converts %s → %s" % (s.tool, name)
            action = " Enter converts %s → %s" % (paint(s.tool, s.tool, True), paint(chosen.name, name, True))
        if free and chosen.free_flags:
            extra = " with " + " ".join(chosen.free_flags)
            plain, action = plain + extra, action + sgr("1;31", extra)
    if menu is not None:
        hints = ["↑↓ choose", "Tab normal mode" if free else "Tab skip permissions", "Esc back"]
    else:
        hints = ["%d/%d" % (sel + 1 if rows else 0, len(rows)), "↑↓ move", "Esc cancel search" if searching else "/ search"]
        if len(opts) > 1:
            hints.append("→ open in another CLI")
        hints.append("Tab normal mode" if free else "Tab skip permissions")
        hints.append(("Ctrl+P" if searching else "Space") + (" hide preview" if pw else " preview"))
        if mouse:
            hints.append("double-click open")
        if not searching:
            hints.append("Esc exit")
    hint = ""
    for h in hints:
        if len(plain) + len(hint) + len(h) + 3 >= width:
            break
        hint += "   " + h
    out.append(action + sgr("2", hint))
    frame = "\x1b[H" + "".join(l + "\x1b[K\r\n" for l in out[:-1]) + out[-1] + "\x1b[K\x1b[J"
    if pw:
        # The panel is drawn over the right-hand side, one positioned line at a time.
        lines = preview_lines(rows[sel], preview, pw, body) if rows else []
        for i in range(body):
            frame += "\x1b[%d;%dH\x1b[K%s %s" % (HEADER + 1 + i, left + 2, sgr("2", "│"), lines[i] if i < len(lines) else "")
    if menu is not None:
        x, y, inner, items = menu_layout(opts, rows[sel].tool, HEADER + 1 + sel - top, width, height)
        frame += "\x1b[%d;%dH%s" % (y, x, sgr("2", "┌─ Open in " + "─" * (inner - 10) + "┐"))
        for i, (a, text) in enumerate(items):
            cell = ((" ❯ " if i == menu else "   ") + text).ljust(inner)
            frame += "\x1b[%d;%dH%s%s%s" % (y + 1 + i, x, sgr("2", "│"), paint(a.name, cell, i == menu), sgr("2", "│"))
        frame += "\x1b[%d;%dH%s" % (y + 1 + len(items), x, sgr("2", "└" + "─" * inner + "┘"))
    return frame


def picker(sessions, ui=None):
    """Full-screen picker with keyboard and mouse. Returns (session, target adapter, extra flags) or None.
    `ui` carries what should survive between showings: {"preview": panel open?, "msg": a line to show once}."""
    ui = {} if ui is None else ui
    old_tty = con = None
    # The terminal is put back the way it was when xresume first started, not the way the last
    # CLI happened to leave it.
    if os.name == "nt":
        con = WinConsole.open(ui.get("console"))
        if con:
            ui["console"] = con.old
    else:
        import termios
        import tty
        old_tty = ui.setdefault("tty", termios.tcgetattr(sys.stdin.fileno()))
        tty.setcbreak(sys.stdin.fileno())
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)   # drop anything typed at the CLI that was just quit
        raw = termios.tcgetattr(sys.stdin.fileno())
        raw[3] &= ~termios.ISIG          # deliver Ctrl+C as a key instead of an interrupt
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, raw)
    global AMBIGUOUS_WIDE
    if AMBIGUOUS_WIDE is None:
        AMBIGUOUS_WIDE = measure_ambiguous(con)
    getc, more = char_source()
    next_key = con.read if con else (lambda: read_key(getc, more))
    old_winch = wake = None
    if os.name != "nt":
        # A window resize arrives as a signal; pass it through a pipe so the wait for a key
        # also wakes for it, and redraw at the new size.
        import select
        import signal
        wake = os.pipe()
        os.set_blocking(wake[1], False)

        def on_resize(*_):
            try:
                os.write(wake[1], b"r")
            except OSError:
                pass
        old_winch = signal.signal(signal.SIGWINCH, on_resize)

        def next_key():
            if wake[0] in select.select([sys.stdin.fileno(), wake[0]], [], [])[0]:
                os.read(wake[0], 4096)
                return "resize"
            return read_key(getc, more)
    vt_mouse = os.name != "nt"           # terminals elsewhere report the mouse as escape sequences
    mouse = vt_mouse or con is not None
    sel = top = 0
    free = False                         # Tab: launch the CLI with its skip-permissions flags
    hover, msg, query = None, ui.pop("msg", ""), ""
    presses, last_press = 0, 0.0         # consecutive Ctrl+C presses; two in a row exits
    clicked, clicked_at = None, 0.0      # last row clicked, for double-click
    preview, previews = ui.get("preview", False), {}   # Space / Ctrl+P: the preview panel, and what it has loaded
    searching = False                    # "/" was pressed: typing goes to the search box
    menu = None                          # Right: the "open in" menu is open, with this entry highlighted
    w = sys.stdout.write
    # alternate screen, hide cursor, no line wrapping, then mouse reporting: any motion (1003) in
    # SGR coordinates (1006)
    w("\x1b[?1049h\x1b[?25l\x1b[?7l" + ("\x1b[?1000h\x1b[?1003h\x1b[?1006h" if vt_mouse else ""))
    try:
        while True:
            rows = matches(sessions, query)
            sel = max(0, min(sel, len(rows) - 1))
            size = shutil.get_terminal_size((100, 30))
            body = max(1, size.lines - 5)
            top = max(0, min(top, sel), sel - body + 1)
            opts = targets_for(rows[sel]) if rows else []
            if not rows or len(opts) < 2:
                menu = None
            pw = panel_width(size.columns) if preview else 0
            ends = (load_preview(rows[sel], previews) if rows else []) if pw else None
            frame = render(rows, sel, top, query, opts, size.columns, size.lines, hover, msg, mouse, free, ends, searching, menu)
            w(frame.translate(NARROW) if AMBIGUOUS_WIDE else frame)
            sys.stdout.flush()
            msg = ""
            try:
                key = next_key()
            except KeyboardInterrupt:
                key = "ctrl-c"

            if key == "ctrl-c":
                now = time.time()
                presses = presses + 1 if now - last_press < 1.0 else 1
                last_press = now
                if presses >= 2:
                    return None
                msg = "Press Ctrl+C again to exit."
                continue
            if not isinstance(key, tuple):                  # mouse movement does not break the pair
                presses = 0

            def go(target):
                return rows[sel], target, (target.free_flags if free else [])

            if menu is not None:
                # The menu takes every key and click until it is closed.
                if isinstance(key, tuple):
                    _, b, x, y, pressed = key
                    mx, my, inner, items = menu_layout(opts, rows[sel].tool, HEADER + 1 + sel - top, size.columns, size.lines)
                    over = y - my - 1 if mx <= x <= mx + inner + 1 and my < y <= my + len(items) else None
                    if b & 64:
                        pass
                    elif b & 32:
                        menu = over if over is not None else menu
                    elif pressed and b & 3 == 0:
                        if over is not None:
                            return go(opts[over])
                        menu = None                         # a click anywhere else closes it
                elif key in ("esc", "left"):
                    menu = None
                elif key == "up":
                    menu = (menu - 1) % len(opts)
                elif key == "down":
                    menu = (menu + 1) % len(opts)
                elif key == "enter":
                    return go(opts[menu])
                elif key == "tab":
                    free = not free
                continue

            if isinstance(key, tuple):
                _, b, _x, y, pressed = key
                at = top + y - 1 - HEADER
                at = at if y > HEADER and at < min(len(rows), top + body) else None
                if pw and _x > size.columns - pw - 2:
                    at = None                               # the pointer is over the preview panel
                if b & 64:                                  # wheel
                    sel += 1 if b & 1 else -1
                    hover = None
                elif b & 32:                                # pointer moved
                    hover = at
                elif pressed and at is not None and b & 3 == 0:
                    now = time.time()                       # left click selects; a double-click opens
                    double = clicked == rows[at].id and now - clicked_at < DOUBLE_CLICK
                    clicked, clicked_at = rows[at].id, now
                    if double and at == sel:                # the first click selected it, so this is the row shown
                        return go(opts[0])
                    sel = at
                continue

            if key == "esc" and not searching:
                return None
            if key == "esc":
                searching, query = False, ""
            elif key == "clear":
                query = ""
            elif key == "/" and not searching:
                searching = True
            elif key == "enter":
                if rows:
                    return go(opts[0])
            elif key == "right":
                if len(opts) > 1:
                    menu = 0                                # opens with the conversation's own CLI highlighted
                elif rows:
                    msg = "No other supported CLI is installed."
            elif key == "tab":
                free = not free
            elif key == "preview" or (key in (" ", "p") and not searching):
                preview = not preview
                if preview and not panel_width(size.columns):
                    msg = "Make the window wider to show the preview."
            elif key == "up":
                sel -= 1
            elif key == "down":
                sel += 1
            elif key == "pgup":
                sel -= body
            elif key == "pgdn":
                sel += body
            elif key == "home":
                sel = 0
            elif key == "end":
                sel = len(rows) - 1
            elif key == "backspace":
                if searching and not query:
                    searching = False                       # backspace on an empty box leaves the search
                query = query[:-1]
            elif searching and len(key) == 1 and key.isprintable():
                query += key
                sel, hover = 0, None
    except KeyboardInterrupt:
        return None
    finally:
        w(("\x1b[?1006l\x1b[?1003l\x1b[?1000l" if vt_mouse else "") + "\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        ui["preview"] = preview
        if old_tty is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        if wake:
            import signal
            signal.signal(signal.SIGWINCH, old_winch)
            os.close(wake[0])
            os.close(wake[1])
        if con is not None:
            con.close()


MAX_OUTPUT = 6000   # characters kept from each tool output when a conversation is moved


def launch(cmd, cwd):
    """Run a CLI in the conversation's folder and wait until it is quit. Returns its exit code."""
    exe = shutil.which(cmd[0])
    if not exe:
        raise RuntimeError("'%s' is not on PATH." % cmd[0])
    sys.stdout.flush()
    # Ctrl+C typed in the CLI also reaches this process; it belongs to the CLI, so do nothing
    # with it here. (A handler, not "ignore": an ignored signal would be inherited by the CLI.)
    import signal
    old = signal.signal(signal.SIGINT, lambda *_: None)
    started = time.time()
    try:
        code = subprocess.Popen([exe] + cmd[1:], cwd=cwd, env=child_env()).wait()
    finally:
        signal.signal(signal.SIGINT, old)
    if code and time.time() - started < 5 and sys.stdin.isatty():
        # It failed straight away: keep its message readable before the list covers the screen.
        print("\n%s stopped with an error (exit code %s). Press Enter to go back to the list." % (cmd[0], code))
        sys.stdout.flush()
        sys.stdin.readline()
    return code


def handoff(src, target, flags=()):
    source = ADAPTERS[src.tool]
    cwd = src.cwd
    if not os.path.isdir(cwd):
        print("Original folder %s no longer exists; using the current folder." % cwd)
        cwd = os.getcwd()
    if target.name == src.tool:
        # Same tool: nothing to convert, just run its own resume where the session lives.
        return launch(target.resume_cmd(src.id, flags), cwd)
    # Notes from an earlier import are dropped; the new copy gets one of its own.
    ev = own_events(join_calls(source.read(src.path, sid=src.id), MAX_OUTPUT))
    if not ev:
        raise RuntimeError("Nothing to carry over; that session has no conversation in it.")
    calls = sum(1 for e in ev if e["k"] == "call")
    size = "%d messages and %d tool calls from %s session %s" % (len(ev) - calls, calls, src.tool, src.id[:8])
    links = load_links()
    mine = links.get(link_key(src.tool, src.id))
    thread = mine["thread"] if mine else str(uuid.uuid4())
    # The copy this conversation already has in the target tool, if any (the newest that is still there).
    copies = [k for k, v in links.items() if mine and v["thread"] == thread and k.startswith(target.name + ":")
              and not v.get("old") and target.exists(v["path"], k.split(":", 1)[1])]
    sibling = copies[-1] if copies else None

    if sibling:
        theirs, sid = links[sibling], sibling.split(":", 1)[1]
        try:
            new = missing_from(ev, join_calls(target.read(theirs["path"], sid=sid), MAX_OUTPUT))
        except Exception as e:
            print("Could not read your existing %s session %s (%s)." % (target.name, sid[:8], e))
            new = None
        if new == []:
            print("Your existing %s session %s already has everything." % (target.name, sid[:8]))
            return launch(target.resume_cmd(sid, flags), cwd)
        if new and target.can_append:
            target.append(theirs["path"], new)
            added = sum(1 for e in new if e["k"] != "call")
            print("Added %d new messages and %d tool calls to your existing %s session %s"
                  % (added, len(new) - added, target.name, sid[:8]))
            return launch(target.resume_cmd(sid, flags), cwd)
        if new:
            # Behind, but this CLI cannot take new turns into an existing session. The old copy is
            # left as it is and no longer listed; a new one replaces it.
            print("%s cannot add turns to an existing session, so this is a new session; the earlier "
                  "copy stays in its history." % target.label)
            links[sibling]["old"] = True
        else:
            print("The %s copy of this conversation cannot be brought up to date (the two have moved on "
                  "separately, or one was compacted)." % target.name)
            print("Starting a new %s session from this one instead." % target.name)
            del links[sibling]

    sid, path = target.write(ev, cwd, src.title, source.label)
    links[link_key(src.tool, src.id)] = {"thread": thread, "path": str(src.path)}
    links[link_key(target.name, sid)] = {"thread": thread, "path": str(path)}
    save_links(links)
    print("Carried over %s into new %s session %s" % (size, target.name, sid))
    return launch(target.resume_cmd(sid, flags), cwd)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if len(sys.argv) > 1:
        print(__doc__.strip() + "\n\nxresume " + __version__)
        return 0 if sys.argv[1] in ("-h", "--help", "help") else 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("xresume: this is an interactive tool; run it in a terminal.")
    ui = {}
    while True:                                          # quitting a CLI comes back to the list
        init_terminal()
        sessions = all_sessions()
        if not sessions:
            sys.exit("xresume: no Claude Code, Codex, agy, OpenCode or Grok conversations found.")
        got = picker(sessions, ui)
        if not got:
            return 0
        try:
            handoff(*got)
        except RuntimeError as e:
            ui["msg"] = str(e)
        except Exception as e:
            ui["msg"] = "Could not open that conversation: %s: %s" % (type(e).__name__, e)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
