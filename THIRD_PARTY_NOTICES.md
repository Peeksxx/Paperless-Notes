# Third-party notices

Paperless Notes 4.3.0 for Windows ships the components below. Each keeps its own license; the
Paperless Notes license (`LICENSE`) does not change or limit these terms. The full license texts are in
the `licenses` folder next to `Paperless Notes.exe` and in the source repository.

## Qt for Python: PySide6 Essentials 6.10.0 and shiboken6 6.10.0

- Copyright (C) The Qt Company Ltd. and other contributors.
- Licensed to Paperless Notes under the GNU Lesser General Public License version 3
  (`LGPL-3.0-only`, full text in `licenses/LGPL-3.0.txt`, which builds on the GNU General Public
  License version 3 in `licenses/GPL-3.0.txt`). Qt for Python is also offered under GPL-2.0-only,
  GPL-3.0-only and commercial terms; Paperless Notes uses it under the LGPL.
- Includes the Qt 6.10.0 libraries Qt Core, Qt GUI and Qt Widgets, the Windows platform plugin and the
  GIF, ICO, JPEG and WebP image format plugins, copyright (C) The Qt Company Ltd. and other
  contributors, under the same LGPL terms.
- The libraries are used unmodified, exactly as published on PyPI as `PySide6-Essentials==6.10.0` and
  `shiboken6==6.10.0`.
- They ship as separate files (`Qt6*.dll`, `Qt*.pyd`, `pyside6.abi3.dll`, `shiboken6.abi3.dll` and the
  `plugins` folder under `_internal\PySide6` and `_internal\shiboken6`), so you can replace them with
  other compatible or modified versions.
- Source code:
  - Qt for Python: https://code.qt.io/cgit/pyside/pyside-setup.git/ (tag `v6.10.0`).
  - Qt: https://code.qt.io/cgit/qt/qtbase.git/ (tag `v6.10.0`).
  - Qt Image Formats: https://code.qt.io/cgit/qt/qtimageformats.git/ (tag `v6.10.0`).
  - Release archives: https://download.qt.io/official_releases/QtForPython/ and
    https://download.qt.io/official_releases/qt/6.10/.
- Documentation: https://doc.qt.io/qtforpython.

### Third-party code embedded in Qt

The shipped Qt DLLs and image plugins contain the following code. Copyright notices and complete
license statements are reproduced in `licenses/Qt-Third-Party.txt`. Components under Apache-2.0
also use `licenses/Apache-2.0.txt`.

| Shipped code | License |
| --- | --- |
| Apache Tika MIME definitions; Emoji Segmenter | Apache-2.0 |
| BLAKE2; SHA-3 Keccak; SipHash; `tl::expected` | CC0-1.0 or the listed alternative |
| zlib; the FreeType zlib module | Zlib |
| Robert Penner easing equations; double-conversion; RFC 6234 SHA-384 and SHA-512 | BSD-3-Clause |
| MD4; MD5; SHA-1 | Public domain |
| PCRE2 and its SLJIT compiler | BSD-3-Clause with PCRE2 exception; BSD-2-Clause |
| SHA-3 endian helper | BSD-2-Clause |
| TinyCBOR | MIT |
| Unicode Character Database and Common Locale Data Repository | Unicode-3.0 |
| FreeType 2 and Qt's FreeType rasterizer | FreeType Project License |
| FreeType BDF and PCF drivers | MIT and MIT Open Group variant |
| D3D12 Memory Allocator; Vulkan Memory Allocator; D3D12 MiniEngine mipmap code | MIT |
| HarfBuzz; MD4C; Khronos OpenGL headers; WebGradients | MIT |
| Adobe Glyph List for New Fonts | BSD-3-Clause |
| libpng | libpng and PNG Reference Library version 2 licenses |
| libjpeg-turbo in `qjpeg.dll` | Independent JPEG Group and BSD-3-Clause |
| Qt smooth image scaling code | BSD-2-Clause and Imlib2 |
| Vulkan API Registry | Apache-2.0 or MIT |
| Wintab headers | LCS-Telegraphics |
| X Consortium region code | X11 and Historical Permission Notice and Disclaimer |
| sRGB ICC profile | International Color Consortium license |
| libwebp in `qwebp.dll` | BSD-3-Clause |

