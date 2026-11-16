"""Tests for xresume. Run: python tests/test_xresume.py

Everything happens in a throwaway folder; no real Claude Code, Codex or agy data is read or written."""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

work = tempfile.mkdtemp(prefix="xr-review-")
os.environ["XRESUME_HOME"] = os.path.join(work, "state")
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("xr", os.path.join(here, "..", "xresume.py"))
xr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xr)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
A = xr.ADAPTERS
ok = []


def check(label, good, detail=""):
    ok.append(bool(good))
    print("%s %s %s" % ("PASS" if good else "FAIL", label, detail))


# timestamps
check("iso_now shape", len(xr.iso_now()) == 24 and xr.iso_now().endswith("Z"), xr.iso_now())
check("to_dt 9-digit fraction", xr.to_dt("2026-11-14T10:00:00.123456789Z").microsecond == 123456)
check("to_dt 1-digit fraction", xr.to_dt("2026-11-14T10:00:00.5Z").microsecond == 500000)
check("to_dt naive is utc", xr.to_dt("2026-11-14T10:00:00").utcoffset().total_seconds() == 0)
check("negative varint terminates", len(xr.pb_varint(-1)) == 10)

# fit
check("fit plain", xr.fit("hello", 10) == "hello")
check("fit pad", xr.fit("hello", 8, pad=True) == "hello   ")
check("fit cut", xr.fit("hello world", 8) == "hello w…", repr(xr.fit("hello world", 8)))
check("fit wide", xr.fit("日本語のタイトル", 8) == "日本語…", repr(xr.fit("日本語のタイトル", 8)))
check("fit wide pad is 8 columns", xr.fit("日本語のタイトル", 8, pad=True) == "日本語… ")
check("fit strips escapes", "\x1b" not in xr.fit("a\x1b[2Jb", 20))
check("typed", xr.typed("hi") and not xr.typed("[xresume] note") and not xr.typed("<system-reminder>x") and not xr.typed("  "))

# backups: timestamped, newest five kept
f = os.path.join(work, "abc.jsonl")
open(f, "w").write("{}\n")
real_dt = xr.datetime


class FakeDT(real_dt):
    n = 0

    @classmethod
    def now(cls, tz=None):
        cls.n += 1
        return real_dt(2026, 1, 1, 0, 0, cls.n % 60, tzinfo=tz)


xr.datetime = FakeDT
for _ in range(8):
    xr.append_lines(f, ["{}"])
xr.datetime = real_dt
kept = sorted(os.listdir(os.path.join(work, "state", "backups")))
check("five backups kept", len(kept) == 5, str(kept[-2:]))
check("file appended 8 times", sum(1 for _ in open(f)) == 9)

# Claude: round trip, append onto a huge last line, title rules
A["claude"].root = xr.Path(work) / "claude" / "projects"
ev = [{"k": "user", "text": "first question", "ts": "2026-11-14T10:00:00.000Z"},
      {"k": "assistant", "text": "first answer", "ts": "2026-11-14T10:00:01.000Z"},
      {"k": "call", "id": "call_1", "name": "Bash", "input": {"command": "ls"}, "output": "a.txt", "ts": "2026-11-14T10:00:02.000Z"}]
sid, path = A["claude"].write(ev, work, "My title", "the Codex CLI")
s = A["claude"].sessions()[0]
check("claude write+list", s.id == sid and s.title == "My title")
back = xr.join_calls(A["claude"].read(path), 6000)
check("claude round trip in sync", xr.missing_from(ev, back) == [])
# a 3 MB entry at the end of the chain must still be the point new turns hang from
last_uuid = xr.loads(list(xr.read_lines(path))[-2])["uuid"]
big = xr.dumps({"parentUuid": last_uuid, "isSidechain": False, "type": "attachment", "attachment": "x" * 3000000,
                "uuid": "11111111-1111-4111-8111-111111111111", "cwd": work, "sessionId": sid})
open(path, "a", encoding="utf-8", newline="\n").write(big + "\n")
A["claude"].append(path, [{"k": "user", "text": "second question", "ts": ""}])
lines = list(xr.read_lines(path))
check("append chains from the huge last entry", xr.loads(lines[-2])["parentUuid"] == "11111111-1111-4111-8111-111111111111")
check("append readable", [e["text"] for e in A["claude"].read(path) if e["k"] == "user"][-1] == "second question")
# title falls back past the xresume note; customTitle beats aiTitle
txt = open(path, encoding="utf-8").read().replace('"type":"ai-title"', '"type":"x"').replace('"aiTitle"', '"zz"')
open(path, "w", encoding="utf-8", newline="\n").write(txt)
check("title skips the xresume note", A["claude"].sessions()[0].title == "first question", A["claude"].sessions()[0].title)
open(path, "a", encoding="utf-8", newline="\n").write('{"type":"custom-title","customTitle":"Mine"}\n{"type":"ai-title","aiTitle":"Auto"}\n')
check("custom title wins", A["claude"].sessions()[0].title == "Mine")

