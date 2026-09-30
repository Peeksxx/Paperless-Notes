"""A text document whose resource loading is fully owned by the resource policy.

``QTextDocument.loadResource`` falls back to reading local files itself (a ``//host/share`` URL is a
UNC path, which means an SMB connection). This override never calls the base implementation.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl
from PySide6.QtGui import QImage, QImageReader, QTextDocument
from PySide6.QtWidgets import QPlainTextDocumentLayout

from paperless_notes.core.fsops import FileSystem, WindowsFileSystem
from paperless_notes.core.security.resources import ResourcePolicy


class SafeTextDocument(QTextDocument):
    def __init__(
        self,
        policy: ResourcePolicy | None = None,
        fs: FileSystem | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._policy = policy
        self._fs = fs or WindowsFileSystem()
        self.requests: deque[str] = deque(maxlen=32)
        self.setDocumentLayout(QPlainTextDocumentLayout(self))

    def set_policy(self, policy: ResourcePolicy | None) -> None:
        self._policy = policy

    def loadResource(self, type: int, name: QUrl | str) -> object:  # noqa: N802 - Qt override
        reference = name.toString() if isinstance(name, QUrl) else str(name)
        self.requests.append(reference)
        if type != QTextDocument.ResourceType.ImageResource.value or self._policy is None:
            return None
        data = self._policy.load(reference, self._fs)
        if data is None:
            return None
        return decode_image(data, self._policy.max_pixels)


def decode_image(data: bytes, max_pixels: int) -> QImage | None:
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > max_pixels:
        return None
    image = reader.read()
    return None if image.isNull() else image
