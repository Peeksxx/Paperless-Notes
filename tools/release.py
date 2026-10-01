"""Local release pipeline for Paperless Notes: what the PyInstaller onedir build may contain, the release
manifest, the artifact verifier, the portable zip, checksums and static checks of the installer script.

Nothing here uses the network, installs anything or touches app state. ``tools/build_release.ps1`` runs
the steps in order; the functions are also used by the tests and by the PyInstaller spec.

Usage: python tools/release.py {preflight,clean,stage,verify,portable,sums,check-installer,postflight}
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from paperless_notes.branding import (  # noqa: E402 - the source tree is added to the path above
    COPYRIGHT,
    CREDIT,
    EXE_NAME,
    PRODUCT_NAME,
    VERSION,
    version_tuple,
)

DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / PRODUCT_NAME
RELEASE_DIR = DIST / "release"
MANIFEST_NAME = "release-manifest.json"
INSTALLER_SCRIPT = ROOT / "packaging" / "installer.iss"
REQUIRED_LEGAL = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses/LGPL-3.0.txt",
    "licenses/GPL-3.0.txt",
    "licenses/Python-3.13.txt",
    "licenses/Apache-2.0.txt",
    "licenses/Qt-Third-Party.txt",
    "licenses/ESDB-Word-List.txt",
    "licenses/gemoji-MIT.txt",
)
# Data the spell checker and emoji suggestions read at run time; the spec bundles the whole folder.
REQUIRED_DATA = ("_internal/paperless_notes/data/words.txt.gz", "_internal/paperless_notes/data/emoji.tsv")
LEGAL_FILES = (*REQUIRED_LEGAL, "README.md", "CHANGELOG.md")
WATCHED_TREES = ("src", "tests", "docs", "tools", "packaging", "assets", "licenses")
ZIP_DATE = (2026, 1, 1, 0, 0, 0)
BUILD_PYTHON = "3.13.7"
BUILD_DISTRIBUTIONS = {
    "PyInstaller": "6.16.0",
    "PySide6-Essentials": "6.10.0",
    "shiboken6": "6.10.0",
}

EXCLUDED_QT_MODULES = (
    "PySide6.QtNetwork",
    "PySide6.QtNetworkAuth",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
)
_FORBIDDEN_QT = ("QtNetwork", "QtQml", "QtQuick", "QtWeb", "QtMultimedia", "QtSvg", "QtPdf", "QtOpenGL")
FORBIDDEN_MODULE_PREFIXES = (
    *EXCLUDED_QT_MODULES,
    "PyQt5",
    "PyQt6",
    "PySide2",
    "tkinter",
    "_tkinter",
    "socket",
    "_socket",
    "ssl",
    "_ssl",
    "asyncio",
    "http",
    "urllib.request",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "xmlrpc",
)
BUILD_EXCLUDES = (
    *FORBIDDEN_MODULE_PREFIXES,
    "_hashlib",
    "unittest",
    "pydoc",
    "doctest",
    "lib2to3",
    "idlelib",
    "ensurepip",
    "venv",
    "setuptools",
    "pip",
)
_FORBIDDEN_FILES = re.compile(
    r"(?i)(^|/)("
    r"qt6(network|qml\w*|quick\w*|webengine\w*|webchannel|websockets|multimedia\w*|svg\w*|pdf\w*"
    r"|virtualkeyboard\w*|opengl\w*)\.dll"
    r"|qt(network|qml\w*|quick\w*|webengine\w*|webchannel|websockets|multimedia\w*|svg\w*|pdf\w*"
    r"|opengl\w*)\.pyd"
    r"|qtwebengineprocess\.exe|opengl32sw\.dll|d3dcompiler_47\.dll|_socket\.pyd|_ssl\.pyd|_hashlib\.pyd"
    r"|libssl[-\w]*\.dll|libcrypto[-\w]*\.dll|_tkinter\.pyd|tcl\d*t?\.dll|tk\d*t?\.dll"
    r")$"
)
ALLOWED_QT_PLUGINS = frozenset(
    {
        "platforms/qwindows.dll",
        "imageformats/qgif.dll",
        "imageformats/qico.dll",
        "imageformats/qjpeg.dll",
        "imageformats/qwebp.dll",
    }
)


class ReleaseError(Exception):
    """A release step refused its input; the message says why."""


def build_environment_problems(
    python_version: str = platform.python_version(),
    distribution_version: Callable[[str], str] = importlib.metadata.version,
) -> list[str]:
    """Unexpected build-tool versions that would make the artifact differ from its notices and lock."""
    problems: list[str] = []
    if python_version != BUILD_PYTHON:
        problems.append(f"Python is {python_version}, expected {BUILD_PYTHON}")
    for name, expected in BUILD_DISTRIBUTIONS.items():
        try:
            actual = distribution_version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name} is not installed")
            continue
        if actual != expected:
            problems.append(f"{name} is {actual}, expected {expected}")
    return problems


def qt_plugin(dest: str) -> str | None:
    """``platforms/qwindows.dll`` for a bundled Qt plugin path, else None."""
    posix = dest.replace("\\", "/")
    marker = "PySide6/plugins/"
    at = posix.find(marker)
    return posix[at + len(marker) :] if at >= 0 else None


def allowed_file(dest: str) -> bool:
    """Whether a file the analysis collected belongs in the release: required Qt plugins only, no
    translations, no network, QML, web, multimedia, SVG or PDF module, and no software OpenGL."""
    posix = dest.replace("\\", "/")
    if _FORBIDDEN_FILES.search(posix):
        return False
    if "PySide6/translations/" in posix or "PySide6/qml/" in posix:
        return False
    plugin = qt_plugin(posix)
    return plugin is None or plugin in ALLOWED_QT_PLUGINS


def forbidden_module(name: str) -> bool:
    if name.startswith("PySide6.") and name[len("PySide6.") :].startswith(_FORBIDDEN_QT):
        return True
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)


def version_resource(version: str = VERSION) -> str:
    """PyInstaller version-resource text built from the branding module."""
    numbers = version_tuple(version)
    strings = {
        "CompanyName": "@peeksxx",
        "FileDescription": PRODUCT_NAME,
        "FileVersion": version,
        "InternalName": PRODUCT_NAME,
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": EXE_NAME,
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
        "Comments": CREDIT,
    }
    table = ",\n".join(f"          StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return (
        "VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3F, flags=0x0,\n"
        "    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[\n"
        "    StringFileInfo([StringTable('040904B0', [\n"
        f"{table}])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])]),\n"
        "  ],\n"
        ")\n"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(root: Path) -> list[str]:
    """Every file under ``root`` as a sorted POSIX-style relative path."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def git(*args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ReleaseError("git is required to record which commit a release was built from")
    return subprocess.run([executable, *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def source_state() -> tuple[str, bool]:
    """(commit, dirty) of the repository."""
    commit = git("rev-parse", "HEAD").strip()
    dirty = bool(git("status", "--porcelain").strip())
    return commit, dirty


@dataclass(frozen=True)
class Manifest:
    product: str
    version: str
    exe: str
    commit: str
    dirty: bool
    files: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        data = {
            "product": self.product,
            "version": self.version,
            "exe": self.exe,
            "commit": self.commit,
            "dirty": self.dirty,
            "files": self.files,
        }
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            data = json.loads(text)
            files = data["files"]
            if not isinstance(files, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in files.items()
            ):
                raise ReleaseError("the release manifest has an invalid file list")
            return cls(
                str(data["product"]),
                str(data["version"]),
                str(data["exe"]),
                str(data["commit"]),
                bool(data["dirty"]),
                dict(files),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ReleaseError(f"the release manifest is unreadable: {type(exc).__name__}") from exc


def build_manifest(app_dir: Path, commit: str, dirty: bool, version: str = VERSION) -> Manifest:
    files = {rel: sha256_file(app_dir / rel) for rel in tree_files(app_dir) if rel != MANIFEST_NAME}
    return Manifest(PRODUCT_NAME, version, EXE_NAME, commit, dirty, files)


def stage(app_dir: Path, sources: Path, commit: str, dirty: bool) -> Manifest:
    """Copy the legal and user documents next to the executable and write the manifest."""
    if not (app_dir / EXE_NAME).is_file():
        raise ReleaseError(f"{EXE_NAME} is missing from {app_dir.name}; build first")
    for rel in LEGAL_FILES:
        source = sources / rel
        if not source.is_file():
            raise ReleaseError(f"{rel} is missing from the source tree")
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = build_manifest(app_dir, commit, dirty)
    (app_dir / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8", newline="\n")
    return manifest


def read_manifest(app_dir: Path) -> Manifest:
    path = app_dir / MANIFEST_NAME
    if not path.is_file():
        raise ReleaseError("the build has no release manifest; run the stage step")
    return Manifest.from_json(path.read_text(encoding="utf-8"))


def check_current(app_dir: Path, version: str = VERSION, commit: str | None = None) -> Manifest:
    """Refuse a stale or mixed tree: the manifest must match this version (and commit when given), and
    every file must be listed with its hash, with nothing missing or added."""
    manifest = read_manifest(app_dir)
    if manifest.version != version:
        raise ReleaseError(f"the build is version {manifest.version}, the source is {version}")
    if commit is not None and manifest.commit != commit:
        raise ReleaseError("the build was made from another commit; build again")
    present = {rel for rel in tree_files(app_dir) if rel != MANIFEST_NAME}
    listed = set(manifest.files)
    if present - listed:
        raise ReleaseError(f"files not in the manifest: {', '.join(sorted(present - listed)[:5])}")
    if listed - present:
        raise ReleaseError(f"files missing from the build: {', '.join(sorted(listed - present)[:5])}")
    changed = [rel for rel in sorted(listed) if sha256_file(app_dir / rel) != manifest.files[rel]]
    if changed:
        raise ReleaseError(f"files changed after the build: {', '.join(changed[:5])}")
    return manifest


def read_file_version(path: Path) -> tuple[str, str] | None:
    """(ProductVersion, ProductName) from a Windows version resource, or None."""
    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    values: list[str] = []
    for key in ("ProductVersion", "ProductName"):
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        query = f"\\StringFileInfo\\040904B0\\{key}"
        if not version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)):
            return None
        values.append(ctypes.wstring_at(pointer, length.value).rstrip("\x00"))
    return values[0], values[1]


def bundled_modules(exe: Path) -> list[str] | None:
    """Module names in the executable's PYZ archive, or None when PyInstaller is unavailable."""
    try:
        from PyInstaller.archive.readers import pkg_archive_contents  # type: ignore[import-untyped]
    except ImportError:
        return None
    return [str(name) for name in pkg_archive_contents(str(exe))]


@dataclass
class Verification:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(
    app_dir: Path,
    version: str = VERSION,
    file_version: Callable[[Path], tuple[str, str] | None] = read_file_version,
    modules: Callable[[Path], list[str] | None] = bundled_modules,
) -> Verification:
    """Check the onedir tree: executable, legal files, version metadata, directory shape, forbidden
    files and (when PyInstaller is available) forbidden bundled modules, plus the manifest."""
    result = Verification()
    exe = app_dir / EXE_NAME
    if not exe.is_file():
        result.problems.append(f"{EXE_NAME} is missing")
        return result
    for rel in REQUIRED_LEGAL:
        if not (app_dir / rel).is_file():
            result.problems.append(f"required legal file missing: {rel}")
    for rel in REQUIRED_DATA:
        if not (app_dir / rel).is_file():
            result.problems.append(f"bundled data missing: {rel}")
    internal = app_dir / "_internal"
    if not internal.is_dir():
        result.problems.append("the _internal folder is missing (not a onedir build)")
    top = {p.name for p in app_dir.iterdir()}
    unexpected = top - {EXE_NAME, "_internal", "licenses", MANIFEST_NAME, *LEGAL_FILES}
    if unexpected:
        result.problems.append(f"unexpected top-level entries: {', '.join(sorted(unexpected))}")
    if (app_dir / "portable.txt").exists():
        result.problems.append("portable.txt must not ship: notes and settings stay in %LOCALAPPDATA%")
    files = tree_files(app_dir)
    for rel in files:
        if not allowed_file(rel):
            result.problems.append(f"forbidden file bundled: {rel}")
    plugins = {p for rel in files if (p := qt_plugin(rel)) is not None}
    if "platforms/qwindows.dll" not in plugins:
        result.problems.append("the Windows platform plugin is missing")
    metadata = file_version(exe)
    if metadata is None:
        result.problems.append("the executable has no readable version resource")
    elif metadata != (version, PRODUCT_NAME):
        result.problems.append(f"version resource says {metadata}, expected {(version, PRODUCT_NAME)}")
    names = modules(exe)
    if names is None:
        result.notes.append("PyInstaller is unavailable: bundled Python modules were not inspected")
    else:
        bad = sorted({n for n in names if forbidden_module(n)})
        if bad:
            result.problems.append(f"forbidden modules bundled: {', '.join(bad[:10])}")
        if "paperless_notes.app" not in names:
            result.problems.append("the application package is not in the bundle")
    try:
        check_current(app_dir, version)
    except ReleaseError as exc:
        result.problems.append(str(exc))
    return result


def portable_name(version: str = VERSION) -> str:
    return f"Paperless-Notes-{version}-portable-win64.zip"


def installer_name(version: str = VERSION) -> str:
    return f"Paperless-Notes-{version}-setup.exe"


def build_portable(app_dir: Path, out_dir: Path, version: str = VERSION, commit: str | None = None) -> Path:
    """A zip of the verified onedir tree inside one ``Paperless Notes <version>`` folder, with fixed
    timestamps and sorted entries so the same tree always gives the same bytes."""
    manifest = check_current(app_dir, version, commit)
    if manifest.dirty:
        raise ReleaseError("the build was made from uncommitted changes; commit and build again")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / portable_name(version)
    partial = target.with_suffix(".zip.partial")
    prefix = PurePosixPath(f"{PRODUCT_NAME} {version}")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in tree_files(app_dir):
            info = zipfile.ZipInfo(str(prefix / rel), ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (app_dir / rel).read_bytes())
    partial.replace(target)
    return target


def write_sums(out_dir: Path, version: str = VERSION) -> Path:
    """SHA256SUMS.txt for the portable zip and, when present, the installer of the same version.
    Any other version's artifact in the folder is refused as mixed input."""
    names = {portable_name(version), installer_name(version)}
    others = [
        p.name
        for p in out_dir.iterdir()
        if p.is_file()
        and p.name != "SHA256SUMS.txt"
        and p.name not in names
        and not p.name.endswith(".partial")
    ]
    if others:
        raise ReleaseError(f"other files in the release folder: {', '.join(sorted(others))}")
    present = [out_dir / n for n in sorted(names) if (out_dir / n).is_file()]
    if not (out_dir / portable_name(version)).is_file():
        raise ReleaseError("the portable zip is missing")
    lines = [f"{sha256_file(p)} *{p.name}\n" for p in present]
    target = out_dir / "SHA256SUMS.txt"
    target.write_text("".join(lines), encoding="utf-8", newline="\n")
    return target


def iss_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].casefold()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def iss_setup(sections: dict[str, list[str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in sections.get("setup", []):
        key, _, value = line.partition("=")
        values[key.strip().casefold()] = value.strip()
    return values


def check_installer_script(text: str) -> list[str]:
    """Static ownership and safety checks of the Inno Setup script; returns problems (empty is good)."""
    problems: list[str] = []
    sections = iss_sections(text)
    setup = iss_setup(sections)
    expect = {
        "privilegesrequired": "lowest",
        "defaultdirname": "{localappdata}\\Programs\\Paperless Notes",
        "changesassociations": "yes",
    }
    for key, value in expect.items():
        if setup.get(key, "").casefold() != value.casefold():
            problems.append(f"[Setup] {key} must be {value}")
    if "privilegesrequiredoverridesallowed" in setup:
        problems.append("[Setup] must not offer an administrator install")
    lowered = text.casefold()
    for banned, why in (
        ("{localappdata}\\paperless notes", "the installer must never touch app state"),
        ("{userappdata}", "the installer must not use roaming app data"),
        ("http://", "no network addresses in the installer"),
        ("downloadtemporaryfile", "no downloads"),
        ("idp.iss", "no download plugin"),
        ("cmd.exe", "no shell commands"),
        ("powershell", "no shell commands"),
        ("exec(", "no programs started from the installer code"),
    ):
        if banned in lowered:
            problems.append(why)
    for line in sections.get("uninstalldelete", []) + sections.get("installdelete", []):
        name = _param(line, "Name")
        if name is None or not name.casefold().startswith("{app}\\"):
            problems.append(f"deletes outside the install folder: {line}")
    for line in sections.get("registry", []):
        root = (_param(line, "Root") or "").casefold()
        subkey = (_param(line, "Subkey") or "").casefold()
        if root not in ("hkcu", "hka"):
            problems.append(f"registry entry outside the current user: {line}")
        if subkey.endswith("microsoft\\windows\\currentversion\\run"):
            problems.append(f"startup stays with the app's own Run entry: {line}")
        extension_key = subkey in (
            "software\\classes\\.md",
            "software\\classes\\.markdown",
            "software\\classes\\.txt",
        )
        if extension_key and _param(line, "ValueName") in (None, ""):
            problems.append(f"must not take the default opener of a file type: {line}")
        if (_param(line, "Tasks") or "") != "associate" and "classes" in subkey:
            problems.append(f"file-type registration must belong to the optional task: {line}")
    tasks = sections.get("tasks", [])
    for line in tasks:
        if "unchecked" not in (_param(line, "Flags") or "").casefold():
            problems.append(f"optional tasks must be unchecked by default: {line}")
    for line in sections.get("run", []):
        filename = _param(line, "Filename") or ""
        if filename not in ("{app}\\Paperless Notes.exe", "{app}\\{#AppExe}"):
            problems.append(f"the installer may only start the installed app: {line}")
    return problems


def _param(line: str, name: str) -> str | None:
    """The value of ``Name: value`` in an Inno Setup entry line (quotes removed), or None."""
    for part in _split_params(line):
        key, sep, value = part.partition(":")
        if sep and key.strip().casefold() == name.casefold():
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('""', '"')
            return value
    return None


def _split_params(line: str) -> Iterable[str]:
    part: list[str] = []
    quoted = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            yield "".join(part)
            part = []
        else:
            part.append(ch)
    if part:
        yield "".join(part)


def watched_state() -> str:
    """Tracked and ignored changes under the source trees, to prove a build modified nothing there."""
    return git("status", "--porcelain=v1", "--ignored", "--", *WATCHED_TREES)


def clean(targets: Iterable[Path]) -> None:
    """Remove earlier build output; only folders inside ``build`` or ``dist`` of this repository."""
    for target in targets:
        resolved = target.resolve()
        if not any(resolved == base or base in resolved.parents for base in (BUILD, DIST)):
            raise ReleaseError(f"refusing to remove {target}: not build output")
        if resolved.exists():
            shutil.rmtree(resolved)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step",
        choices=[
            "preflight",
            "clean",
            "stage",
            "verify",
            "portable",
            "sums",
            "check-installer",
            "postflight",
        ],
    )
    parser.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes (no zip)")
    parser.add_argument("--state-file", type=Path, default=BUILD / "source-state.txt")
    args = parser.parse_args(argv[1:])
    try:
        return _run(args.step, args.allow_dirty, args.state_file)
    except ReleaseError as exc:
        print(f"release {args.step}: {exc}")
        return 1


def _run(step: str, allow_dirty: bool, state_file: Path) -> int:
    if step == "preflight":
        version_tuple(VERSION)
        problems = build_environment_problems()
        if problems:
            raise ReleaseError("; ".join(problems))
        commit, dirty = source_state()
        if dirty and not allow_dirty:
            raise ReleaseError("the worktree has uncommitted changes; commit them or pass -AllowDirty")
        BUILD.mkdir(exist_ok=True)
        state_file.write_text(watched_state(), encoding="utf-8", newline="\n")
        print(f"release preflight: version {VERSION}, commit {commit[:12]}{' (dirty)' if dirty else ''}")
    elif step == "clean":
        clean([BUILD / "pyinstaller", APP_DIR, RELEASE_DIR])
    elif step == "stage":
        commit, dirty = source_state()
        manifest = stage(APP_DIR, ROOT, commit, dirty)
        print(f"release stage: {len(manifest.files)} files listed in {MANIFEST_NAME}")
    elif step == "verify":
        result = verify(APP_DIR)
        for note in result.notes:
            print(f"note: {note}")
        for problem in result.problems:
            print(f"problem: {problem}")
        print(f"release verify: {'passed' if result.ok else 'FAILED'}")
        return 0 if result.ok else 1
    elif step == "portable":
        commit, _dirty = source_state()
        print(f"release portable: {build_portable(APP_DIR, RELEASE_DIR, VERSION, commit).name}")
    elif step == "sums":
        print(f"release sums: {write_sums(RELEASE_DIR).read_text(encoding='utf-8').strip()}")
    elif step == "check-installer":
        problems = check_installer_script(INSTALLER_SCRIPT.read_text(encoding="utf-8"))
        for problem in problems:
            print(f"problem: {problem}")
        print(f"release check-installer: {'passed' if not problems else 'FAILED'}")
        return 0 if not problems else 1
    elif step == "postflight":
        if not state_file.is_file():
            raise ReleaseError("no preflight state recorded")
        if watched_state() != state_file.read_text(encoding="utf-8"):
            raise ReleaseError("the build changed files under the source trees")
        print("release postflight: source trees unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