# Codex: write, append continues ordinals
A["codex"].home = xr.Path(work) / "codex"
A["codex"].root = A["codex"].home / "sessions"
A["codex"].index = A["codex"].home / "session_index.jsonl"
A["codex"].home.mkdir()
A["codex"]._meta_template = lambda: {}
tid, cpath = A["codex"].write(ev, work, "Codex title", "Claude Code")
before = [xr.loads(l)["ordinal"] for l in xr.read_lines(cpath)]
A["codex"].append(cpath, [{"k": "user", "text": "more", "ts": ""}, {"k": "assistant", "text": "sure", "ts": ""}])
after = [xr.loads(l)["ordinal"] for l in xr.read_lines(cpath)]
check("codex ordinals continuous", after == list(range(len(after))) and len(after) > len(before))
check("codex list+title", A["codex"].sessions()[0].title == "Codex title")
check("codex round trip", xr.missing_from(ev + [{"k": "user", "text": "more"}, {"k": "assistant", "text": "sure"}],
                                           xr.join_calls(A["codex"].read(cpath), 6000)) == [])

# agy: write, title when agy renames it after the note, backup via sqlite includes WAL content
A["agy"].home = xr.Path(work) / "agy"
A["agy"].root = A["agy"].home / "conversations"
A["agy"].summaries = A["agy"].home / "conversation_summaries.db"
A["agy"].home.mkdir()
con = sqlite3.connect(str(A["agy"].summaries))
con.execute("create table conversation_summaries (conversation_id text primary key, title text, preview text, step_count integer, "
            "last_modified_time text, workspace_uris text, status text, project_id text, last_user_input_time text, "
            "last_user_input_step_index integer, app_data_dir text, raw_summary blob)")
con.commit()
con.close()
gid, gpath = A["agy"].write(ev, work, "Agy title", "Claude Code")
check("agy write+list", [(s.id, s.title) for s in A["agy"].sessions()] == [(gid, "Agy title")])
con = sqlite3.connect(str(A["agy"].summaries))
con.execute("update conversation_summaries set title = ?, preview = ?", ("[xresume] This conversation was started", "[xresume] x"))
con.commit()
con.close()
check("agy title past the note", A["agy"].sessions()[0].title == "first question", A["agy"].sessions()[0].title)
live = sqlite3.connect(str(gpath))            # like agy holding the file open with unflushed WAL data
live.execute("pragma journal_mode = wal")
live.execute("pragma wal_autocheckpoint = 0")
A["agy"].append(gpath, [{"k": "user", "text": "agy more", "ts": ""}])
bk = [b for b in os.listdir(os.path.join(work, "state", "backups")) if b.endswith(".db")]
n_backup = sqlite3.connect(os.path.join(work, "state", "backups", bk[0])).execute("select count(*) from steps").fetchone()[0]
n_now = live.execute("select count(*) from steps").fetchone()[0]
live.close()
check("agy backup is a valid pre-append database", len(bk) == 1 and n_now == n_backup + 1, "%d -> %d" % (n_backup, n_now))
check("agy round trip", xr.missing_from(ev + [{"k": "user", "text": "agy more"}], xr.join_calls(A["agy"].read(gpath), 6000)) == [])

# handoff: notes are not carried twice; unreadable sibling falls back to a fresh copy
launched = []
xr.launch = lambda cmd, cwd: launched.append(cmd) or 0
xr._installed.update(claude=True, codex=True, agy=True)
src = next(s for s in A["agy"].sessions())
xr.handoff(src, A["codex"])
k2 = launched[-1][-1]
p2 = next(s.path for s in A["codex"].sessions() if s.id == k2)
notes = [e for e in A["codex"].read(p2) if e["k"] == "user" and e["text"].startswith("[xresume]")]
check("one note in the new copy", len(notes) == 1 and "Antigravity" in notes[0]["text"], str(len(notes)))
check("one picker row for the linked pair", len([s for s in xr.all_sessions() if s.id in (gid, k2)]) == 1)
open(p2, "wb").write(b"\xff\xfe garbage")
n_before = len(A["codex"].sessions())
orig_read = A["codex"].read
A["codex"].read = lambda p: (_ for _ in ()).throw(ValueError("corrupt")) if str(p) == str(p2) else orig_read(p)
xr.handoff(src, A["codex"])
A["codex"].read = orig_read
check("unreadable sibling -> new session, no crash", launched[-1][-1] != k2)

