# Changelog

## 4.1.0 (2026-09-30)

### Fixed
- An error after saving and when closing the window ("Internal C++ object ... QTimer already deleted").
  In 4.0.0 it could stop shutdown before every open note was saved; the recovery journal kept the
  unsaved text. Shutdown now saves every open note before closing any of them.
- The minimize, maximize and close buttons are the same size and aligned with the rest of the top bar.

### Search
- One search box in the top bar, also opened with Ctrl+Shift+P. Plain text searches note names and note
  text. A leading `>` lists commands, `#` lists tags, `@` lists the headings of the open note, `:` goes to
  a line number and `?` lists the prefixes. The prefixes are shown along the bottom of the palette and
  can be clicked.
- Enter opens the first result, even when pressed before the results appear. Result snippets leave out
  heading marks.
- Ctrl+G goes to a line. Ctrl+Shift+F also opens note search.
- The palette replaces the Search commands field, the Search notes button and the Search and Tags panel
  pages. The outline keeps its panel (Ctrl+Shift+O).

### Home
- A Home page with notes to continue, recent changes across the library (including changes synced from
  other PCs), pinned notes, the most used tags and the library folders. It appears when no note is open,
  and from the Home button or Alt+Home while tabs stay open.

### Interface
- One top bar combines the title bar and the toolbar, with the search box in the centre and Windows 11
  style window buttons. The window has rounded corners and a border in the theme colour.
- Tabs show a save and sync state dot and a marker on the active tab. The close button appears on the
  active or hovered tab, a middle click closes a tab, and a + button creates a note.
- The library shows notes without the `.md` extension, indentation guides, a New note button on hovered
  folders, dots on open notes, and whether each library folder is in OneDrive or on this PC.
- The sidebar has rows for Home, Search and New note, and a summary of the save and sync state of open
  notes.
- Keyboard shortcuts are shown as keys. The palette and messages fade in; Reduce motion turns this off.

### Upgrading from 4.0.0
- Settings, open tabs, version history and the search index are kept. The tip about the new search is
  shown once.

## 4.0.0 (2026-09-30)

Paperless Notes 4 is a rewrite of version 3 around one rule: the note on disk is the source of truth.

### Notes and editing
- The editor shows the Markdown file itself, styled live (headings, emphasis, lists, task boxes, quotes,
  code, tables); marks are hidden except on the line you are editing. A note you only read is never
  rewritten, and edits change only the lines you touch, keeping the file's encoding and line endings.
- Insert and Format menus, a `/` command menu, a formatting bar for selections, smart lists and task
  counts, links, tables, code blocks, image paste and drop into an `assets` folder, safe paste from web
  pages, and line move and duplicate. Every command is one undo step.

### Saving, sync and history
- Atomic saves through a temporary file in the same folder; automatic saving with a recovery journal
  that survives crashes; per-PC version history with compare and restore.
- OneDrive awareness: a plain-language status line, changes from other PCs applied in place with margin
  marks, and a conflict flow that never overwrites (compare, keep yours, keep theirs, keep both).

### Organising
- Library sidebar with folders you add, tabs with pins and recent order, command search.
- Search of note contents with a local index, `#tags` with a tag browser, find and replace, an outline of
  headings, two-pane split view, and export to HTML, PDF and plain text or copy as Markdown and rich
  text.

### Release
- Per-user installer (no administrator rights) and a portable zip, both built from one verified onedir
  folder, with SHA-256 sums.
- Startup failures now show a message instead of closing silently; logs keep folders and the user name
  out; only local `.md`, `.markdown` and `.txt` files open from the command line or "Open with".
- Source available under the Paperless Notes Source-Available License; third-party notices included.

### Migration from version 3
- Version 4 is installed separately and keeps its own settings in `%LOCALAPPDATA%\Paperless Notes`.
- It reads the last used folder and the interface scale from version 3 once; nothing is written back.
- Version 3 notes are plain Markdown files: add their folder with **Add folder**. Version 3 backup
  folders are left as they are.

### Known limitations
- Unsigned: Windows SmartScreen may warn on first start.
- Live styling is off for notes above 512 KB; notes above 10 MB do not open for editing.
- Search skips notes above 2 MB and online-only OneDrive files, and cannot match parts of words in
  scripts without spaces; changes made by other programs reach search at the next window activation.
- Images do not move with a moved note; unused images are not removed automatically.
- At most two panes in split view; no spell check.
