"""Export a note to HTML, PDF or plain text, and copy it as Markdown or rich text.

Every route is a projection: the note's source, dirty state, history, ledger and undo stack are never
touched. HTML, PDF and rich copy share ``mdio.render`` and one image policy: an image appears only when
its reference resolves (through the note's resource policy) to a file inside the note's own ``assets``
folder that is already on this PC, within the size limit and a valid image; otherwise its alt text is
shown. No other file is read and nothing is fetched. Files are written atomically after the target is
checked; cancelling or failing writes nothing and says why.
"""

from __future__ import annotations

import base64
import logging
import ntpath
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument

from paperless_notes.branding import PRODUCT_NAME
from paperless_notes.core import pathid
from paperless_notes.core.assets import ASSET_FOLDER, AssetService
from paperless_notes.core.fsops import Expect, FileSystem, atomic_save
from paperless_notes.core.local_state import NoteColors
from paperless_notes.core.onedrive import needs_hydration
from paperless_notes.core.runtime import IOExecutor, Outcome
from paperless_notes.core.security.links import link_allowed
from paperless_notes.core.security.resources import ResourcePolicy
from paperless_notes.mdio.document import SafeTextDocument, decode_image
from paperless_notes.mdio.render import RenderPolicy, render_html, render_plain
from paperless_notes.ui.shell.dialogs import Prompter
from paperless_notes.ui.shell.export_shown import ShownView, TooLongForImageError, shown_pdf, shown_png
from paperless_notes.ui.theme.tokens import LIGHT, NOTE_FONTS, Theme, note_theme, theme

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EMBEDDED_BYTES = 40 * 1024 * 1024
MAX_EXPORT_BYTES = 200 * 1024 * 1024
_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_STATIC_FACES = {"Segoe UI Variable": "Segoe UI", "Cascadia Mono": "Consolas"}


class ExportFormat(Enum):
    HTML = ("HTML", ".html", (".html", ".htm"), "Web page (*.html *.htm)")
    PDF = ("PDF", ".pdf", (".pdf",), "PDF document (*.pdf)")
    TEXT = ("plain text", ".txt", (".txt",), "Plain text (*.txt)")
    SHOWN_PDF = ("PDF as shown", ".pdf", (".pdf",), "PDF document (*.pdf)")
    SHOWN_PNG = ("image as shown", ".png", (".png",), "PNG image (*.png)")
    SHOWN_HTML = ("web page as shown", ".html", (".html", ".htm"), "Web page (*.html *.htm)")

    def __init__(self, label: str, default: str, extensions: tuple[str, ...], filter_text: str) -> None:
        self.label = label
        self.default = default
        self.extensions = extensions
        self.filter_text = filter_text


type Clipboard = Callable[[dict[str, str]], None]


def system_clipboard(data: dict[str, str]) -> None:
    """Put text formats on the clipboard in one go (``text/plain``, ``text/html``)."""
    from PySide6.QtCore import QMimeData

    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return
    mime = QMimeData()
    for kind, text in data.items():
        if kind == "text/plain":
            mime.setText(text)
        elif kind == "text/html":
            mime.setHtml(text)
    clipboard.setMimeData(mime)


