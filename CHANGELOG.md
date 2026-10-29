# Changelog

## 4.4.0 (2026-10-29)

### Writing
- Spelling is rebuilt. Obvious typos such as teh, recieve or becuase are corrected as soon as the word
  ends, each correction as its own undo step, and a small note under the word shows the change: Esc puts
  your word back this time and the same typo is still corrected next time, Shift+Esc puts it back and adds
  it to your dictionary, and Ctrl+Z also undoes it. Names, slang and less certain words are only underlined: rest the
  pointer on one, or press Ctrl+Period with the caret in it, for suggestions, Add to dictionary and Ignore.
  Right-clicking the word lists the same. Settings, Writing, Fix obvious typos as you type turns the
  corrections off. The faded suggestion drawn over the word and its bar are gone.
- Enter inside bold, italic or strikethrough keeps both lines formatted; Backspace or Delete at the join
  makes the run whole again. Each is one undo step.
- Bold, italic, strikethrough, code and links can run across a line break inside one paragraph, list item
  or quote, as on GitHub and in Obsidian, in the editor and in exports.
- Pasting from web pages, Word or Google Docs no longer leaves stray or empty `**` marks. Quotes and list
  items keep their structure, and text pasted inside bold takes on the bold.
- Ctrl+B, Ctrl+I and strikethrough leave punctuation next to a word outside the marks, so the result shows
  as bold everywhere.
- Enter in a quote continues the quote; Enter on an empty quote line ends it.
- The word count leaves out Markdown marks: list dashes, heading hashes, quote marks, table pipes, task
  boxes and list numbers.
- A new, empty note shows a faint "Use [/] for quick slash commands!" until you type.

### Tab groups
- Split view is replaced by tab groups, as in Notepad++. The button beside the search box, or
  Ctrl+Shift+\\, opens the current note in a new group beside it, up to three groups. Drag a tab onto
  another group, or right-click it and choose Move to group. A group closes with its last tab, F6 moves to
  the next group, and the groups come back when the app starts. A second pane saved by 4.3.0 comes back as
  a second group.

### Note colours
- Format, Theme chooses one note's background and text colours with colour pickers. The page, title and
  buttons above the text take them on, the other colours are mixed from them, and the dialog warns when
  the text would be hard to read. Every view of that note shows them, they are kept on this PC, and they
  follow the note when it or its folder is renamed. Use default theme goes back to the theme in Settings,
  which is now called Default theme.
- Export as HTML and PDF follow the note's colours, with the colour reaching the edge of each PDF page.

### Interface
- A separate Export menu, last in the row beside Note, holds every export and copy option. The Note menu
  keeps Find, Replace and the outline. The buttons in that row are evenly spaced.
- On first start the window opens at a moderate size in the middle of the screen instead of filling it,
  and a remembered size that no longer fits the screen is brought back onto it.
- Closing the sidebar slides, with the page moving along, instead of snapping.
- The check after a save stays for as long as the note is saved.
- Settings is shorter and scrolls.
- Tips stay on screen, even when the window is wider than the screen.
- The top-right corner pixel of the window now hits Close.
- Settings is redesigned with sections for Appearance, Writing, Saving and Window, and switches that slide.
- Switching between light and dark fades smoothly, the window fades in when the app starts, and dialogs
  fade in.
- The slash menu, the emoji list, the selection format bar and the spelling popups share one look and fade
  into place.
- The status bar shows a small turning mark while a save takes more than a moment, then a check once the
  note is saved.
- Windows no longer flash on the screen for a moment while the app starts.
- Settings, Window, Reduce motion turns all of these animations off.

### Export
- Export as shown, in the Export menu: a PDF on US Letter pages, one long PNG image, or a web page that looks
  the way the note looks in the editor, in your theme, note font, zoom and column width, with its title,
  dots, task boxes, quote bars, code panels and rules. Pages break between lines and keep a heading with the
  text under it. The existing HTML, PDF and plain text exports are unchanged.

### Fixes
- Typing `- ` under a list item or quote no longer turns the line above into a large heading for a moment.
  Lists and quotes followed by a dash or equals line are never read as headings, in the editor, the outline
  or exports.
- List dashes, task boxes and quote bars always become dots, boxes and bars. Lines with emoji lost them, and
  the line you left after an undo or paste could keep its raw dash.
- With the same note in two tab groups, the narrower group wraps its lines instead of scrolling sideways.

## 4.3.0 (2026-10-01)

### Writing
- Built-in spell checking that runs entirely in the app, with no Windows service and no network. Unknown
  words outside code, links, tags, HTML and front matter get a wavy underline. When a misspelled word is
  finished, its likely correction appears in its place in faded text: Tab accepts it, Esc keeps what you
  typed, and a small bar offers Accept, Keep and Add to dictionary. Right-click a word for up to five
  suggestions. The personal dictionary is kept with the local settings. Settings, Spelling, Check
  spelling turns it off.
- Emoji shortcodes: type a colon and two letters (for example `:sk`) for a list of matching emoji. Tab
  or Enter inserts the highlighted one, the arrow keys move through the list and Esc closes it. Typing
  the closing colon (`:skull:`) replaces the code at once. Shortcodes are not offered in code, links or
  front matter.
- A horizontal rule (`---`) draws as a rule. A `---` on the first line opens front matter only when the
  next line looks like `key:`. Insert, Horizontal rule leaves the caret on a new line below the rule.

### Interface
- Motion throughout: messages slide up from the bottom, the sidebar and the outline and help panels slide
  in and out, the find bar and the sync banners slide down, Home's sections rise into place, a note fades
  in when you leave Home, the marker on the active tab slides to the next tab, library folders unroll,
  tips fade in, and hover highlights fade. Settings, Motion, Reduce motion turns every animation off.
- Tabs share their width like a browser: they shrink as tabs are added, grow as tabs close, and a new
  tab grows in. A single tab no longer leaves a large gap before the New tab button. Close buttons show
  on the active and hovered tabs.
- Dragging the top bar of a maximized window restores it under the pointer, so it can be moved and
  resized again.
- The logo button at the far left of the top bar is removed. Home stays in the sidebar, on Alt+Home and
  in the search palette.
- A single click opens a note in the library, and a single click on a folder opens or closes it.
- Library highlights start at the row's own level, so the folder guide lines no longer run through
  them, and folders open without the jump that followed the Loading row.
- The status dots are removed. "Saved" and "All open notes saved" change colour instead: amber while
  saving, red when a note needs attention.
- Pinned tabs show their whole name when it fits.

### Export
- HTML and PDF exports follow the editor: the note's typeface at regular weight (PDFs were all bold),
  natural line spacing instead of 1.5, the editor's heading sizes, italic quotes with a bar (in PDFs
  too), and done tasks struck through.

### Fixed
- Styling after an emoji, or any other character outside the Basic Multilingual Plane, landed one
  character early.
- A rare crash: Python's cycle collector could run on a background thread and free interface objects
  there. Collection now runs only on the interface thread.

### Upgrading from 4.2.0
- Settings, open tabs, version history and the search index are kept. Spell checking is on by default.

## 4.2.0 (2026-09-30)

### Interface
- Hovering the maximize button opens the Windows 11 snap layouts, as in other Windows programs. This
  needs "Show snap layouts when I hover over a window's maximize button" to be on in Windows Settings
  (System, Multitasking); it is on by default.
- Tabs, the path above the note, the window title, the split view header and the "Go to note" commands
  show note names without the `.md` or `.markdown` extension, the same way the library does. Other
  extensions, such as `.txt`, stay visible.

### Upgrading from 4.1.0
- Settings, open tabs, version history and the search index are kept.

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
