# Paperless Notes

A focused Windows Markdown editor built for ordinary files, safe saving, and OneDrive-aware work.

Paperless Notes keeps your writing readable outside the app. Every note is a normal `.md`, `.markdown`
or `.txt` file, and the editor styles the source as you type. It does not silently replace a newer copy
from another PC, rewrite a note merely because you opened it, send telemetry, or depend on a cloud
account.

Paperless Notes is made by [@peeksxx](https://peeksxx.dev). It is source available, not open source.
See [LICENSE](LICENSE) for the terms.

## Download

Paperless Notes 4.2.0 supports 64-bit Windows 10 and Windows 11.

- [Download the Windows installer](https://github.com/Peeksxx/Paperless-Notes/releases/download/v4.2.0/Paperless-Notes-4.2.0-setup.exe)
- [Download the portable ZIP](https://github.com/Peeksxx/Paperless-Notes/releases/download/v4.2.0/Paperless-Notes-4.2.0-portable-win64.zip)
- [View checksums and release notes](https://github.com/Peeksxx/Paperless-Notes/releases/tag/v4.2.0)

The installer is per-user, needs no administrator access, and installs the program to
`%LOCALAPPDATA%\Programs\Paperless Notes`. Desktop and **Open with** integration are optional and off
by default. The portable build runs without installation, but uses the same local data folder as the
installed build.

The current release is unsigned. Windows SmartScreen may show a warning on first launch. Confirm that
the SHA-256 value matches `SHA256SUMS.txt` before choosing **More info**, then **Run anyway**.

## What it does

- Live Markdown styling for headings, emphasis, lists, tasks, quotes, code, tables, links, and images.
- Insert and Format tools, `/` commands, selection formatting, smart lists, task counts, and one-step
  undo for authoring commands.
- Automatic and manual saving through an atomic same-folder replacement, preserving encoding and line
  endings.
- Crash recovery journals and per-PC version history with compare and restore.
- OneDrive status, external-change detection, changed-line marks, and explicit conflict handling.
- One search palette (Ctrl+Shift+P) for note names and text, commands, `#tags`, headings, and line
  numbers.
- A Home page with notes to continue, recent changes from every PC, pinned notes, and tags.
- Folder library, pinned and recent tabs, and an outline of the open note.
- Find and replace, two-pane split view, and export to HTML, PDF, or plain text.
- Copy as Markdown or rich text. Pasted images are validated and stored in an `assets` folder beside
  the note.
- Light and dark themes, interface scaling, keyboard navigation, and graphite as the default accent.

## Where files are stored

Paperless Notes keeps its own files together instead of scattering generated notes around the system.

- New app-created notes go in `%LOCALAPPDATA%\Paperless Notes\Notes` unless you deliberately select a
  different library folder.
- Settings, open tabs, recovery drafts, local history, sync evidence, the search index, and private logs
  stay in `%LOCALAPPDATA%\Paperless Notes`.
- The installed program stays separately in `%LOCALAPPDATA%\Programs\Paperless Notes`.
- To sync notes, create or select a folder inside OneDrive, then choose **Add folder** in the sidebar.
  OneDrive remains the program that transfers those files.
- Uninstall removes the program, shortcuts, and optional file associations. It does not delete notes,
  libraries, settings, drafts, or history.

The app never creates notes in random folders. Opening an existing file or adding a library elsewhere
is always an explicit user choice.

## Saving and OneDrive

Notes save shortly after typing stops, when switching tabs, when the window loses focus, or immediately
with Ctrl+S. Each save writes a temporary file next to the note and atomically replaces the original,
so another program never sees a half-written note.

If saving fails, the editor retains the text and recovery journal. On the next launch, recoverable work
is offered back to you.

The status line distinguishes local saves, uploads that are pending, and files OneDrive reports as
uploaded. This reflects the OneDrive state visible on the current PC, not an independent cloud check.
Online-only files are hydrated only when opened, and search does not download them.

When another PC changes an open note:

- A clean local note updates in place.
- Non-overlapping work can be merged safely.
- Overlapping edits produce a persistent warning with only valid actions.
- Compare, keep yours, keep theirs, keep both, and dismiss appear only when that diagnosis permits them.
- The app retains the evidence needed to explain important changes after restart.

## Privacy and safety

- No telemetry, update checker, account system, network client, or network server.
- No bundled Python socket module or Qt Network module.
- Links open in your browser only when you Ctrl+click them.
- Command-line and **Open with** requests accept only local `.md`, `.markdown`, and `.txt` files.
- UNC paths, mapped drives, substituted drives, junctions, symbolic links, alternate data streams, and
  Windows device names are refused at the launch boundary.
- Logs never contain note text, search terms, or clipboard contents. Private folders and the Windows
  user name are redacted.
- Search data, recovery drafts, history, and sync evidence remain local to that PC.

The Python and Qt platform runtimes can still import normal Windows system networking DLLs internally.
The application itself does not use them to communicate.

History and recovery data are not a substitute for backups. Keep another copy of important notes, such
as OneDrive version history or a backup on another drive.

## Everyday use

Use the **Insert** and **Format** controls above the editor, or type `/` at the start of a line. The
**Note** menu contains find and replace, split view, export, and copy options.

The search box in the centre of the top bar, or Ctrl+Shift+P, opens the search palette. Plain text
searches note names and the text of every eligible note in the library. A leading character changes
what the palette lists:

| Type | To |
| --- | --- |
| `>` | Run a command by name |
| `#` | List tags, then the notes that carry one |
| `@` | Jump to a heading in the open note |
| `:` | Go to a line number in the open note |
| `?` | Show these prefixes |

The prefixes are also shown along the bottom of the palette. Enter opens the first result.

Home appears when no note is open, and from the Home button or Alt+Home. It lists notes to continue,
recent changes across the library (including changes synced from other PCs), pinned notes, the most
used tags, and the library folders.

### Keyboard shortcuts

| Action | Keys |
| --- | --- |
| Search notes and commands | Ctrl+Shift+P (then `>`, `#`, `@`, `:` or `?`) |
| Search notes | Ctrl+Shift+F |
| Home | Alt+Home |
| Go to line | Ctrl+G |
| New note, open file, save | Ctrl+N, Ctrl+O, Ctrl+S |
| Close tab, reopen tab, recent tab | Ctrl+W, Ctrl+Shift+T, Ctrl+Tab |
| Back and forward | Alt+Left, Alt+Right |
| Sidebar, help, settings | Ctrl+\\, F1, Ctrl+, |
| Version history | Ctrl+Shift+H |
| Find, replace, next, previous | Ctrl+F, Ctrl+H, F3, Shift+F3 |
| Outline, switch split pane | Ctrl+Shift+O, F6 |
| Bold, italic, strike, code, link | Ctrl+B, Ctrl+I, Ctrl+Shift+X, Ctrl+E, Ctrl+K |
| Headings 1 through 3 | Ctrl+Alt+1 through Ctrl+Alt+3 |
| Move or duplicate lines | Alt+Up, Alt+Down, Shift+Alt+Down |
| Paste as plain text | Ctrl+Shift+V |
| Zoom | Ctrl+Plus, Ctrl+Minus, Ctrl+0 |

Every shortcut also has a visible button or menu item. Press F1 for the built-in guide.

## Current limitations

- Windows only, and the 4.2.0 binaries are not code-signed.
- Notes above 512 KB open without live styling. Files above 10 MB are not opened for editing.
- Search skips note contents above 2 MB and online-only files. Their names can still appear.
- Search does not match part of a word in scripts commonly written without spaces, including Chinese
  and Japanese.
- Changes made by other programs reach the search index when the window is next activated.
- Moving a note does not move its `assets` images, and unused images are not removed automatically.
- Split view supports two panes. Caret-line styling follows the pane used most recently.
- Export omits front matter and HTML comments. PDF export uses the light theme.
- There is no spell checker. Screen readers read the Markdown source and its punctuation.
- Upload status depends on the OneDrive signal reported on the current PC.

## Moving from Paperless Notes 3

Version 4 installs separately and reads only the last folder and interface scale from version 3, once.
It does not modify the old settings or backup folders. Because version 3 notes are ordinary Markdown
files, choose **Add folder** and select their existing folder.

## Verify a download

Download `SHA256SUMS.txt` from the release, then run:

```powershell
Get-FileHash .\Paperless-Notes-4.2.0-setup.exe -Algorithm SHA256
Get-FileHash .\Paperless-Notes-4.2.0-portable-win64.zip -Algorithm SHA256
```

Compare the output with the matching entries in `SHA256SUMS.txt`.

## Build from source

Building requires 64-bit Windows, Python 3.13.7, and PowerShell. Inno Setup 6 is needed for the
installer.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File tools\build_release.ps1
```

The release script accepts only a clean, committed worktree. It runs the packaging checks, builds the
onedir application, verifies its contents, creates the portable ZIP and installer, and writes
`SHA256SUMS.txt`. Output stays under the ignored `dist` folder.

## Verification status

The 4.2.0 source passed:

- Text hygiene, Ruff lint and formatting, and strict mypy checks.
- 1,231 functional tests with 91.60 percent coverage.
- 44 untraced timing and memory tests.
- 5,300 generated sync worlds and mutation checks.
- Layout checks at 100, 125, 150 and 200 percent in both themes, at 1365 by 900 and 720 by 480.

## License and third-party software

Paperless Notes is source available under the [Paperless Notes Source-Available License](LICENSE).
Qt for Python and other redistributed components retain their own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the [licenses](licenses) folder. This summary is not
legal advice.

Paperless Notes by @peeksxx (Discord, Telegram) - peeksxx.dev

Contact: `@peeksxx` on Discord or Telegram.