# all_sessions survives a broken adapter
A["codex"].sessions = lambda: 1 / 0
check("broken adapter does not hide the others", any(s.tool == "claude" for s in xr.all_sessions()))

# picker logic: switching CLI applies to one row only
rows = [xr.Session("claude", "a" * 8, "p", work, "one", 2), xr.Session("claude", "b" * 8, "p", work, "two", 1)]
keys = iter(["right", "esc", "down", "enter"])
xr.WinConsole.open = staticmethod(lambda *a: type("C", (), {"old": 0, "read": lambda self: next(keys), "close": lambda self: None})())
real_out, sys.stdout = sys.stdout, open(os.devnull, "w", encoding="utf-8")
try:
    got = xr.picker(rows) if os.name == "nt" else None
finally:
    sys.stdout = real_out
if got:
    check("menu opened and closed, then Down, Enter resumes normally", got[0].title == "two" and got[1].name == "claude", got[1].name)
keys = iter(["right", "down", "enter"])
sys.stdout = open(os.devnull, "w", encoding="utf-8")
try:
    got = xr.picker(rows) if os.name == "nt" else None
finally:
    sys.stdout = real_out
if got:
    check("Right, Down, Enter opens it in another CLI", got[0].title == "one" and got[1].name != "claude", got[1].name)

# OpenCode: read from its database. It cannot take turns into an existing session, so a
# conversation moved back to it becomes a new session and the old copy is no longer listed.
import json

O = A["opencode"]
O.db = xr.Path(work) / "oc" / "opencode.db"
O.db.parent.mkdir()
con = sqlite3.connect(str(O.db))
con.execute("create table session_v2 (id text primary key, directory text, title text, time_updated integer, parent_id text, time_archived integer)")
con.execute("create table session_message (id text primary key, session_id text, type text, seq integer, data text)")
con.commit()
con.close()
oc_made = []


def oc_store(sid, title, messages, updated):
    c = sqlite3.connect(str(O.db))
    c.execute("insert into session_v2 values (?,?,?,?,null,null)", (sid, work.replace("\\", "/"), title, updated))
    for i, (kind, data) in enumerate(messages):
        c.execute("insert into session_message values (?,?,?,?,?)", ("%s-m%d" % (sid, i), sid, kind, i, json.dumps(data)))
    c.commit()
    c.close()


def oc_write(ev, cwd, title, src_label):          # stands in for `opencode session import`
    sid = "ses_test%d" % len(oc_made)
    msgs = [("user", {"time": {"created": 1}, "text": xr.note(src_label)})]
    for e in ev:
        if e["k"] == "user":
            msgs.append(("user", {"time": {"created": 2}, "text": e["text"]}))
        elif e["k"] == "assistant":
            msgs.append(("assistant", {"time": {"created": 3}, "content": [{"type": "text", "text": e["text"]}]}))
        else:
            msgs.append(("assistant", {"time": {"created": 3}, "content": [
                {"type": "tool", "id": e["id"], "name": e["name"], "state": {"status": "completed", "input": e["input"],
                                                                         "content": [{"type": "text", "text": e["output"]}]}}]}))
    oc_store(sid, title, msgs, int(time.time() * 1000) + len(oc_made))
    oc_made.append(sid)
    return sid, O.db


O.write = oc_write
oc_store("ses_native", "Native talk", [
    ("user", {"time": {"created": 1794802118199}, "text": "read the note"}),
    ("assistant", {"time": {"created": 1794802118299}, "content": [
        {"type": "reasoning", "text": "thinking"}, {"type": "text", "text": "reading"},
        {"type": "tool", "id": "t1", "name": "read", "state": {"status": "completed", "input": {"path": "n.txt"},
                                                              "content": [{"type": "text", "text": "hello"}]}}]}),
    ("idle", {"time": {"created": 1794802118399}, "outcome": "succeeded"})], 1794802118399)
native = next(s for s in O.sessions() if s.id == "ses_native")
got_ev = xr.join_calls(O.read(native.path, sid=native.id), 6000)
check("opencode list + read", native.title == "Native talk" and [e["k"] for e in got_ev] == ["user", "assistant", "call"]
      and got_ev[2]["output"] == "hello" and got_ev[0]["ts"].startswith("2026-"), [e["k"] for e in got_ev])
check("opencode tail read", len(O.read(native.path, 1, sid=native.id)) == 4)
check("opencode exists()", O.exists(O.db, "ses_native") and not O.exists(O.db, "ses_gone"))

