# xresume

> Resume any coding-agent conversation in the CLI it came from, or carry it into another one.

xresume puts every conversation you have had in **Claude Code**, **Codex CLI**,
**Antigravity CLI (agy)**, **OpenCode** and **Grok CLI** into one searchable list. Press Enter to resume one
where it lives, or press Right and pick another CLI to continue it there with its history intact:
messages, tool calls and tool results.

[Project website](https://eekebin.github.io/xresume/) | [Report an issue](https://github.com/EEkebin/xresume/issues)

---

## Contents

- [Install](#install)
- [Uninstall](#uninstall)
- [Features](#features)
- [Usage](#usage)
- [How it works](#how-it-works)
- [Limits](#limits)
- [License](#license)

---

## Install

**macOS and Linux**

```sh
curl -fsSL https://eekebin.github.io/xresume/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://eekebin.github.io/xresume/install.ps1 | iex
```

Both install a standalone executable, so nothing else is needed, on Windows x64, Linux x64 and
arm64, and macOS on Apple Silicon. On other systems they install the Python version, which needs
Python 3.8 or newer. Run the same command again to update.

You can also download an executable yourself from the
[latest release](https://github.com/EEkebin/xresume/releases/latest).

---

## Uninstall

macOS and Linux:

```sh
rm -rf ~/.local/bin/xresume ~/.xresume
```

Windows (PowerShell):

```powershell
irm https://eekebin.github.io/xresume/uninstall.ps1 | iex
```

These remove xresume and `~/.xresume`, where it keeps its links and backups. Sessions xresume
created stay in each CLI's own history.

---

## Features

- **One list for every CLI.** Newest first, colour-coded per tool, searchable by title, folder or id.
- **Move a conversation between CLIs.** A real import into the other CLI's own session format, not a pasted summary.
- **One session per CLI.** Move a conversation back later and only the new turns are added to the copy already there.
- **Comes back when you quit.** Leave a CLI and the list reopens, ready for the next one.
- **Preview before you open.** A side panel shows the last few messages of the selected conversation.
- **Skip-permissions toggle.** One key launches the CLI with its no-prompts flags.
- **Keyboard and mouse.** Arrows, `/` to search, a menu for choosing the CLI, click, double-click and wheel.
- **Backups first.** Any session xresume adds to is copied to `~/.xresume/backups` beforehand.
- **Nothing to set up.** A standalone executable per platform; the source is one Python file with no dependencies.

---

## Usage

Run `xresume`. There are no subcommands or options; everything happens in the list.

Quit the CLI you opened and you are back in the list, so switching a conversation from one CLI
to another is: quit, press Right, pick the CLI, press Enter.

| Key | What it does |
| --- | --- |
| **Up / Down**, PgUp / PgDn, Home / End | Move |
| **/** | Search by tool, title, folder or id; Esc cancels the search |
| **Enter** | Resume the conversation in the CLI it came from |
| **Right** | Open the conversation in another CLI: a menu lists them with the current one first; Up / Down choose, Enter opens, Esc closes the menu |
| **Space** or **p** | Show or hide the preview panel (**Ctrl+P** while searching) |
| **Tab** | Turn skip-permissions on or off for the launch |
| **Esc** | Exit |
| **Ctrl+C** twice | Exit |
| **Mouse** | Hover marks a row, click selects, double-click opens, wheel scrolls |

Conversations without a title are not listed. The preview panel needs a window at least 90 columns wide.

Tab launches with these flags, shown in the footer before you press Enter:

| CLI | Flags |
| --- | --- |
| Claude Code | `--dangerously-skip-permissions` |
| Codex CLI | `--yolo --no-daemon` |
| Antigravity CLI | `--dangerously-skip-permissions` |
| OpenCode | `--auto` |
| Grok CLI | `--always-approve` |

Only CLIs found on your PATH are offered in the menu.

---

## How it works

- **Enter** runs the CLI's own resume in the conversation's folder.
- **Right** reads the conversation, writes it as a real session in the other CLI's own format and opens it there. The model gets the earlier messages, tool calls and results as its own history, and the original is left as it was.
- **Moving it again** only adds the new turns to the copy that CLI already has, after backing it up. Linked copies show as one row.
- **OpenCode is the exception:** xresume hands the conversation to `opencode session import` instead of writing to OpenCode's database.

---

## Limits

- **Antigravity's format is undocumented.** Support follows what agy 1.5 writes, so an agy update can break it.
- **Grok support follows Grok CLI 1.0.** Grok documents where it keeps sessions but not every field in them, so a Grok update can break it.
- **OpenCode cannot add turns to an existing session.** Moving a conversation back into OpenCode after it has grown creates a new OpenCode session; the earlier one stays in OpenCode's history but is no longer listed by xresume.
- **Hidden reasoning is not carried over.**
- **Codex shows imported messages but not imported tool calls.** The model still has the tool calls and their results; they are just not redrawn on Codex's screen.
- **Long tool outputs are trimmed** to 6,000 characters each when a conversation is moved.
- **Compacted or separately continued copies cannot be merged.** xresume starts a fresh copy in the target CLI instead.
- **Close the target session first.** Do not move a conversation into a CLI that has that same session open.
- **Tested on Windows and Linux.** macOS shares the Linux code but has not been tested by hand.
- **The executables are not code-signed.** If you download one in a browser, Windows or macOS may warn before running it. The install commands above do not trigger that.

---

## License

[MIT](LICENSE)