def export_css(
    current: Theme,
    note_font: str = "sans",
    pdf: bool = False,
    view: ShownView | None = None,
    fill: bool = False,
) -> str:
    """Styling that matches the editor: the note's face at the note size, natural line spacing with one
    blank line's worth of space between blocks, the editor's heading sizes, and its quote, code, link and
    task styles. The PDF writer embeds variable faces at their default instance (heavy for Segoe UI
    Variable), so PDF output names static faces instead, and leaves the page framing to the PDF margins.
    With ``view`` the page takes the editor's column width, zoom and page colour all round, and styles the
    note title the way the editor's header shows it. With ``fill`` (a note with its own colours) the page
    colour reaches every edge of the page or window."""
    p = current.palette
    t = current.typography
    families = NOTE_FONTS.get(note_font, NOTE_FONTS["sans"])
    mono_families = t.mono_families
    if pdf:
        families = tuple(_STATIC_FACES.get(f, f) for f in families)
        mono_families = tuple(_STATIC_FACES.get(f, f) for f in mono_families)
    generic = {"serif": "serif", "mono": "monospace"}.get(note_font, "sans-serif")
    body = ", ".join(f'"{f}"' for f in dict.fromkeys(families)) + f", {generic}"
    mono = ", ".join(f'"{f}"' for f in dict.fromkeys(mono_families)) + ", monospace"
    display = ", ".join(f'"{f}"' for f in dict.fromkeys(t.display_families)) + ", sans-serif"
    gap = "0.9em"
    size = t.note_pt
    framing = "" if pdf else f"background: {p.page}; max-width: 46em; margin: 2em auto; padding: 0 1em; "
    shown = ""
    if fill:
        framing = f"background-color: {p.page}; " if pdf else framing
        shown = "" if pdf else f"html {{ background: {p.page}; }}\n"
    if view is not None:
        size = round(t.note_pt * view.zoom / 100, 2)
        framing = f"background: {p.page}; max-width: {view.text_width}px; margin: 3em auto; padding: 0 1em; "
        shown = (
            f"html {{ background: {p.page}; }}\n"
            f"h1.note-title {{ font-family: {display}; font-size: {t.title_pt}pt; font-weight: 600; "
            f"margin: 0 0 0.8em 0; }}\n"
        )
    return (
        shown + f"body {{ font-family: {body}; font-size: {size}pt; color: {p.text}; {framing}}}\n"
        f"p {{ margin: 0 0 {gap} 0; }}\n"
        "h1, h2, h3, h4, h5, h6 { font-weight: bold; margin: 1.1em 0 0.45em 0; }\n"
        "h1 { font-size: 1.6em; } h2 { font-size: 1.35em; } h3 { font-size: 1.2em; } "
        "h4 { font-size: 1.1em; } h5, h6 { font-size: 1em; }\n"
        f"ul, ol {{ margin: 0 0 {gap} 0; padding-left: 1.6em; }} li {{ margin: 0; }}\n"
        f"li.task {{ list-style: none; }} .done {{ color: {p.text_muted}; text-decoration: line-through; }}\n"
        f"code {{ font-family: {mono}; background: {p.code_background}; }}\n"
        f"pre {{ font-family: {mono}; background: {p.code_background}; padding: 0.6em 0.8em; "
        f"margin: 0 0 {gap} 0; white-space: pre-wrap; }}\n"
        "pre code { background: transparent; }\n"
        f"blockquote {{ margin: 0 0 {gap} 0; padding-left: 0.9em; border-left: 3px solid {p.border_strong}; "
        f"color: {p.quote}; font-style: italic; }}\n"
        f"table {{ border-collapse: collapse; margin: 0 0 {gap} 0; }}\n"
        f"th, td {{ border: 1px solid {p.border}; padding: 0.3em 0.6em; }}\n"
        f"table.quote {{ margin: {gap} 0 {gap} 0; }}\n"
        f"td.quote {{ border: 0; border-left: 3px solid {p.border_strong}; padding: 0 0 0 0.9em; "
        f"color: {p.quote}; font-style: italic; }}\n"
        f"a {{ color: {p.link}; }}\n"
        f"hr {{ border: 0; border-top: 1px solid {p.border_strong}; margin: {gap} 0; }}\n"
        f".image {{ color: {p.text_muted}; }}\n"
    )


def quote_tables(body: str) -> str:
    """Blockquotes as one-cell tables, for the PDF: Qt's layout draws a table cell's left border but not a
    blockquote's."""
    opening = '<table class="quote" cellspacing="0" cellpadding="0"><tr><td class="quote">'
    return body.replace("<blockquote>", opening).replace("</blockquote>", "</td></tr></table>")


def html_page(title: str, body: str, css: str) -> str:
    """A self-contained UTF-8 page that cannot load or run anything (content security policy)."""
    import html as _html

    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
        "style-src 'unsafe-inline'\">\n"
        f"<title>{_html.escape(title)}</title>\n<style>\n{css}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


class ImagePolicy:
    """Which images an export may include, and how: a data URI, or a named resource for the PDF layout."""

    def __init__(self, fs: FileSystem, assets: AssetService, note_path: str, embed: bool) -> None:
        self._fs = fs
        self._assets = assets
        self._note_dir = ntpath.dirname(pathid.normalize(note_path))
        self._folder = ntpath.join(self._note_dir, ASSET_FOLDER)
        self._embed = embed
        self._total = 0
        self.resources: dict[str, bytes] = {}

    def __call__(self, reference: str, _alt: str) -> str | None:
        path = ResourcePolicy(self._note_dir, MAX_IMAGE_BYTES).resolve(reference)
        if path is None or not pathid.is_within(path, self._folder):
            return None
        st = self._fs.stat(path)
        if st is None or needs_hydration(st.attributes) or st.size > MAX_IMAGE_BYTES:
            return None
        if self._total + st.size > MAX_EMBEDDED_BYTES:
            return None
        try:
            data = self._fs.read_bytes(path, MAX_IMAGE_BYTES)
        except OSError as exc:
            logger.info("An image was left out of an export: %s", type(exc).__name__)
            return None
        info, problem = self._assets.check(data)
        if info is None or problem is not None:
            return None
        self._total += len(data)
        if self._embed:
            return f"data:{_MIME[info.kind]};base64,{base64.b64encode(data).decode('ascii')}"
        name = f"pn-image-{len(self.resources) + 1}"
        self.resources[name] = data
        return name