csid, cpath2 = A["claude"].write([dict(e) for e in ev], work, "Hop talk", "x")
hop = next(s for s in A["claude"].sessions() if s.id == csid)
xr.handoff(hop, O)
first_oc = launched[-1][-1]
xr.handoff(hop, O)
check("level copy is reused", launched[-1][-1] == first_oc and len(oc_made) == 1)
A["claude"].append(cpath2, [{"k": "user", "text": "one more", "ts": ""}, {"k": "assistant", "text": "sure thing", "ts": ""}])
xr.handoff(hop, O)
second_oc = launched[-1][-1]
check("behind copy that cannot be appended to is replaced by a new session", second_oc != first_oc and len(oc_made) == 2)
check("the old copy was left as it was", O.exists(O.db, first_oc) and len(O.read(O.db, sid=first_oc)) == 5)
shown = [x for x in xr.all_sessions() if x.id in (csid, first_oc, second_oc)]
check("one row for the conversation, old copy not listed", len(shown) == 1, [(x.tool, x.id) for x in shown])
xr.handoff(hop, O)
check("the new copy is the one reused afterwards", launched[-1][-1] == second_oc and len(oc_made) == 2)

# Grok: a folder per session; Grok's own context lines are skipped, prompts are unwrapped
Gk = A["grok"]
Gk.root = xr.Path(work) / "grok" / "sessions"
native_dir = Gk.root / "C%3A%5Cwork" / "01a1e29a-4231-74f3-b77f-004c36a97be9"
native_dir.mkdir(parents=True)
(native_dir / "summary.json").write_text(json.dumps({"info": {"id": native_dir.name, "cwd": work}, "generated_title": "Grok talk"}))
(native_dir / "chat_history.jsonl").write_text("\n".join(json.dumps(o) for o in [
    {"type": "system", "content": "You are Grok"},
    {"type": "user", "content": [{"type": "text", "text": "<user_info>\nOS: windows\n</user_info>"}]},
    {"type": "user", "content": [{"type": "text", "text": "<system-reminder>skills</system-reminder>"}], "synthetic_reason": "system_reminder"},
    {"type": "user", "content": [{"type": "text", "text": "<user_query>\nread the note\n</user_query>"}], "prompt_index": 0},
    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
    {"type": "assistant", "content": "reading", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": "{\"target_file\": \"n.txt\"}"}]},
    {"type": "tool_result", "tool_call_id": "c1", "content": "hello"},
    {"type": "assistant", "content": "it says hello"}]) + "\n")
gs = next(x for x in Gk.sessions() if x.id == native_dir.name)
gev = xr.join_calls(Gk.read(gs.path, sid=gs.id), 6000)
check("grok list + read skips its own context", gs.title == "Grok talk" and [(e["k"], e.get("text")) for e in gev if e["k"] != "call"]
      == [("user", "read the note"), ("assistant", "reading"), ("assistant", "it says hello")] and gev[2]["output"] == "hello", gev)
gsid, gpath = Gk.write([dict(e) for e in ev], work, "Moved to grok", "Claude Code")
check("grok write lists and reads back level", xr.missing_from(ev, xr.join_calls(Gk.read(gpath), 6000)) == []
      and next(x for x in Gk.sessions() if x.id == gsid).title == "Moved to grok")
Gk.append(gpath, [{"k": "user", "text": "next question", "ts": ""}, {"k": "assistant", "text": "next answer", "ts": ""}])
idx = [json.loads(l).get("prompt_index") for l in open(str(gpath / "chat_history.jsonl"), encoding="utf-8")]
check("grok append continues the prompt numbering", [i for i in idx if i is not None] == [0, 1, 2], idx)
check("grok append readable", [e["text"] for e in Gk.read(gpath) if e["k"] == "user"][-1] == "next question")
check("grok summary counts updated", json.load(open(str(gpath / "summary.json")))["num_chat_messages"] == len(idx))
check("grok backups named per session", any(b.startswith(gsid + "-chat_history.") for b in os.listdir(os.path.join(work, "state", "backups"))))

# the "open in" menu sits under the row, or above it near the bottom of the screen
mx, my, inner, items = xr.menu_layout([A["claude"], A["codex"], O], "claude", 6, 100, 30)
check("menu below the row", my == 7 and items[0][1].endswith("(current)") and inner >= len(items[0][1]) + 3)
check("menu above the row near the bottom", xr.menu_layout([A["claude"], A["codex"], O], "claude", 27, 100, 30)[1] == 22)

print("RESULT: %d/%d" % (sum(ok), len(ok)))
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if all(ok) else 1)