The FreeType acknowledgement required by its selected license is: Portions of this software are
copyright 2025 The FreeType Project (www.freetype.org). All rights reserved.

The shipped `qjpeg.dll` identifies its embedded libjpeg-turbo as version 3.0.3. Qt's `v6.10.0`
attribution metadata names a newer codec revision, so this release records the observed binary version
and also links its corresponding upstream source: https://github.com/libjpeg-turbo/libjpeg-turbo/tree/3.0.3.

## Python 3.13.7 runtime

- Copyright (c) 2001 Python Software Foundation; all rights reserved.
- Python Software Foundation License Version 2. The embedded runtime (`python313.dll`,
  `base_library.zip` and the `.pyd` extension modules) also includes bzip2, libffi, SQLite, xz and
  mpdecimal.
- The full text, including the notices for those libraries, is in `licenses/Python-3.13.txt`.
  SQLite (`sqlite3.dll`) is in the public domain.

## Microsoft Visual C++ runtime

`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140.dll`, `MSVCP140_1.dll` and `MSVCP140_2.dll` are
Microsoft Visual C++ redistributable files, distributed with the Python and Qt for Python builds under
Microsoft's redistribution terms for that runtime.

## PyInstaller bootloader

`Paperless Notes.exe` starts through the PyInstaller 6.16.0 bootloader.

- Copyright (c) 2010-2025, PyInstaller Development Team.
- The bootloader is licensed under the GNU General Public License version 2 or later, with an exception
  that allows it to be combined with and distributed as part of any program, which places no
  obligations on that program.
- The PyInstaller run-time hooks compiled into the executable are licensed under the Apache License,
  Version 2.0 (full text in `licenses/Apache-2.0.txt`).
- https://github.com/pyinstaller/pyinstaller/blob/v6.16.0/COPYING.txt

## English word list (spell checking)

The spell checker's word list (`_internal\paperless_notes\data\words.txt.gz`) is built from the English
Speller Database (ESDB, previously SCOWL).

- Copyright 2000-2026 by Kevin Atkinson; Australian English data copyright 2016 by Benjamin Titze.
- Permission to use, copy, modify, distribute and sell the database and word lists created from it, with
  the copyright and permission notices kept; full notice in `licenses/ESDB-Word-List.txt`.
- https://wordlist.aspell.net

## Emoji names (shortcodes)

The `:shortcode:` names (`_internal\paperless_notes\data\emoji.tsv`) come from gemoji.

- Copyright (c) 2019 GitHub, Inc.
- MIT License; full text in `licenses/gemoji-MIT.txt`.
- https://github.com/github/gemoji

## License texts in this release

| File | License |
| --- | --- |
| `licenses/LGPL-3.0.txt` | GNU Lesser General Public License, version 3 |
| `licenses/GPL-3.0.txt` | GNU General Public License, version 3 |
| `licenses/Python-3.13.txt` | Python Software Foundation License Version 2, with bundled-library notices |
| `licenses/Apache-2.0.txt` | Apache License, Version 2.0 (terms and conditions) |
| `licenses/Qt-Third-Party.txt` | License texts and notices for code embedded in the shipped Qt binaries |
| `licenses/ESDB-Word-List.txt` | Copyright and permission notice of the English Speller Database |
| `licenses/gemoji-MIT.txt` | MIT License of the gemoji emoji names |

The GNU and Apache texts are the standard published texts; the Python text is the `LICENSE.txt` of the
Python 3.13.7 Windows distribution used for the build.