def pdf_bytes(page_html: str, resources: dict[str, bytes], title: str, fill: bool = False) -> bytes:
    """Lay the page out with Qt and print it to PDF in memory. Only the named images are available. With
    ``fill`` the page is drawn in full-page mode, so the page colour reaches the paper's edge; Qt keeps its
    own 2 cm around the text."""
    document = SafeTextDocument(policy=None)
    for name, data in resources.items():
        image = decode_image(data, 40_000_000)
        if image is not None:
            document.addResource(QTextDocument.ResourceType.ImageResource.value, QUrl(name), image)
    document.setHtml(page_html)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setTitle(title)
    writer.setCreator(PRODUCT_NAME)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(20, 20, 20, 20), QPageLayout.Unit.Millimeter)
    if fill:
        layout = writer.pageLayout()
        layout.setMode(QPageLayout.Mode.FullPageMode)
        writer.setPageLayout(layout)
    document.print_(writer)
    del writer
    return bytes(buffer.data().data())


@dataclass(frozen=True, slots=True)
class ExportResult:
    ok: bool
    message: str
    path: str | None = None


class Exporter:
    """Asks where to save, checks the target, renders and writes. One export runs at a time."""

    def __init__(
        self,
        fs: FileSystem,
        assets: AssetService,
        prompter: Prompter,
        executor: IOExecutor,
        current_theme: Callable[[], Theme],
        open_notes: Callable[[], list[str]],
        clipboard: Clipboard = system_clipboard,
    ) -> None:
        self._fs = fs
        self._assets = assets
        self._prompter = prompter
        self._executor = executor
        self._theme = current_theme
        self._open_notes = open_notes
        self._clipboard = clipboard
        self.busy = False

    def copy_markdown(self, text: str) -> str:
        self._clipboard({"text/plain": text})
        return "Copied the note as Markdown"

    def copy_rich(self, note_path: str, text: str) -> str:
        images = ImagePolicy(self._fs, self._assets, note_path, embed=True)
        body = render_html(text, RenderPolicy(link_allowed, images))
        fragment = f"<html><body>\n{body}\n</body></html>"
        self._clipboard({"text/html": fragment, "text/plain": text})
        return "Copied the note as rich text"

    def choose_target(self, note_path: str, fmt: ExportFormat) -> tuple[str | None, Expect, str]:
        """A confirmed target, its write precondition and an empty reason, or a refusal reason.

        The precondition is captured before rendering starts. If another program creates a target that
        was absent in the save dialog, the later atomic write refuses it instead of silently treating it
        as a replacement the user approved.
        """
        stem = ntpath.splitext(ntpath.basename(note_path))[0]
        start = ntpath.join(ntpath.dirname(note_path), stem + fmt.default)
        chosen = self._prompter.choose_save_file(f"Export as {fmt.label}", start, fmt.filter_text)
        if not chosen:
            return None, Expect.ABSENT, "Export cancelled. Nothing was written."
        path = pathid.normalize(chosen)
        extension = ntpath.splitext(path)[1].casefold()
        if not extension:
            path += fmt.default
        elif extension not in fmt.extensions:
            names = " or ".join(fmt.extensions)
            return None, Expect.ABSENT, f"Choose a file name ending in {names}. Nothing was written."
        if pathid.same_path(path, note_path) or any(pathid.same_path(path, p) for p in self._open_notes()):
            return (
                None,
                Expect.ABSENT,
                "That file is an open note. Choose another name; exporting never replaces a note.",
            )
        expected = Expect.ABSENT if self._fs.stat(path) is None else Expect.ANY
        if expected is Expect.ANY and not self._prompter.confirm(
            "Replace file?",
            f"{ntpath.basename(path)} already exists. Replace it with the exported note?",
            "Replace",
            True,
        ):
            return None, Expect.ABSENT, "Export cancelled. Nothing was written."
        return path, expected, ""

    def export(
        self,
        note_path: str,
        text: str,
        fmt: ExportFormat,
        done: Callable[[ExportResult], None],
        note_font: str = "sans",
        view: ShownView | None = None,
        colors: NoteColors | None = None,
    ) -> None:
        """Export ``text`` (a snapshot of the note) and report through ``done`` on the UI thread. The formats
        "as shown" need ``view``, how the note looks in its editor. ``colors`` are the note's own background
        and text colours, which the web page and PDF take on."""
        if self.busy:
            done(ExportResult(False, "Another export is still running."))
            return
        shown = fmt in (ExportFormat.SHOWN_PDF, ExportFormat.SHOWN_PNG, ExportFormat.SHOWN_HTML)
        if shown and view is None:
            done(ExportResult(False, "Open the note in a tab to export it as shown. Nothing was written."))
            return
        path, expected, reason = self.choose_target(note_path, fmt)
        if path is None:
            done(ExportResult(False, reason))
            return
        self.busy = True
        title = ntpath.splitext(ntpath.basename(note_path))[0]
        current = self._theme() if fmt is ExportFormat.HTML else theme(LIGHT)
        if colors is not None:
            current = note_theme(current, *colors)
        fill = colors is not None
        css = export_css(current, note_font, pdf=fmt is ExportFormat.PDF, fill=fill)
        if view is not None and fmt is ExportFormat.SHOWN_HTML:
            css = export_css(view.theme, view.note_font, view=view)
        target = path

        def finish(result: ExportResult) -> None:
            self.busy = False
            done(result)

        if view is not None and fmt in (ExportFormat.SHOWN_PDF, ExportFormat.SHOWN_PNG):
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                data = shown_pdf(text, view) if fmt is ExportFormat.SHOWN_PDF else shown_png(text, view)
            except TooLongForImageError as exc:
                finish(ExportResult(False, str(exc)))
                return
            finally:
                QGuiApplication.restoreOverrideCursor()
            self._write(target, data, expected, finish)
            return

        if fmt is ExportFormat.PDF:
            images = ImagePolicy(self._fs, self._assets, note_path, embed=False)

            def build() -> str:
                body = render_html(text, RenderPolicy(link_allowed, images))
                return html_page(title, quote_tables(body), css)

            def laid_out(outcome: Outcome[str]) -> None:
                if outcome.error is not None or outcome.value is None:
                    finish(self._failed(outcome.error))
                    return
                QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    data = pdf_bytes(outcome.value, images.resources, title, fill)
                finally:
                    QGuiApplication.restoreOverrideCursor()
                self._write(target, data, expected, finish)

            self._executor.submit(build, laid_out, "export")
            return

        heading = ""
        if view is not None and fmt is ExportFormat.SHOWN_HTML and view.title:
            import html as _html

            heading = f'<h1 class="note-title">{_html.escape(view.title)}</h1>\n'

        def render() -> bytes:
            if fmt is ExportFormat.TEXT:
                return render_plain(text).encode("utf-8")
            images = ImagePolicy(self._fs, self._assets, note_path, embed=True)
            body = heading + render_html(text, RenderPolicy(link_allowed, images))
            return html_page(title, body, css).encode("utf-8")

        def rendered(outcome: Outcome[bytes]) -> None:
            if outcome.error is not None or outcome.value is None:
                finish(self._failed(outcome.error))
                return
            self._write(target, outcome.value, expected, finish)

        self._executor.submit(render, rendered, "export")

    def _write(
        self,
        path: str,
        data: bytes,
        expected: Expect,
        finish: Callable[[ExportResult], None],
    ) -> None:
        def write() -> None:
            atomic_save(self._fs, path, data, expected, MAX_EXPORT_BYTES)

        def written(outcome: Outcome[None]) -> None:
            if outcome.error is not None:
                finish(self._failed(outcome.error))
                return
            finish(ExportResult(True, f"Exported to {ntpath.basename(path)}", path))

        self._executor.submit(write, written, "export")

    @staticmethod
    def _failed(error: Exception | None) -> ExportResult:
        name = type(error).__name__ if error is not None else "no result"
        logger.warning("An export failed: %s", name)
        detail = getattr(error, "strerror", None) or "the file could not be written"
        return ExportResult(False, f"Export failed: {detail}. The note was not changed.")
