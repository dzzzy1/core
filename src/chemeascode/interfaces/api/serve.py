"""``cheme serve``: a read-only JSON API over a plant repository, for the viewer.

Every endpoint reads; nothing writes to the repository. The one exception to "only reads" is
``POST /api/checks/run``: it evaluates the gates without writing ``checks/status.json``, but a
``kind: command`` check runs a shell command from ``checks/checks.yaml``, which can do anything.
That endpoint therefore needs the per-launch token from ``GET /api/session`` in the
``X-Cheme-Token`` header, runs one evaluation at a time, and stops each command after
:data:`CHECK_TIMEOUT` seconds. A ``rev`` query parameter selects a git revision; without
one the working tree is read. Files at a revision come from git's object store (``git ls-tree``,
``git cat-file``), never from a checkout.

Security, since the API serves files from disk:

- every ``path`` is repo-relative POSIX, may not contain ``..``, ``.`` or empty segments, may not
  be absolute, may not name ``.git`` or anything under it, and in the working tree must resolve
  (symlinks followed) inside the repository and outside ``.git``;
- every ``rev`` is refused when it starts with ``-`` and is resolved with
  ``git rev-parse --verify --end-of-options <rev>^{commit}``; only the resulting sha is passed on;
- git runs with argument lists, never a shell;
- the server binds 127.0.0.1 by default and accepts only requests whose ``Host`` header names the
  address it serves (against DNS rebinding): the local names, a bound address that is not a
  wildcard, and the names given with ``--allow-host``, never any name (:func:`accepted_hosts`);
- bound to a non-local address, the server refuses ``GET /api/session`` and ``POST
  /api/checks/run`` unless started with ``--allow-remote-checks``;
- the viewer's pages may be framed only by the served origin (``frame-ancestors 'self'``), so
  another site cannot trick a click on "Run now" in a frame;
- ``/api/*`` refuses requests another site makes (``Sec-Fetch-Site: cross-site`` or
  ``same-site``, or an ``Origin`` that is not the served origin), so a web page the user visits
  cannot drive the API;
- files larger than :data:`MAX_FILE_BYTES` are not parsed, and YAML is converted to JSON with a
  node budget (aliases cannot blow it up) and only JSON types. The working-tree reports
  (``/api/validate``, ``/api/status``, ``/api/impact``, ``/api/control``, ``/api/maturity``) run
  the command modules on the repository as ``cheme validate`` and the others do, without that
  cap;
- an unexpected failure answers JSON ``{"error": "internal error"}`` with no internals.

Rendering a sheet takes a fraction of a second, so SVGs and diffs are cached in memory by the git
blob ids of their inputs.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from lxml import etree
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from chemeascode.domains.chemistry.catalysts import CATALYSTS_FILE, Catalysts
from chemeascode.domains.chemistry.reactions import REACTIONS_FILE, Reactions
from chemeascode.domains.piping.diffview import _represented_by, diff_sheet
from chemeascode.domains.piping.render import load_model, render_svg
from chemeascode.domains.piping.show import (
    Sheet,
    _ancestor_or_self,
    _equipment_of,
    _functions_of,
    _is_nozzle,
    _loops,
    _object_children,
    _piping_index,
    _references,
    _referrers,
    _targets,
    data_value,
    element_spans,
    is_equipment,
    short_class,
)
from chemeascode.domains.process import FLOWSHEET_FILE, MATERIALS_FILE
from chemeascode.domains.process.schema import Flowsheet, Materials
from chemeascode.domains.spatial.layout_validate import (
    building_footprint,
    building_heights,
    check_layout,
    level_heights,
)
from chemeascode.foundation.workspace import (
    LAYOUT_FILES,
    LAYOUT_MODEL_FILE,
    LIST_FILES,
    PLANT_FILE,
    Workspace,
)
from chemeascode.project.checks import run_checks
from chemeascode.project.kinetics import DATA_DIR as KINETICS_DIR
from chemeascode.project.validate import validate_workspace
from chemeascode.schemas import (
    LAYOUT_SCHEMAS,
    LIST_SCHEMAS,
    EquipmentList,
    LayoutModelFile,
    PlantConfig,
)

#: Largest file the API reads (returns, parses or renders).
MAX_FILE_BYTES = 5 * 1024 * 1024
#: Most YAML nodes ``/api/yaml`` returns, counted after aliases are expanded.
MAX_YAML_NODES = 200_000
#: Seconds a ``kind: command`` check may run under ``POST /api/checks/run``.
CHECK_TIMEOUT = 120.0
#: Header carrying the per-launch token of ``GET /api/session``.
TOKEN_HEADER = "X-Cheme-Token"
#: Directories left out of ``/api/tree`` at the repository root (``.git`` is left out anywhere).
EXCLUDED_TOP_DIRS = {"build", ".venv"}
#: Host names accepted by default (the server binds 127.0.0.1 by default).
LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1"]
WORKING_TREE = "working tree"

_KIND_BY_LIST_PATH = {path: kind for kind, path in LIST_FILES.items()}
_KIND_BY_LAYOUT_PATH = {path: kind for kind, path in LAYOUT_FILES.items()}
_PROCESS_SCHEMAS = {
    MATERIALS_FILE: ("materials", Materials),
    FLOWSHEET_FILE: ("flowsheet", Flowsheet),
    REACTIONS_FILE: ("reactions", Reactions),
    CATALYSTS_FILE: ("catalysts", Catalysts),
}
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}
#: The viewer's pages: framed only by the served origin (against clickjacking "Run now").
_UI_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN",
}


class ApiError(Exception):
    """An error answered as JSON ``{"error": message}`` with *status*."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Paths and revisions
# ---------------------------------------------------------------------------


def clean_path(path: str) -> str:
    """*path* when it is a safe repo-relative POSIX path, else :class:`ApiError` 400."""
    if not path or "\0" in path or "\\" in path:
        raise ApiError(400, f"invalid path {path!r}")
    if path.startswith("/") or PurePosixPath(path).is_absolute():
        raise ApiError(400, f"path must be relative to the repository: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ApiError(400, f"path may not contain empty, '.' or '..' segments: {path!r}")
    if any(part.lower() == ".git" for part in parts):
        raise ApiError(400, "paths under .git are not served")
    return path


def blob_id(content: bytes) -> str:
    """The git blob id of *content* (what ``git hash-object`` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One file or directory of a snapshot."""

    path: str
    type: str  # file, dir
    blob: str | None = None  # git blob id, at a revision
    mode: str = ""


class Repo:
    """The plant repository and the git repository around it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.workspace = Workspace(self.root)
        prefix = self._git("rev-parse", "--show-prefix")
        self.is_git = prefix.returncode == 0
        #: The plant root relative to the git top level ("" or "sub/dir/").
        self.prefix = prefix.stdout.decode().strip() if self.is_git else ""
        self._trees = _LRU(32)
        #: When a set, the path of every file a snapshot reads the content of is added to it:
        #: ``cheme export-static`` withholds the answers built from an excluded file.
        self.reads: set[str] | None = None

    def _git(self, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=self.root,
            capture_output=True,
            check=False,
            env=env,
            input=input,
        )

    def git(self, *args: str) -> bytes:
        """Run git; :class:`ApiError` 500 when it fails."""
        result = self._git(*args)
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip().splitlines()[:1]
            raise ApiError(500, f"git {args[0]} failed: {message[0] if message else ''}")
        return result.stdout

    # -- revisions ----------------------------------------------------------

    def resolve(self, rev: str) -> str:
        """The commit sha *rev* names; 400 when it is malformed, 404 when unknown."""
        if not rev or rev.startswith("-") or len(rev) > 256 or any(c in rev for c in "\0\r\n"):
            raise ApiError(400, f"invalid revision {rev!r}")
        if not self.is_git:
            raise ApiError(400, "the plant repository is not in git")
        result = self._git(
            "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}"
        )
        if result.returncode != 0:
            raise ApiError(404, f"unknown revision {rev!r}")
        return result.stdout.decode().strip()

    def head(self) -> str | None:
        """The commit HEAD points at, or ``None`` (no git, or no commit yet)."""
        if not self.is_git:
            return None
        result = self._git("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def snapshot(self, rev: str | None) -> Snapshot:
        """The working tree (``rev`` absent) or the tree of commit *rev*."""
        return Snapshot(self, self.resolve(rev) if rev else None)

    # -- trees at a commit ----------------------------------------------------

    def tree(self, commit: str) -> dict[str, Entry]:
        """Every entry under the plant root at *commit*, by repo-relative path."""

        def load() -> dict[str, Entry]:
            raw = self.git("ls-tree", "-r", "-t", "-z", "--full-tree", commit)
            entries: dict[str, Entry] = {}
            for record in raw.split(b"\0"):
                if not record:
                    continue
                meta, _, name = record.partition(b"\t")
                mode, kind, sha = meta.decode().split()
                full = name.decode("utf-8", errors="surrogateescape")
                if not full.startswith(self.prefix) or full == self.prefix.rstrip("/"):
                    continue
                path = full[len(self.prefix) :]
                if kind == "tree":
                    entries[path] = Entry(path, "dir", None, mode)
                elif kind == "blob":
                    entries[path] = Entry(path, "file", sha, mode)
            return entries

        return self._trees.get_or_compute(commit, load)

    def cat_blob(self, sha: str) -> bytes:
        return self.git("cat-file", "blob", sha)


class Snapshot:
    """The files of the repository at one commit, or in the working tree (``commit`` None)."""

    def __init__(self, repo: Repo, commit: str | None) -> None:
        self.repo = repo
        self.commit = commit

    @property
    def label(self) -> str:
        return self.commit or WORKING_TREE

    def _working_path(self, path: str) -> Path:
        """*path* in the working tree, resolved, after the confinement checks."""
        resolved = (self.repo.root / clean_path(path)).resolve()
        try:
            relative = resolved.relative_to(self.repo.root)
        except ValueError:
            raise ApiError(400, f"{path} resolves outside the repository") from None
        if any(part.lower() == ".git" for part in relative.parts):
            raise ApiError(400, "paths under .git are not served")
        return resolved

    def _entry(self, path: str) -> Entry | None:
        """The tree entry of a file at the commit, ``None`` when absent; 400 for a non-file."""
        entry = self.repo.tree(self.commit or "").get(clean_path(path))
        if entry is not None and (entry.type != "file" or entry.mode in ("120000", "160000")):
            raise ApiError(400, f"{path} is not a file at this revision")
        return entry

    def _working_file(self, path: str) -> Path | None:
        resolved = self._working_path(path)
        if not resolved.exists():
            return None
        if not resolved.is_file():
            raise ApiError(400, f"{path} is not a file")
        return resolved

    def size(self, path: str) -> int | None:
        """The size of file *path* in bytes, ``None`` when absent."""
        if self.commit is None:
            found = self._working_file(path)
            return found.stat().st_size if found else None
        entry = self._entry(path)
        if entry is None:
            return None
        return int(self.repo.git("cat-file", "-s", entry.blob or "").decode().strip())

    def read(self, path: str) -> bytes | None:
        """The bytes of *path*, or ``None`` when it does not exist."""
        found = self.read_with_id(path)
        return found[0] if found else None

    def raw_fingerprint(self, path: str) -> str | None:
        """Hash a declared native asset without parsing it or exposing it in API payloads."""
        if self.commit is None:
            found = self._working_file(path)
            if found is None:
                return None
            with found.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            entry = self._entry(path)
            if entry is None or entry.blob is None:
                return None
            digest = hashlib.sha256(self.repo.cat_blob(entry.blob)).hexdigest()
        if self.repo.reads is not None:
            self.repo.reads.add(clean_path(path))
        return "sha256:" + digest

    def read_with_id(self, path: str) -> tuple[bytes, str] | None:
        """The bytes of *path* and their git blob id, or ``None`` when it does not exist."""
        if self.commit is None:
            found = self._working_file(path)
            if found is None:
                return None
            content = found.read_bytes()
            sha = blob_id(content)
        else:
            entry = self._entry(path)
            if entry is None or entry.blob is None:
                return None
            content, sha = self.repo.cat_blob(entry.blob), entry.blob
        if self.repo.reads is not None:
            self.repo.reads.add(clean_path(path))
        return content, sha

    def entries(self) -> list[Entry]:
        """Every file and directory, sorted, without ``.git``, ``build/`` and ``.venv``."""
        if self.commit is not None:
            found = [
                entry
                for entry in self.repo.tree(self.commit).values()
                if entry.mode not in ("120000", "160000")
            ]
        else:
            found = self._walk()
        return sorted(
            (e for e in found if not _excluded(e.path)),
            key=lambda e: e.path,
        )

    def _walk(self) -> list[Entry]:
        root = self.repo.root
        found: list[Entry] = []
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(directory)
            keep = []
            for name in sorted(dirnames):
                relative = (here / name).relative_to(root).as_posix()
                if _excluded(relative) or (here / name).is_symlink():
                    continue
                keep.append(name)
                found.append(Entry(relative, "dir"))
            dirnames[:] = keep
            for name in filenames:
                relative = (here / name).relative_to(root).as_posix()
                if _excluded(relative):
                    continue
                if (here / name).is_symlink():
                    try:
                        if not self._working_path(relative).is_file():
                            continue
                    except ApiError:
                        continue  # escapes the repository
                found.append(Entry(relative, "file"))
        return found

    # -- the plant -------------------------------------------------------------

    def config(self) -> PlantConfig:
        """``plant.yaml`` of this snapshot, validated (422 when it does not validate)."""
        content = self.read(PLANT_FILE)
        if content is None:
            raise ApiError(404, f"no {PLANT_FILE} at {self.label}")
        try:
            return PlantConfig.model_validate(yaml.safe_load(content) or {})
        except (ValidationError, yaml.YAMLError) as failure:
            raise ApiError(422, f"{PLANT_FILE}: {str(failure).splitlines()[0]}") from failure

    def sheets(self, config: PlantConfig) -> list[dict[str, str]]:
        """The P&ID sheets, as ``Workspace.pid_files`` finds them: listed, else ``pid/*.xml``."""
        if config.sheets:
            return [sheet.model_dump() for sheet in config.sheets]
        return [
            {"id": PurePosixPath(e.path).stem, "file": e.path, "title": "", "kind": "pid"}
            for e in self.entries()
            if e.type == "file" and PurePosixPath(e.path).parent.as_posix() == "pid"
            if e.path.endswith(".xml")
        ]


def _excluded(path: str) -> bool:
    parts = path.split("/")
    return parts[0] in EXCLUDED_TOP_DIRS or any(part.lower() == ".git" for part in parts)


def file_kind(path: str, sheet_files: set[str]) -> str:
    """``pid``, ``yaml``, ``markdown``, ``json`` or ``other``."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".xml" and (path in sheet_files or path.startswith("pid/")):
        return "pid"
    return {
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
    }.get(suffix, "other")


def json_safe(data: Any, budget: int = MAX_YAML_NODES) -> Any:
    """*data* (from ``yaml.safe_load``) as plain JSON values, every alias expanded.

    :class:`ApiError` 422 when it holds more than *budget* nodes (an alias bomb), refers to
    itself, or holds a value JSON has no type for (``!!binary``, ``!!set``).
    """
    remaining = budget
    active: set[int] = set()

    def convert(value: Any) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise ApiError(422, f"the YAML expands to more than {budget} values")
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        if isinstance(value, (dict, list)):
            if id(value) in active:
                raise ApiError(422, "the YAML refers to itself (a recursive alias)")
            active.add(id(value))
            try:
                if isinstance(value, list):
                    return [convert(item) for item in value]
                converted = {}
                for key, item in value.items():
                    if not isinstance(key, (str, int, float, bool)) and key is not None:
                        raise ApiError(422, f"a YAML mapping key of type {type(key).__name__}")
                    converted[key if isinstance(key, str) else json.dumps(key)] = convert(item)
                return converted
            finally:
                active.discard(id(value))
        raise ApiError(422, f"a YAML value of type {type(value).__name__} has no JSON form")

    return convert(data)


def _first_line(failure: Exception) -> str:
    return (str(failure).splitlines() or [type(failure).__name__])[0]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _LRU:
    """A small thread-safe least-recently-used cache."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._items: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        value = compute()  # outside the lock: rendering is slow, a duplicate is harmless
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return value

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def _host_name(header: str) -> str:
    """The host part of a ``Host`` header: ``[::1]:8765`` gives ``::1``, ``a:80`` gives ``a``."""
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _cross_site(request: Request, host: str) -> str | None:
    """Why a request made by another web site is refused, ``None`` when it is not one."""
    site = request.headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):
        return f"cross-site request refused (Sec-Fetch-Site: {site})"
    origin = request.headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        if origin == "null" or not parts.netloc or parts.netloc.lower() != host.lower():
            return f"cross-origin request refused (Origin: {origin})"
    return None


#: The pyDEXPI modules cheme-as-code imports where it uses them. :func:`create_app` imports them
#: all before the server answers requests in its thread pool: pyDEXPI's first import is not safe
#: from several threads at once (one of them sees a partially initialised module), and the P&ID a
#: concurrent request was drawing then answered 422 although it loads.
PYDEXPI_MODULES = (
    "pydexpi",
    "pydexpi.diff",
    "pydexpi.exports.svg_export",
    "pydexpi.loaders.compat",
    "pydexpi.loaders.error_registry",
    "pydexpi.loaders.public",
    "pydexpi.loaders.xml_serializer",
    "pydexpi.pydexpi_classes.core.diagram",
    "pydexpi.pydexpi_classes.plant",
    "pydexpi.pydexpi_classes.pydexpi_classes",
    "pydexpi.toolkits.base_model_utils",
    "pydexpi.toolkits.display_name",
    "pydexpi.toolkits.label_toolkit",
    "pydexpi.toolkits.model_summary",
    "pydexpi.toolkits.model_toolkit",
    "pydexpi.validation",
    "pydexpi.versioning",
    "pydexpi.versioning.projection",
)


def import_pydexpi() -> None:
    """Import :data:`PYDEXPI_MODULES` in this thread."""
    import importlib

    for name in PYDEXPI_MODULES:
        importlib.import_module(name)


# ---------------------------------------------------------------------------
# P&ID object inspection (GET /api/pid/object)
# ---------------------------------------------------------------------------

_DN_PROPS = ("NominalDiameterRepresentation", "NominalDiameterNumericalValueRepresentation")
_CLASS_CODE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z])(\d*)$")
_FAIL_POS_RE = re.compile(r"\b(FC|FO|FL|FCI|FOI)\b")


def _sheet_from_bytes(content: bytes, name: str) -> Sheet:
    """Parse DEXPI XML bytes into a :class:`Sheet` without touching disk."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as failure:
        raise ApiError(422, f"{name}: not well-formed XML: {_first_line(failure)}") from failure
    elements = [e for e in root.iter() if isinstance(e.tag, str)]
    spans = element_spans(content)
    if len(spans) != len(elements):
        spans = [(e.sourceline or 0, e.sourceline or 0) for e in elements]
    sheet = Sheet(name, root, dict(zip(elements, spans, strict=True)))
    for element in elements:
        if element.tag == "Object" and element.get("id"):
            sheet.by_id.setdefault(element.get("id"), element)
        if element.tag == "References":
            holder = element.getparent()
            prop = element.get("property", "")
            for target in element.get("objects", "").split():
                target = target.lstrip("#")
                if prop == "Represents":
                    sheet.drawings.setdefault(target, []).append(holder)
                else:
                    sheet.incoming.setdefault(target, []).append((element, prop))
    sheet.owner = _represented_by(root)[1]
    return sheet


def _clean_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    text = tag.strip()
    if not text or text.startswith("A$"):
        return None
    return text


def _norm_tag(text: str) -> str:
    return re.sub(r"[\s\-_]", "", text).upper()


def _derive_piping_class(code: str | None) -> tuple[str | None, str | None]:
    """Derive ``(nominal_pressure, material_group)`` from a piping class code.

    Example: ``1.6A3`` -> ``("PN16", "PN16 / A系")``, ``2.5K2`` -> ``("PN25", "PN25 / K系")``.
    """
    if not code:
        return None, None
    text = code.strip()
    m = _CLASS_CODE_RE.match(text)
    if m:
        mpa = float(m.group(1))
        pn = mpa * 10.0
        pn_str = f"PN{int(pn)}" if pn.is_integer() else f"PN{pn:g}"
        letter = m.group(2).upper()
        return pn_str, f"{pn_str} / {letter}系"
    m_pn = re.match(r"^(PN\s*\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    if m_pn:
        pn_str = m_pn.group(1).upper().replace(" ", "")
        return pn_str, pn_str
    return None, None


_MATERIAL_SERIES_RANK = {"A": 1, "B": 2, "C": 3, "D": 3, "E": 4, "K": 5, "L": 5, "S": 5}


def _piping_class_rank(code: str) -> tuple[float, int]:
    """Rank piping class codes by nominal pressure (MPa), breaking ties by corrosion series (K > A)."""
    m = _CLASS_CODE_RE.match(code.strip())
    if not m:
        return (0.0, 0)
    return (float(m.group(1)), _MATERIAL_SERIES_RANK.get(m.group(2).upper(), 0))


def _lookup_pipe_spec_material(lists: dict[str, dict[str, Any]] | None, piping_class: str | None) -> str | None:
    """Resolve non-empty material text for ``piping_class`` from ``piping/pipe-specs.yaml``."""
    if not lists or not piping_class:
        return None
    target = piping_class.strip().upper()
    classes = (lists.get("pipe_specs") or {}).get("classes") or []
    for entry in classes:
        if isinstance(entry, dict) and str(entry.get("code") or "").strip().upper() == target:
            mat = entry.get("material")
            if isinstance(mat, str) and mat.strip():
                return mat.strip()
    return None


def _extract_raw_dn(element: etree._Element) -> str | None:
    for prop in _DN_PROPS:
        val = data_value(element, prop)
        if val:
            return val
    node_dns: list[str] = []
    for node in _object_children(element, "Nodes"):
        for prop in _DN_PROPS:
            val = data_value(node, prop)
            if val:
                if val not in node_dns:
                    node_dns.append(val)
                break
    if not node_dns:
        return None
    if len(node_dns) == 1:
        return node_dns[0]
    return "×".join(node_dns)


def _format_dn(raw: str | None) -> str | None:
    if not raw:
        return None
    parts = [p.strip() for p in re.split(r"[×xX/]", raw) if p.strip()]
    if not parts:
        return None
    formatted = [p if p.upper().startswith("DN") else f"DN{p}" for p in parts]
    return "×".join(formatted)


def _resolve_pid_element(sheet: Sheet, obj_id: str) -> etree._Element:
    """Resolve an XML object id or RepresentationGroup/label id to its conceptual element."""
    element = sheet.by_id.get(obj_id)
    if element is None:
        found = sheet.find(obj_id)
        if not found:
            raise ApiError(404, f"object {obj_id!r} not found in {sheet.name}")
        element = found[0]
    for _ in range(3):
        el_type = element.get("type", "")
        if not el_type.startswith(("Core/Diagram.", "Plant/Diagram.")):
            break
        refs = _references(element)
        next_id = refs.get("Represents") or refs.get("Object") or refs.get("Node")
        if next_id and next_id in sheet.by_id and sheet.by_id[next_id] is not element:
            element = sheet.by_id[next_id]
        else:
            break
    return element


def _classify_pid_object(element: etree._Element) -> tuple[str, str]:
    obj_id = element.get("id") or ""
    cls = short_class(element)
    el_type = element.get("type", "")
    if (
        obj_id.startswith("SB_")
        or "SpecBreak" in cls
        or "PropertyBreak" in cls
        or "PipingClassBreak" in cls
    ):
        return "spec_break", "规格分界"
    if is_equipment(element):
        return "equipment", "设备"
    if _is_nozzle(element):
        return "nozzle", "管口"
    if cls in ("PipingNetworkSystem", "PipingNetworkSegment", "Pipe"):
        return "pipe", "管线" if cls == "PipingNetworkSystem" else "管段"
    if cls.endswith("Valve") or cls in ("ControlValve", "BreatherValve"):
        return "valve", "阀门"
    if el_type.startswith("Plant/Instrumentation.") or cls in (
        "ProcessInstrumentationFunction",
        "InstrumentationLoopFunction",
        "ProcessSignalGeneratingFunction",
        "SignalConveyingFunction",
        "SignalLineFunction",
        "MeasuringLineFunction",
        "ActuatingFunction",
        "ActuatingSystem",
    ):
        return "instrument", "仪表"
    if cls.endswith("OffPageConnector"):
        return "connector", "跨页连接"
    if obj_id.startswith("Dim_"):
        return "annotation", "尺寸标注"
    if el_type.startswith("Plant/Piping."):
        return "fitting", "管件/特殊件"
    return "other", "对象"


def _pipe_graph(
    sheet: Sheet,
) -> tuple[
    dict[str, list[tuple[str, etree._Element, etree._Element | None]]],
    dict[str, list[tuple[str, etree._Element, etree._Element | None]]],
]:
    """Directed adjacency from all ``Pipe`` connections (falling back to segment ends)."""
    outgoing: dict[str, list[tuple[str, etree._Element, etree._Element | None]]] = {}
    incoming: dict[str, list[tuple[str, etree._Element, etree._Element | None]]] = {}
    for seg in sheet.root.iter("Object"):
        if short_class(seg) != "PipingNetworkSegment":
            continue
        sys_el = _ancestor_or_self(seg, "PipingNetworkSystem")
        pipes = _object_children(seg, "Connections")
        holders = pipes if pipes else [seg]
        for holder in holders:
            refs = _references(holder)
            src = refs.get("SourceItem")
            dst = refs.get("TargetItem")
            if src and dst and src != dst:
                outgoing.setdefault(src, []).append((dst, seg, sys_el))
                incoming.setdefault(dst, []).append((src, seg, sys_el))
    return outgoing, incoming


def _ordered_segment_items(segment: etree._Element) -> list[etree._Element]:
    """Inline items of *segment* ordered along its ``Pipe`` chain from SourceItem."""
    items = _object_children(segment, "Items")
    if len(items) <= 1:
        return items
    by_id = {item.get("id"): item for item in items if item.get("id")}
    adj: dict[str, list[str]] = {}
    for pipe in _object_children(segment, "Connections"):
        refs = _references(pipe)
        src, dst = refs.get("SourceItem"), refs.get("TargetItem")
        if src and dst:
            adj.setdefault(src, []).append(dst)
    ordered: list[etree._Element] = []
    seen: set[str] = set()
    start = _references(segment).get("SourceItem")
    queue: deque[str] = deque([start] if start else [])
    visited: set[str] = set()
    while queue:
        cur = queue.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        if cur in by_id and cur not in seen:
            seen.add(cur)
            ordered.append(by_id[cur])
        for nxt in adj.get(cur, ()):
            if nxt not in visited:
                queue.append(nxt)
    for item in items:
        iid = item.get("id")
        if iid and iid not in seen:
            seen.add(iid)
            ordered.append(item)
    return ordered


def _display_item(sheet: Sheet, element: etree._Element) -> tuple[str, str | None, str]:
    """Return ``(id, display_tag, class_name)`` for a referenced item."""
    el_id = element.get("id") or ""
    cls = short_class(element)
    if _is_nozzle(element):
        equip = _equipment_of(element)
        noz_tag = _clean_tag(sheet.tag(element)) or el_id
        if equip is not None and equip is not element:
            eq_tag = _clean_tag(sheet.tag(equip)) or equip.get("id") or ""
            return el_id, f"{eq_tag}:{noz_tag}", cls
        return el_id, noz_tag, cls
    tag = _clean_tag(sheet.tag(element))
    if not tag and cls.endswith("OffPageConnector"):
        tag = data_value(element, "PipeConnectorNumber") or el_id
    return el_id, tag, cls


def _trace_nozzle_connections(
    sheet: Sheet,
    piping: Any,
    nozzle: etree._Element,
    equip: etree._Element | None,
) -> list[dict[str, Any]]:
    """Trace every segment attached to *nozzle* to its far-end equipment/OPC/tee."""
    noz_id = nozzle.get("id") or ""
    noz_tag = _clean_tag(sheet.tag(nozzle)) or noz_id
    attached = piping.ending_at.get(noz_id, [])
    results: list[dict[str, Any]] = []
    seen_keys: set[tuple[str | None, str | None, str | None, str]] = set()

    def _add(entry: dict[str, Any]) -> None:
        key = (entry["from_part"], entry["via_tag"], entry["other_id"], entry["direction"])
        if key not in seen_keys:
            seen_keys.add(key)
            results.append(entry)

    for seg in attached:
        seg_id = seg.get("id") or ""
        sys_el = _ancestor_or_self(seg, "PipingNetworkSystem")
        via_tag = _clean_tag(sheet.tag(sys_el)) if sys_el is not None else seg_id
        seg_refs = _references(seg)
        if seg_refs.get("SourceItem") == noz_id:
            direction = "out"
        elif seg_refs.get("TargetItem") == noz_id:
            direction = "in"
        else:
            direction = "peer"

        # BFS along segments belonging to the same PipingNetworkSystem to find far ends.
        reached: list[tuple[etree._Element, str]] = []
        visited_segs: set[etree._Element] = {seg}
        queue: deque[etree._Element] = deque([seg])
        while queue:
            cur_seg = queue.popleft()
            # Inspect segment ends and items inside this segment.
            end_ids: list[str] = []
            for prop in ("SourceItem", "TargetItem"):
                for ref in cur_seg.findall(f"References[@property='{prop}']"):
                    for t in ref.get("objects", "").split():
                        tid = t.lstrip("#")
                        if tid and tid not in end_ids:
                            end_ids.append(tid)
            for item in _object_children(cur_seg, "Items"):
                iid = item.get("id")
                if iid and iid not in end_ids:
                    end_ids.append(iid)
            for tid in end_ids:
                if tid == noz_id:
                    continue
                target = sheet.by_id.get(tid)
                if target is None:
                    continue
                t_cls = short_class(target)
                t_equip = _equipment_of(target)
                if t_equip is not None:
                    if equip is not None and t_equip is equip:
                        if _is_nozzle(target) and tid != noz_id:
                            reached.append((target, "peer"))
                    else:
                        reached.append((target, direction))
                    continue
                if t_cls.endswith("OffPageConnector"):
                    reached.append((target, direction))
                    continue
                # Follow connected segments in the same PipingNetworkSystem (via tees/shared items)
                other_segs = [
                    s
                    for s in piping.ending_at.get(tid, [])
                    if s not in visited_segs
                    and (sys_el is None or _ancestor_or_self(s, "PipingNetworkSystem") is sys_el)
                ]
                holder = piping.segment_of.get(tid)
                if (
                    holder is not None
                    and holder not in visited_segs
                    and (sys_el is None or _ancestor_or_self(holder, "PipingNetworkSystem") is sys_el)
                ):
                    other_segs.append(holder)
                if other_segs:
                    for nxt in other_segs:
                        visited_segs.add(nxt)
                        queue.append(nxt)
                elif (
                    holder is not None
                    and holder is not cur_seg
                    and (sys_el is None or _ancestor_or_self(holder, "PipingNetworkSystem") is not sys_el)
                ):
                    # Reached a tee / junction belonging to a different PipingNetworkSystem
                    reached.append((target, direction))

        if reached:
            for target, eff_dir in reached:
                t_equip = _equipment_of(target)
                if t_equip is not None and (equip is None or t_equip is not equip):
                    other_id = t_equip.get("id") or ""
                    other_tag = _clean_tag(sheet.tag(t_equip)) or other_id
                    other_cls = short_class(t_equip)
                    target_noz = _clean_tag(sheet.tag(target)) if _is_nozzle(target) else None
                    target_label = f"{other_tag}:{target_noz}" if target_noz else other_tag
                else:
                    other_id, other_tag, other_cls = _display_item(sheet, target)
                    target_label = other_tag or other_id
                arrow = "→" if eff_dir == "out" else ("←" if eff_dir == "in" else "↔")
                _add(
                    {
                        "via_id": seg_id,
                        "via_tag": via_tag,
                        "via_class": "PipingNetworkSegment",
                        "from_part": noz_tag,
                        "other_id": other_id,
                        "other_tag": other_tag or other_id,
                        "other_class": other_cls,
                        "direction": eff_dir,
                        "summary": f"{noz_tag} {arrow} {via_tag} {arrow} {target_label}",
                    }
                )
        else:
            # Fallback to the segment's direct opposite end
            opp_ids = [
                seg_refs.get(p)
                for p in ("SourceItem", "TargetItem")
                if seg_refs.get(p) and seg_refs.get(p) != noz_id
            ]
            for opp_id in opp_ids:
                opp = sheet.by_id.get(opp_id or "")
                if opp is None:
                    continue
                other_id, other_tag, other_cls = _display_item(sheet, opp)
                arrow = "→" if direction == "out" else ("←" if direction == "in" else "↔")
                _add(
                    {
                        "via_id": seg_id,
                        "via_tag": via_tag,
                        "via_class": "PipingNetworkSegment",
                        "from_part": noz_tag,
                        "other_id": other_id,
                        "other_tag": other_tag or other_id,
                        "other_class": other_cls,
                        "direction": direction,
                        "summary": f"{noz_tag} {arrow} {via_tag} {arrow} {other_tag or other_id}",
                    }
                )

    # Also instruments sensing this nozzle directly in XML
    for gen in _referrers(sheet, nozzle, "SensingLocation"):
        fn = _ancestor_or_self(gen, "ProcessInstrumentationFunction") or gen
        fn_id = fn.get("id") or ""
        fn_tag = _clean_tag(sheet.tag(fn)) or fn_id
        _add(
            {
                "via_id": None,
                "via_tag": "SensingLocation",
                "via_class": "Instrumentation",
                "from_part": noz_tag,
                "other_id": fn_id,
                "other_tag": fn_tag,
                "other_class": short_class(fn),
                "direction": "peer",
                "summary": f"{noz_tag} ↔ 测点 {fn_tag}",
            }
        )
    return results


def _safe_yaml_dict(snapshot: Snapshot, small_fn: Callable[[Snapshot, str], Any], path: str) -> dict[str, Any]:
    try:
        found = small_fn(snapshot, path)
    except ApiError:
        return {}
    if found is None:
        return {}
    try:
        raw = yaml.safe_load(found[0])
        if not isinstance(raw, dict):
            return {}
        return json_safe(raw)
    except (yaml.YAMLError, ApiError):
        return {}


def _match_entry(items: list[Any], key_field: str, candidates: list[str]) -> dict[str, Any] | None:
    if not candidates or not items:
        return None
    exact_set = {c.strip() for c in candidates if c}
    norm_set = {_norm_tag(c) for c in candidates if c}
    for item in items:
        if isinstance(item, dict):
            val = str(item.get(key_field, "") or "").strip()
            if val and val in exact_set:
                return dict(item)
    for item in items:
        if isinstance(item, dict):
            val = str(item.get(key_field, "") or "").strip()
            if val and _norm_tag(val) in norm_set:
                return dict(item)
    return None


def _tag_in_text(tag: str, text: str) -> bool:
    if not tag or not text:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(tag)}(?![A-Za-z0-9])", text))


def _str_val(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    if isinstance(value, dict):
        if "value" in value:
            unit = value.get("unit") or ""
            return f"{value['value']} {unit}".strip()
        if any(k in value for k in ("diameter", "height", "length", "width", "depth")):
            parts: list[str] = []
            d = value.get("diameter")
            h = value.get("height")
            ln = value.get("length")
            w = value.get("width") if value.get("width") is not None else value.get("depth")
            if isinstance(d, (int, float)):
                parts.append(f"Ø{d:g} m")
            if isinstance(ln, (int, float)) and isinstance(d, (int, float)):
                parts.append(f"L {ln:g} m")
            elif isinstance(h, (int, float)) and isinstance(d, (int, float)):
                parts.append(f"H {h:g} m")
            else:
                for val, lbl in ((ln, "L"), (w, "W"), (h, "H")):
                    if isinstance(val, (int, float)):
                        parts.append(f"{lbl} {val:g} m")
            if parts:
                return " × ".join(parts)
        return None
    text = str(value).strip()
    return text or None


_PHYSICAL_QUANTITY_UNIT_LABELS: dict[tuple[str, str], str] = {
    ("PressureGaugeUnit", "Kilopascal"): "kPa(g)",
    ("PressureGaugeUnit", "Bar"): "barg",
    ("PressureGaugeUnit", "Megapascal"): "MPa(g)",
    ("PressureAbsoluteUnit", "Kilopascal"): "kPa(a)",
    ("PressureAbsoluteUnit", "Bar"): "bara",
    ("PressureAbsoluteUnit", "Megapascal"): "MPa(a)",
    ("LengthUnit", "Millimetre"): "mm",
    ("LengthUnit", "Metre"): "m",
    ("TemperatureUnit", "DegreeCelsius"): "°C",
}

_LOCATION_LABELS: dict[str, str] = {
    "CentralLocation": "中控室 (DCS)",
    "Field": "现场 (Field)",
    "ControlPanel": "就地盘 (ControlPanel)",
}

_FAIL_ACTION_LABELS: dict[str, str] = {
    "FailClose": "FC (故障关)",
    "FC": "FC (故障关)",
    "FailOpen": "FO (故障开)",
    "FO": "FO (故障开)",
    "FailRetainPosition": "FL (故障保位)",
    "FL": "FL (故障保位)",
}


def _extract_physical_quantity(element: etree._Element, prop: str) -> tuple[float, str, str] | None:
    """Parse ``Core/PhysicalQuantities.PhysicalQuantity`` into ``(value, unit_class, unit_leaf)``."""
    data = element.find(f"Data[@property='{prop}']")
    if data is None:
        return None
    agg = data.find("AggregatedDataValue[@type='Core/PhysicalQuantities.PhysicalQuantity']")
    if agg is None:
        return None
    raw_val = data_value(agg, "Value")
    if raw_val is None:
        return None
    try:
        num = float(raw_val)
    except ValueError:
        return None
    unit_ref = agg.find("Data[@property='Unit']/DataReference")
    ref_str = (unit_ref.get("data") or "") if unit_ref is not None else ""
    parts = [p for p in ref_str.split(".") if p]
    unit_class = parts[-2] if len(parts) >= 2 else ""
    unit_leaf = parts[-1] if parts else ""
    return num, unit_class, unit_leaf


def _format_physical_quantity(
    val: float,
    unit_class: str,
    unit_leaf: str,
    *,
    include_barg_equiv: bool = False,
) -> str:
    """Format a DEXPI ``PhysicalQuantity`` for display (optionally including barg equivalent)."""
    if include_barg_equiv and unit_class == "PressureGaugeUnit":
        if unit_leaf == "Kilopascal":
            barg = round(val / 100.0, 6)
            sign = "+" if val > 0 else ""
            return f"{sign}{val:g} kPa(g) ({barg:g} barg)"
        if unit_leaf == "Bar" and abs(val) < 0.1 and val != 0:
            kpa = round(val * 100.0, 6)
            sign = "+" if val > 0 else ""
            return f"{sign}{kpa:g} kPa(g) ({val:g} barg)"
    unit_str = _PHYSICAL_QUANTITY_UNIT_LABELS.get((unit_class, unit_leaf), unit_leaf)
    return f"{val:g} {unit_str}".strip()


def _extract_data_text(element: etree._Element, prop: str) -> str | None:
    """Read direct ``<Data property=prop>`` supporting strings, refs, PhysicalQuantity & MultiLanguageString."""
    data = element.find(f"Data[@property='{prop}']")
    if data is None:
        return None
    agg = data.find("AggregatedDataValue")
    if agg is not None:
        agg_type = agg.get("type") or ""
        if agg_type == "Core/PhysicalQuantities.PhysicalQuantity":
            pq = _extract_physical_quantity(element, prop)
            return _format_physical_quantity(*pq) if pq is not None else None
        if agg_type == "Core/DataTypes.MultiLanguageString":
            candidates: list[tuple[str, str]] = []
            for sls in agg.findall("Data[@property='SingleLanguageStrings']/AggregatedDataValue"):
                lang = (data_value(sls, "Language") or "").strip().lower()
                val_txt = (data_value(sls, "Value") or "").strip()
                if val_txt:
                    candidates.append((lang, val_txt))
            for lang, val_txt in candidates:
                if lang.startswith("zh"):
                    return val_txt
            return candidates[0][1] if candidates else None
    return data_value(element, prop)


def _xml_valve_actuator_info(sheet: Sheet, valve_el: etree._Element) -> tuple[str | None, str | None]:
    """Read native fail action and actuator device type via ``OperatedValveReference`` -> ``ActuatingSystem``."""
    fail_action: str | None = None
    device_type: str | None = None
    for ref_el in _referrers(sheet, valve_el, "Valve"):
        act_sys = _ancestor_or_self(ref_el, "ActuatingSystem")
        if act_sys is None:
            continue
        for act_el in _object_children(act_sys, "ControlledActuator"):
            if not fail_action:
                raw_fail = data_value(act_el, "FailAction") or data_value(act_el, "FailActionRepresentation")
                if raw_fail:
                    fail_action = _FAIL_ACTION_LABELS.get(raw_fail, raw_fail)
            if not device_type:
                device_type = data_value(act_el, "DeviceTypeName")
        if fail_action and device_type:
            break
    return fail_action, device_type


def _xml_chamber_summary(element: etree._Element) -> dict[str, str | None]:
    """Summarize native DEXPI ``Chamber`` dimensions, material and design limits on equipment."""
    dim_parts: list[str] = []
    mats: list[str] = []
    pressures: list[str] = []
    temps: list[str] = []
    descs: list[str] = []
    for chamber in _object_children(element, "Chambers"):
        c_dims: list[str] = []
        dn_txt = _extract_data_text(chamber, "NominalDiameter") or _extract_data_text(chamber, "InsideDiameter")
        if dn_txt:
            c_dims.append(f"Ø{dn_txt}")
        ln_txt = _extract_data_text(chamber, "Length")
        ht_txt = _extract_data_text(chamber, "Height")
        if ln_txt:
            c_dims.append(f"L {ln_txt}")
        elif ht_txt:
            c_dims.append(f"H {ht_txt}")
        if c_dims:
            dim_parts.append(" × ".join(c_dims))
        for val, bucket in (
            (_extract_data_text(chamber, "MaterialOfConstructionCode"), mats),
            (_extract_data_text(chamber, "UpperLimitDesignPressure"), pressures),
            (_extract_data_text(chamber, "UpperLimitDesignTemperature"), temps),
            (_extract_data_text(chamber, "ChamberDescription"), descs),
        ):
            if val and val not in bucket:
                bucket.append(val)
    return {
        "dimensions": "; ".join(dim_parts) or None,
        "material": ", ".join(mats) or None,
        "design_pressure": ", ".join(pressures) or None,
        "design_temperature": ", ".join(temps) or None,
        "description": "; ".join(descs) or None,
    }


def _inspect_pid_object(
    sheet: Sheet,
    obj_id: str,
    lists: dict[str, dict[str, Any]],
    datasheet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the rich inspection payload for ``GET /api/pid/object``."""
    element = _resolve_pid_element(sheet, obj_id)
    resolved_id = element.get("id") or obj_id
    dexpi_class = short_class(element)
    category, category_label = _classify_pid_object(element)

    raw_tags = sheet.tags(element)
    clean_tags = [t for t in (_clean_tag(x) for x in raw_tags) if t]
    tag = clean_tags[0] if clean_tags else None
    if not tag and dexpi_class.endswith("OffPageConnector"):
        tag = data_value(element, "PipeConnectorNumber")

    # Direct XML attributes
    attributes: dict[str, str] = {}
    for data_el in element.findall("Data"):
        prop = data_el.get("property") or ""
        if not prop:
            continue
        val = _extract_data_text(element, prop)
        if val:
            attributes[prop] = val

    # Ancestor / owner resolution
    seg_el = (
        element
        if dexpi_class == "PipingNetworkSegment"
        else _ancestor_or_self(element, "PipingNetworkSegment")
    )
    sys_el = (
        element
        if dexpi_class == "PipingNetworkSystem"
        else _ancestor_or_self(element, "PipingNetworkSystem")
    )
    equip_el = _equipment_of(element) if _is_nozzle(element) else None

    owner_el: etree._Element | None = None
    if _is_nozzle(element) and equip_el is not None and equip_el is not element:
        owner_el = equip_el
    elif dexpi_class == "PipingNetworkSegment" and sys_el is not None and sys_el is not element:
        owner_el = sys_el
    elif category in ("valve", "fitting", "connector", "spec_break"):
        if sys_el is not None and sys_el is not element:
            owner_el = sys_el
        elif seg_el is not None and seg_el is not element:
            owner_el = seg_el
    else:
        for anc in element.iterancestors("Object"):
            anc_cls = short_class(anc)
            if anc_cls not in ("PlantModel", "EngineeringModel", "Diagram", "Drawing"):
                owner_el = anc
                break

    owner = (
        {
            "id": owner_el.get("id") or "",
            "class": short_class(owner_el),
            "tag": _clean_tag(sheet.tag(owner_el)),
        }
        if owner_el is not None and owner_el.get("id")
        else None
    )

    # Inherit line/segment piping metadata when applicable
    line_number: str | None = attributes.get("LineNumber")
    fluid_code: str | None = attributes.get("FluidCode")
    piping_class: str | None = attributes.get("PipingClassCode")
    if not piping_class:
        fin = attributes.get("FlowInPipingClassCode")
        fout = attributes.get("FlowOutPipingClassCode")
        if fin and fout and fin != fout:
            piping_class = f"{fin} → {fout}"
        elif fin or fout:
            piping_class = fin or fout
    raw_dn: str | None = _extract_raw_dn(element)
    seg_id_val: str | None = seg_el.get("id") if seg_el is not None else None

    for anc in (seg_el, sys_el):
        if anc is None:
            continue
        if not line_number:
            line_number = data_value(anc, "LineNumber")
        if not fluid_code:
            fluid_code = data_value(anc, "FluidCode")
        if not piping_class:
            piping_class = data_value(anc, "PipingClassCode")
        if not raw_dn:
            raw_dn = _extract_raw_dn(anc)

    if line_number:
        attributes.setdefault("LineNumber", line_number)
    if fluid_code:
        attributes.setdefault("FluidCode", fluid_code)
    if piping_class:
        attributes.setdefault("PipingClassCode", piping_class)
    if raw_dn:
        attributes["NominalDiameter"] = raw_dn
    if seg_id_val and category in ("valve", "fitting", "connector", "pipe"):
        attributes.setdefault("Segment", seg_id_val)

    # Match YAML lists (equipment-list, line-list, special-items, instrument-list)
    eq_list = lists.get("equipment", {}).get("equipment") or []
    line_list = lists.get("lines", {}).get("lines") or []
    sp_list = lists.get("piping_items", {}).get("piping_items") or []
    inst_list = lists.get("instruments", {}).get("instruments") or []

    tag_candidates = list(dict.fromkeys([*clean_tags, *(t for t in (tag, line_number) if t)]))
    list_info: dict[str, Any] | None = None
    if category == "equipment":
        list_info = _match_entry(eq_list, "tag", clean_tags)
    elif category == "pipe":
        line_cands = [c for c in (line_number, tag) if c]
        list_info = _match_entry(line_list, "number", line_cands)
    elif category == "instrument":
        inst_hit = _match_entry(inst_list, "tag", clean_tags)
        sp_hit = _match_entry(sp_list, "tag", clean_tags)
        if inst_hit or sp_hit:
            list_info = {**(sp_hit or {}), **(inst_hit or {})}
    elif category in ("valve", "fitting"):
        sp_hit = _match_entry(sp_list, "tag", clean_tags)
        inst_hit = _match_entry(inst_list, "tag", clean_tags)
        if sp_hit or inst_hit:
            list_info = {**(inst_hit or {}), **(sp_hit or {})}
    else:
        list_info = (
            _match_entry(eq_list, "tag", clean_tags)
            or _match_entry(sp_list, "tag", clean_tags)
            or _match_entry(inst_list, "tag", clean_tags)
            or _match_entry(line_list, "number", tag_candidates)
        )

    if list_info:
        if not line_number and list_info.get("line"):
            line_number = str(list_info["line"])
            attributes.setdefault("LineNumber", line_number)
        if not piping_class and list_info.get("pressure_class"):
            piping_class = str(list_info["pressure_class"])
            attributes.setdefault("PipingClassCode", piping_class)
        if not fluid_code and list_info.get("fluid"):
            fluid_code = str(list_info["fluid"])
            attributes.setdefault("FluidCode", fluid_code)
        if not raw_dn and list_info.get("nominal_diameter"):
            raw_dn = str(list_info["nominal_diameter"])
            attributes.setdefault("NominalDiameter", raw_dn)

    # Controls & interlocks matching (control/loops.yaml & control/cause-effect.yaml)
    loops_raw = lists.get("loops", {}).get("loops") or []
    interlocks_raw = lists.get("interlocks", {}).get("interlocks") or []
    controls: list[dict[str, Any]] = []
    control_peer_tags: list[tuple[str, str, str]] = []  # (other_tag, direction, summary_prefix)
    loop_ids: list[str] = []
    alarm_parts: list[str] = []
    interlock_parts: list[str] = []
    fail_position: str | None = None
    actuator_op: str | None = None

    norm_self_tags = {_norm_tag(t) for t in clean_tags}

    for lp in loops_raw:
        if not isinstance(lp, dict):
            continue
        lp_id = str(lp.get("id") or "")
        lp_title = str(lp.get("title") or "")
        lp_purpose = str(lp.get("purpose") or "")
        ctrl_tag = str(lp.get("controller") or "")
        meas_tags = [str(m.get("tag") or "") for m in (lp.get("measurement") or []) if isinstance(m, dict) and m.get("tag")]
        fe_items = [fe for fe in (lp.get("final_elements") or []) if isinstance(fe, dict) and fe.get("tag")]
        fe_tags = [str(fe["tag"]) for fe in fe_items]
        al_items = [a for a in (lp.get("alarms") or []) if isinstance(a, dict)]
        al_tags = [str(a.get("tag") or "") for a in al_items if a.get("tag")]

        all_lp_tags = [t for t in [lp_id, ctrl_tag, *meas_tags, *fe_tags, *al_tags] if t]
        matched = any(_norm_tag(t) in norm_self_tags for t in all_lp_tags)
        if not matched and category == "equipment" and tag:
            matched = _tag_in_text(tag, lp_title) or _tag_in_text(tag, lp_purpose)
        if not matched:
            continue

        if lp_id and lp_id not in loop_ids:
            loop_ids.append(lp_id)
        detail_bits: list[str] = []
        if meas_tags:
            detail_bits.append(f"测量: {', '.join(meas_tags)}")
        if ctrl_tag:
            act = f" ({lp['action']})" if lp.get("action") else ""
            detail_bits.append(f"控制: {ctrl_tag}{act}")
        if fe_items:
            fe_strs = []
            for fe in fe_items:
                f_str = str(fe["tag"])
                if fe.get("fail"):
                    f_str += f" ({'FC' if fe['fail'] == 'closed' else 'FO' if fe['fail'] == 'open' else fe['fail']})"
                fe_strs.append(f_str)
                if _norm_tag(str(fe["tag"])) in norm_self_tags:
                    if fe.get("fail") and not fail_position:
                        fail_position = (
                            "FC (故障关)"
                            if fe["fail"] == "closed"
                            else "FO (故障开)"
                            if fe["fail"] == "open"
                            else str(fe["fail"])
                        )
                    if fe.get("actuator") and not actuator_op:
                        act_map = {"pneumatic": "气动 (pneumatic)", "electric": "电动 (electric)", "hydraulic": "液动 (hydraulic)"}
                        actuator_op = act_map.get(str(fe["actuator"]), str(fe["actuator"]))
            detail_bits.append(f"执行: {', '.join(fe_strs)}")
        if al_items:
            al_strs = []
            for a in al_items:
                cond = str(a.get("condition") or "")
                lim = a.get("limit")
                lim_s = _str_val(lim) if lim else None
                entry_s = f"{cond} {lim_s}".strip() if cond or lim_s else ""
                if entry_s:
                    al_strs.append(entry_s)
                    if not a.get("tag") or _norm_tag(str(a["tag"])) in norm_self_tags:
                        if entry_s not in alarm_parts:
                            alarm_parts.append(entry_s)
            if al_strs:
                detail_bits.append(f"报警: {', '.join(al_strs)}")
        controls.append(
            {
                "kind": "loop",
                "id": lp_id or ctrl_tag or "Loop",
                "title": lp_title or lp_purpose,
                "detail": " | ".join(detail_bits),
            }
        )
        # Clickable loop connections for instruments & final elements
        if category in ("instrument", "valve"):
            if ctrl_tag and _norm_tag(ctrl_tag) in norm_self_tags:
                for mt in meas_tags:
                    if _norm_tag(mt) not in norm_self_tags:
                        control_peer_tags.append((mt, "in", f"回路 {lp_id} 测量"))
                for ft in fe_tags:
                    if _norm_tag(ft) not in norm_self_tags:
                        control_peer_tags.append((ft, "out", f"回路 {lp_id} 执行"))
            elif any(_norm_tag(mt) in norm_self_tags for mt in meas_tags) and ctrl_tag:
                if _norm_tag(ctrl_tag) not in norm_self_tags:
                    control_peer_tags.append((ctrl_tag, "out", f"回路 {lp_id} 控制器"))
            elif any(_norm_tag(ft) in norm_self_tags for ft in fe_tags) and ctrl_tag:
                if _norm_tag(ctrl_tag) not in norm_self_tags:
                    control_peer_tags.append((ctrl_tag, "in", f"回路 {lp_id} 控制器"))

    for il in interlocks_raw:
        if not isinstance(il, dict):
            continue
        il_id = str(il.get("id") or "")
        il_title = str(il.get("title") or "")
        il_notes = str(il.get("notes") or "")
        cause = il.get("cause") if isinstance(il.get("cause"), dict) else {}
        cause_tag = str(cause.get("tag") or "")
        cause_cond = str(cause.get("condition") or "")
        effects = [e for e in (il.get("effects") or []) if isinstance(e, dict) and e.get("tag")]
        eff_tags = [str(e["tag"]) for e in effects]

        all_il_tags = [t for t in [il_id, cause_tag, *eff_tags] if t]
        matched = any(_norm_tag(t) in norm_self_tags for t in all_il_tags)
        if not matched and tag:
            matched = _tag_in_text(tag, il_notes) or (category == "equipment" and _tag_in_text(tag, il_title))
        if not matched:
            continue

        eff_desc = ", ".join(
            f"{e['tag']} ({e.get('action', '')})".strip() if e.get("action") else str(e["tag"])
            for e in effects
        )
        cause_desc = f"{cause_tag} ({cause_cond})" if cause_cond else cause_tag
        detail = f"触发: {cause_desc} → 动作: {eff_desc}"
        if il_notes:
            detail += f" — {il_notes}"
        controls.append(
            {
                "kind": "interlock",
                "id": il_id or "Interlock",
                "title": il_title,
                "detail": detail,
            }
        )
        il_short = f"{il_id}: {cause_cond or '联锁'} → {eff_desc}" if il_id else detail
        if il_short not in interlock_parts:
            interlock_parts.append(il_short)
        if category in ("instrument", "valve"):
            if cause_tag and _norm_tag(cause_tag) in norm_self_tags:
                for et in eff_tags:
                    if _norm_tag(et) not in norm_self_tags:
                        control_peer_tags.append((et, "out", f"联锁 {il_id} 动作"))
            elif any(_norm_tag(et) in norm_self_tags for et in eff_tags) and cause_tag:
                if _norm_tag(cause_tag) not in norm_self_tags:
                    control_peer_tags.append((cause_tag, "in", f"联锁 {il_id} 触发"))
            elif tag and _tag_in_text(tag, il_notes):
                if cause_tag and _norm_tag(cause_tag) not in norm_self_tags:
                    control_peer_tags.append((cause_tag, "in", f"联锁 {il_id} 触发"))
                for et in eff_tags:
                    if _norm_tag(et) not in norm_self_tags:
                        control_peer_tags.append((et, "out", f"联锁 {il_id} 动作"))

    # Also check XML InstrumentationLoopFunction
    for fn in _functions_of(sheet, element):
        for lp_el in _loops(sheet, fn):
            lp_tag = _clean_tag(sheet.tag(lp_el)) or lp_el.get("id") or ""
            if lp_tag and lp_tag not in loop_ids:
                loop_ids.append(lp_tag)

    # Connections & Parts
    piping = _piping_index(sheet)
    outgoing_pipes, incoming_pipes = _pipe_graph(sheet)
    parts: list[dict[str, Any]] = []
    connections_list: list[dict[str, Any]] = []
    seen_conn: set[tuple[str | None, str | None, str | None, str]] = set()

    def _add_conn(entry: dict[str, Any]) -> None:
        key = (entry.get("from_part"), entry.get("via_tag"), entry.get("other_id"), entry.get("direction", "peer"))
        if key not in seen_conn:
            seen_conn.add(key)
            connections_list.append(entry)

    if category == "equipment":
        nozzles = [e for e in element.iter("Object") if _is_nozzle(e)]
        for noz in nozzles:
            noz_id = noz.get("id") or ""
            noz_tag = _clean_tag(sheet.tag(noz)) or noz_id
            noz_dn = _extract_raw_dn(noz)
            noz_conns = _trace_nozzle_connections(sheet, piping, noz, element)
            for c in noz_conns:
                _add_conn(c)
            conn_summary = (
                "; ".join(
                    f"{c['via_tag']} → {c['other_tag']}" if c.get("via_tag") else str(c.get("other_tag") or "")
                    for c in noz_conns[:2]
                )
                or None
            )
            parts.append(
                {
                    "id": noz_id,
                    "class": short_class(noz),
                    "tag": noz_tag,
                    "dn": noz_dn,
                    "connected_to": conn_summary,
                }
            )
        # Other non-nozzle, non-node subcomponents
        for comps in element.findall("Components"):
            for child in comps.findall("Object"):
                c_cls = short_class(child)
                if (
                    _is_nozzle(child)
                    or c_cls == "PipingNode"
                    or (c_cls == "Chamber" and child.get("id") not in sheet.drawings)
                    or not child.get("id")
                ):
                    continue
                parts.append(
                    {
                        "id": child.get("id"),
                        "class": c_cls,
                        "tag": _clean_tag(sheet.tag(child)) or child.get("id"),
                        "dn": _extract_raw_dn(child),
                        "connected_to": None,
                    }
                )
    elif category == "nozzle":
        for c in _trace_nozzle_connections(sheet, piping, element, equip_el):
            _add_conn(c)
    elif dexpi_class == "PipingNetworkSegment":
        seg_refs = _references(element)
        for prop, direction in (("SourceItem", "in"), ("TargetItem", "out")):
            tid = seg_refs.get(prop)
            if tid and tid in sheet.by_id:
                target = sheet.by_id[tid]
                oid, otag, ocls = _display_item(sheet, target)
                arrow = "←" if direction == "in" else "→"
                label_dir = "起点" if direction == "in" else "终点"
                _add_conn(
                    {
                        "via_id": resolved_id,
                        "via_tag": line_number or resolved_id,
                        "via_class": "PipingNetworkSegment",
                        "from_part": None,
                        "other_id": oid,
                        "other_tag": otag or oid,
                        "other_class": ocls,
                        "direction": direction,
                        "summary": f"{label_dir} {arrow} {otag or oid} ({ocls})",
                    }
                )
        for item in _ordered_segment_items(element):
            iid = item.get("id") or ""
            parts.append(
                {
                    "id": iid,
                    "class": short_class(item),
                    "tag": _clean_tag(sheet.tag(item)) or iid,
                    "dn": _extract_raw_dn(item),
                    "connected_to": None,
                }
            )
    elif dexpi_class == "PipingNetworkSystem":
        segs = _object_children(element, "Segments")
        internal_ids: set[str] = set()
        for seg in segs:
            for item in _ordered_segment_items(seg):
                iid = item.get("id") or ""
                if iid:
                    internal_ids.add(iid)
                parts.append(
                    {
                        "id": iid,
                        "class": short_class(item),
                        "tag": _clean_tag(sheet.tag(item)) or iid,
                        "dn": _extract_raw_dn(item),
                        "connected_to": seg.get("id"),
                    }
                )
        for seg in segs:
            s_id = seg.get("id") or ""
            seg_refs = _references(seg)
            for prop, direction in (("SourceItem", "in"), ("TargetItem", "out")):
                tid = seg_refs.get(prop)
                if not tid or tid not in sheet.by_id:
                    continue
                target = sheet.by_id[tid]
                t_cls = short_class(target)
                if tid in internal_ids and not t_cls.endswith("OffPageConnector"):
                    continue
                t_equip = _equipment_of(target)
                if t_equip is not None:
                    oid = t_equip.get("id") or ""
                    otag = _clean_tag(sheet.tag(t_equip)) or oid
                    ocls = short_class(t_equip)
                    noz_s = _clean_tag(sheet.tag(target)) if _is_nozzle(target) else None
                    disp = f"{otag}:{noz_s}" if noz_s else otag
                else:
                    oid, otag, ocls = _display_item(sheet, target)
                    disp = otag or oid
                label_dir = "起点" if direction == "in" else "终点"
                arrow = "←" if direction == "in" else "→"
                _add_conn(
                    {
                        "via_id": s_id,
                        "via_tag": line_number or s_id,
                        "via_class": "PipingNetworkSegment",
                        "from_part": None,
                        "other_id": oid,
                        "other_tag": disp,
                        "other_class": ocls,
                        "direction": direction,
                        "summary": f"{label_dir} {arrow} {disp} ({ocls})",
                    }
                )
    elif category in ("valve", "fitting", "connector", "spec_break"):
        for nb_id, seg, sys_parent in incoming_pipes.get(resolved_id, []):
            nb = sheet.by_id.get(nb_id)
            if nb is None:
                continue
            oid, otag, ocls = _display_item(sheet, nb)
            vtag = _clean_tag(sheet.tag(sys_parent)) if sys_parent is not None else seg.get("id")
            _add_conn(
                {
                    "via_id": seg.get("id"),
                    "via_tag": vtag,
                    "via_class": "PipingNetworkSegment",
                    "from_part": None,
                    "other_id": oid,
                    "other_tag": otag or oid,
                    "other_class": ocls,
                    "direction": "in",
                    "summary": f"上游 ← {otag or oid} ({ocls})",
                }
            )
        for nb_id, seg, sys_parent in outgoing_pipes.get(resolved_id, []):
            nb = sheet.by_id.get(nb_id)
            if nb is None:
                continue
            oid, otag, ocls = _display_item(sheet, nb)
            vtag = _clean_tag(sheet.tag(sys_parent)) if sys_parent is not None else seg.get("id")
            _add_conn(
                {
                    "via_id": seg.get("id"),
                    "via_tag": vtag,
                    "via_class": "PipingNetworkSegment",
                    "from_part": None,
                    "other_id": oid,
                    "other_tag": otag or oid,
                    "other_class": ocls,
                    "direction": "out",
                    "summary": f"下游 → {otag or oid} ({ocls})",
                }
            )
        # XML actuation links for valves
        for fn in _functions_of(sheet, element):
            fn_id = fn.get("id") or ""
            if fn_id and fn_id != resolved_id:
                fn_tag = _clean_tag(sheet.tag(fn)) or fn_id
                _add_conn(
                    {
                        "via_id": None,
                        "via_tag": "Instrumentation",
                        "via_class": "ProcessInstrumentationFunction",
                        "from_part": None,
                        "other_id": fn_id,
                        "other_tag": fn_tag,
                        "other_class": short_class(fn),
                        "direction": "in",
                        "summary": f"控制信号 ← {fn_tag}",
                    }
                )
    elif category == "instrument":
        # XML SensingLocation, SignalConveyingFunction / SignalLineFunction, ActuatingFunction
        for sub in element.iter("Object"):
            sub_cls = short_class(sub)
            if sub_cls == "ProcessSignalGeneratingFunction":
                for target in _targets(sheet, sub, "SensingLocation"):
                    oid, otag, ocls = _display_item(sheet, target)
                    _add_conn(
                        {
                            "via_id": None,
                            "via_tag": "SensingLocation",
                            "via_class": "ProcessSignalGeneratingFunction",
                            "from_part": None,
                            "other_id": oid,
                            "other_tag": otag or oid,
                            "other_class": ocls,
                            "direction": "in",
                            "summary": f"测点 ← {otag or oid} ({ocls})",
                        }
                    )
            elif sub_cls in ("SignalConveyingFunction", "SignalLineFunction", "MeasuringLineFunction"):
                for end_prop, direction in (("Source", "in"), ("Target", "out")):
                    for target in _targets(sheet, sub, end_prop):
                        fn = _ancestor_or_self(target, "ProcessInstrumentationFunction") or target
                        if fn is element:
                            continue
                        oid, otag, ocls = _display_item(sheet, fn)
                        _add_conn(
                            {
                                "via_id": sub.get("id"),
                                "via_tag": sub_cls,
                                "via_class": sub_cls,
                                "from_part": None,
                                "other_id": oid,
                                "other_tag": otag or oid,
                                "other_class": ocls,
                                "direction": direction,
                                "summary": f"信号 {'←' if direction == 'in' else '→'} {otag or oid}",
                            }
                        )
            elif sub_cls == "ActuatingFunction":
                for sys_act in _targets(sheet, sub, "Systems"):
                    for ref_child in sys_act.iter("Object"):
                        if short_class(ref_child) == "OperatedValveReference":
                            for valve in _targets(sheet, ref_child, "Valve"):
                                oid, otag, ocls = _display_item(sheet, valve)
                                _add_conn(
                                    {
                                        "via_id": sys_act.get("id"),
                                        "via_tag": "ActuatingSystem",
                                        "via_class": "ActuatingSystem",
                                        "from_part": None,
                                        "other_id": oid,
                                        "other_tag": otag or oid,
                                        "other_class": ocls,
                                        "direction": "out",
                                        "summary": f"驱动 → {otag or oid} ({ocls})",
                                    }
                                )
        # Incoming signal references targeting this instrument
        for ref_el, prop in sheet.incoming.get(resolved_id, []):
            if prop in ("Source", "Target"):
                holder = ref_el.getparent()
                other_fn = _ancestor_or_self(holder, "ProcessInstrumentationFunction")
                if other_fn is not None and other_fn is not element:
                    oid, otag, ocls = _display_item(sheet, other_fn)
                    direction = "out" if prop == "Source" else "in"
                    _add_conn(
                        {
                            "via_id": holder.get("id"),
                            "via_tag": short_class(holder),
                            "via_class": short_class(holder),
                            "from_part": None,
                            "other_id": oid,
                            "other_tag": otag or oid,
                            "other_class": ocls,
                            "direction": direction,
                            "summary": f"信号 {'→' if direction == 'out' else '←'} {otag or oid}",
                        }
                    )

    # Resolve control loop / interlock peer tags to clickable sheet objects (when not already connected)
    for peer_tag, peer_dir, prefix in control_peer_tags:
        matches = [
            m
            for m in sheet.find(peer_tag)
            if m is not element
            and not m.get("type", "").startswith(("Core/Diagram.", "Plant/Diagram."))
        ]
        # Prefer valves/instruments over labels; pick best single match per peer_tag
        if matches:
            target = matches[0]
            oid, otag, ocls = _display_item(sheet, target)
            _add_conn(
                {
                    "via_id": None,
                    "via_tag": prefix,
                    "via_class": "Control",
                    "from_part": None,
                    "other_id": oid,
                    "other_tag": otag or peer_tag,
                    "other_class": ocls,
                    "direction": peer_dir,
                    "summary": f"{prefix}: {otag or peer_tag} ({ocls})",
                }
            )

    # ------------------------------------------------------------------
    # Standard Category Attribute Template (missing fields -> None -> "—")
    # ------------------------------------------------------------------
    nom_pressure, material_group = _derive_piping_class(piping_class)
    spec_material = _lookup_pipe_spec_material(lists, piping_class)
    dn_fmt = _format_dn(raw_dn)
    li = list_info or {}
    ds = datasheet or {}

    def _from_ds(*keys: str) -> str | None:
        for k in keys:
            if k in li and li[k] is not None:
                v = _str_val(li[k])
                if v:
                    return v
        for k in keys:
            if k in attributes and attributes[k]:
                return attributes[k]
        # Also peek into datasheet sections (design, mechanical, process)
        for sec in (ds, ds.get("design"), ds.get("mechanical"), ds.get("dimensions")):
            if isinstance(sec, dict):
                for k in keys:
                    if k in sec and sec[k] is not None:
                        v = _str_val(sec[k])
                        if v:
                            return v
        return None

    properties: list[dict[str, str | None]] = []

    if category == "equipment":
        ch_info = _xml_chamber_summary(element)
        properties = [
            {"label": "位号", "key": "TagName", "value": tag or attributes.get("TagName")},
            {"label": "类型", "key": "DEXPI Class", "value": dexpi_class},
            {"label": "名称/用途", "key": "service", "value": attributes.get("EquipmentDescription") or _from_ds("service")},
            {"label": "设计压力", "key": "design_pressure_barg", "value": attributes.get("UpperLimitDesignPressure") or ch_info["design_pressure"] or _from_ds("design_pressure_barg", "DesignPressure", "UpperLimitDesignPressure")},
            {"label": "设计温度", "key": "design_temperature_degc", "value": attributes.get("UpperLimitDesignTemperature") or ch_info["design_temperature"] or _from_ds("design_temperature_degc", "DesignTemperature", "UpperLimitDesignTemperature")},
            {"label": "材质", "key": "material", "value": attributes.get("MaterialOfConstructionCode") or ch_info["material"] or _from_ds("material", "MaterialOfConstructionCode")},
            {"label": "尺寸/容积", "key": "dimensions", "value": ch_info["dimensions"] or _from_ds("dimensions", "volume", "capacity", "NominalCapacity")},
            {"label": "备注", "key": "notes", "value": _from_ds("notes")},
        ]
    elif category == "nozzle":
        # Derive connected line and nominal pressure from attached segment(s)
        attached_segs = piping.ending_at.get(resolved_id, [])
        conn_lines: list[str] = []
        noz_np = attributes.get("NominalPressure")
        for seg in attached_segs:
            s_sys = _ancestor_or_self(seg, "PipingNetworkSystem")
            lno = data_value(s_sys, "LineNumber") if s_sys is not None else seg.get("id")
            if lno and lno not in conn_lines:
                conn_lines.append(lno)
            if not noz_np:
                pcls = data_value(seg, "PipingClassCode") or (data_value(s_sys, "PipingClassCode") if s_sys is not None else None)
                noz_np, _ = _derive_piping_class(pcls)
        properties = [
            {"label": "管口号", "key": "SubTagName", "value": attributes.get("SubTagName") or tag},
            {"label": "所属设备", "key": "owner", "value": (owner["tag"] or owner["id"]) if owner else None},
            {"label": "公称通径", "key": "DN", "value": dn_fmt},
            {"label": "公称压力", "key": "NominalPressure", "value": noz_np},
            {"label": "连接管线", "key": "connected_line", "value": ", ".join(conn_lines) if conn_lines else None},
        ]
    elif category == "pipe":
        seg_display = (
            resolved_id
            if dexpi_class == "PipingNetworkSegment"
            else (
                f"{len(_object_children(element, 'Segments'))} 段"
                if dexpi_class == "PipingNetworkSystem"
                else None
            )
        )
        in_conns = [c["other_tag"] for c in connections_list if c["direction"] == "in" and c.get("other_tag")]
        out_conns = [c["other_tag"] for c in connections_list if c["direction"] == "out" and c.get("other_tag")]
        xml_from = ", ".join(dict.fromkeys(in_conns)) or None
        xml_to = ", ".join(dict.fromkeys(out_conns)) or None
        if dexpi_class == "PipingNetworkSegment":
            from_val = xml_from or _str_val(li.get("from"))
            to_val = xml_to or _str_val(li.get("to"))
        else:
            from_val = _str_val(li.get("from")) or xml_from
            to_val = _str_val(li.get("to")) or xml_to
        properties = [
            {"label": "管线号", "key": "LineNumber", "value": line_number},
            {"label": "管段号", "key": "Segment", "value": seg_display},
            {"label": "公称通径", "key": "DN", "value": dn_fmt},
            {"label": "介质", "key": "FluidCode", "value": fluid_code},
            {"label": "管道等级", "key": "PipingClassCode", "value": piping_class},
            {"label": "材质等级", "key": "MaterialGroup", "value": f"{material_group} / {spec_material}" if (material_group and spec_material) else (spec_material or material_group)},
            {"label": "绝热/伴热", "key": "InsulationType", "value": _from_ds("insulation", "InsulationType", "HeatTracingType")},
            {"label": "起点", "key": "from", "value": from_val},
            {"label": "终点", "key": "to", "value": to_val},
        ]
    elif category == "valve":
        notes_text = _from_ds("notes", "relief_case", "service")
        xml_fail, xml_act_op = _xml_valve_actuator_info(sheet, element)
        fail_position = xml_fail or fail_position or attributes.get("FailAction") or attributes.get("FailPosition")
        if not fail_position and notes_text:
            m_fail = _FAIL_POS_RE.search(notes_text)
            if m_fail:
                code = m_fail.group(1)
                fail_position = _FAIL_ACTION_LABELS.get(code, code)
        actuator_op = xml_act_op or actuator_op or attributes.get("Operation") or attributes.get("ActuatorType")
        if not actuator_op and notes_text:
            if "电磁" in notes_text:
                actuator_op = "电磁驱动"
            elif "气动" in notes_text:
                actuator_op = "气动"
            elif "电动" in notes_text:
                actuator_op = "电动"
            elif "手动" in notes_text:
                actuator_op = "手动"
        def _format_set_pressure(raw: Any, is_barg: bool) -> str | None:
            if raw is None:
                return None
            if is_barg and isinstance(raw, (int, float)):
                v = float(raw)
                if abs(v) < 0.1 and v != 0:
                    sign = "+" if v > 0 else ""
                    return f"{sign}{v * 100:g} kPa(g) ({v:g} barg)"
                return f"{v:g} barg"
            s = _str_val(raw)
            if not s:
                return None
            if is_barg and not any(u in s.lower() for u in ("bar", "kpa", "mpa")):
                return f"{s} barg"
            return s

        pq_high = _extract_physical_quantity(element, "SetPressureHigh")
        pq_low = _extract_physical_quantity(element, "SetPressureLow")
        set_p = (
            _format_physical_quantity(*pq_high, include_barg_equiv=True)
            if pq_high is not None
            else (
                _format_set_pressure(li["set_pressure_barg"], True)
                if li.get("set_pressure_barg") is not None
                else _format_set_pressure(attributes.get("SetPressure") or ds.get("SetPressure"), False)
            )
        )
        vac_p = (
            _format_physical_quantity(*pq_low, include_barg_equiv=True)
            if pq_low is not None
            else (
                _format_set_pressure(li["vacuum_set_pressure_barg"], True)
                if li.get("vacuum_set_pressure_barg") is not None
                else _format_set_pressure(attributes.get("VacuumSetPressure") or ds.get("VacuumSetPressure"), False)
            )
        )
        pressure_rows = (
            [
                {"label": "正压整定压力", "key": "SetPressure", "value": set_p},
                {"label": "负压整定压力", "key": "VacuumSetPressure", "value": vac_p},
            ]
            if (dexpi_class == "BreatherValve" or vac_p is not None)
            else [{"label": "整定压力", "key": "SetPressure", "value": set_p}]
        )
        properties = [
            {"label": "位号", "key": "Tag", "value": tag},
            {"label": "阀门类型", "key": "DEXPI Class", "value": dexpi_class},
            {"label": "所在管线", "key": "LineNumber", "value": line_number},
            {"label": "公称通径", "key": "DN", "value": dn_fmt},
            {"label": "管道等级", "key": "PipingClassCode", "value": piping_class},
            {"label": "阀体材质", "key": "Material", "value": _from_ds("material", "MaterialOfConstructionCode") or spec_material or material_group},
            {"label": "连接形式", "key": "end_connection", "value": _from_ds("end_connection", "EndConnection")},
            {"label": "故障位", "key": "FailPosition", "value": fail_position},
            {"label": "驱动方式", "key": "Operation", "value": actuator_op},
            *pressure_rows,
            {"label": "备注", "key": "notes", "value": notes_text},
        ]
    elif category == "fitting":
        if dexpi_class == "PipeTee":
            tee_dns: list[str] = []
            if raw_dn:
                for part in re.split(r"[×xX/]", raw_dn):
                    p = part.strip()
                    if p and p not in tee_dns:
                        tee_dns.append(p)
            for _nb_id, seg, _sys_p in incoming_pipes.get(resolved_id, []) + outgoing_pipes.get(resolved_id, []):
                seg_dn = _extract_raw_dn(seg)
                if seg_dn:
                    for part in re.split(r"[×xX/]", seg_dn):
                        p = part.strip()
                        if p and p not in tee_dns:
                            tee_dns.append(p)
            if tee_dns:
                dn_fmt = " × ".join(p if p.upper().startswith("DN") else f"DN{p}" for p in tee_dns)
        properties = [
            {"label": "位号/标识", "key": "Tag", "value": tag or resolved_id},
            {"label": "管件类型", "key": "DEXPI Class", "value": dexpi_class},
            {"label": "所在管线", "key": "LineNumber", "value": line_number},
            {"label": "公称通径", "key": "DN", "value": dn_fmt},
            {"label": "管道等级", "key": "PipingClassCode", "value": piping_class},
            {"label": "材质", "key": "Material", "value": _from_ds("material", "MaterialOfConstructionCode") or spec_material or material_group},
            {"label": "备注", "key": "notes", "value": _from_ds("notes", "service")},
        ]
        if dexpi_class == "PipeTee":
            distinct_lines = list(dict.fromkeys(
                c["via_tag"].strip()
                for c in connections_list
                if isinstance(c.get("via_tag"), str) and c["via_tag"].strip()
            ))
            if len(distinct_lines) >= 2:
                properties.append({"label": "管道号分界", "key": "line_transition", "value": " ↔ ".join(distinct_lines)})
    elif category == "spec_break":
        up_cls = attributes.get("UpstreamClass")
        dn_cls = attributes.get("DownstreamClass")
        if not up_cls:
            for nb_id, seg, sys_p in incoming_pipes.get(resolved_id, []):
                nb = sheet.by_id.get(nb_id)
                up_cls = (
                    (data_value(nb, "PipingClassCode") if nb is not None else None)
                    or data_value(seg, "PipingClassCode")
                    or (data_value(sys_p, "PipingClassCode") if sys_p is not None else None)
                )
                if up_cls:
                    break
        if not dn_cls:
            for nb_id, seg, sys_p in outgoing_pipes.get(resolved_id, []):
                nb = sheet.by_id.get(nb_id)
                dn_cls = (
                    (data_value(nb, "PipingClassCode") if nb is not None else None)
                    or data_value(seg, "PipingClassCode")
                    or (data_value(sys_p, "PipingClassCode") if sys_p is not None else None)
                )
                if dn_cls:
                    break
        gov_cls = attributes.get("GoverningClass")
        if not gov_cls and up_cls and dn_cls:
            gov_cls = up_cls if _piping_class_rank(up_cls) >= _piping_class_rank(dn_cls) else dn_cls
        if gov_cls:
            boundary_valves: list[str] = []
            for _nb_id, _seg, _sys_p in incoming_pipes.get(resolved_id, []) + outgoing_pipes.get(resolved_id, []):
                nb = sheet.by_id.get(_nb_id)
                if nb is not None and _classify_pid_object(nb)[0] == "valve":
                    vtag = _clean_tag(sheet.tag(nb)) or data_value(nb, "PipingComponentNumber") or _nb_id
                    if vtag and vtag not in boundary_valves:
                        boundary_valves.append(vtag)
            if not boundary_valves:
                for c in connections_list:
                    oid = c.get("other_id")
                    nb = sheet.by_id.get(oid) if oid else None
                    if nb is not None and _classify_pid_object(nb)[0] == "valve":
                        vtag = c.get("other_tag") or oid
                        if vtag and vtag not in boundary_valves:
                            boundary_valves.append(vtag)
            if boundary_valves and "边界阀门" not in gov_cls:
                gov_cls = f"{gov_cls} (边界阀门: {', '.join(boundary_valves)})"
        change_note = attributes.get("ChangeNote") or _from_ds("notes")
        if not change_note and up_cls and dn_cls:
            _, up_mg = _derive_piping_class(up_cls)
            _, dn_mg = _derive_piping_class(dn_cls)
            change_note = f"{up_mg or up_cls} → {dn_mg or dn_cls}"
        properties = [
            {"label": "所在管线", "key": "LineNumber", "value": line_number},
            {"label": "上游等级", "key": "UpstreamClass", "value": up_cls},
            {"label": "下游等级", "key": "DownstreamClass", "value": dn_cls},
            {"label": "边界阀门适用等级", "key": "GoverningClass", "value": gov_cls},
            {"label": "材质/压力变化说明", "key": "ChangeNote", "value": change_note},
        ]
    elif category == "instrument":
        cat_letters = "".join(
            v
            for v in (
                attributes.get("ProcessInstrumentationFunctionCategory"),
                attributes.get("ProcessInstrumentationFunctions"),
            )
            if v
        ) or None
        inst_num = attributes.get("ProcessInstrumentationFunctionNumber")
        alarms_and_il = "; ".join([*alarm_parts, *interlock_parts]) or None
        raw_loc = attributes.get("Location") or _from_ds("location", "MountingLocation")
        loc_display = _LOCATION_LABELS.get(raw_loc, raw_loc) if raw_loc else None
        properties = [
            {"label": "位号", "key": "Tag", "value": tag},
            {"label": "功能分类", "key": "Category", "value": cat_letters},
            {"label": "顺序号", "key": "Number", "value": inst_num},
            {"label": "测量/用途", "key": "service", "value": _from_ds("service")},
            {"label": "安装位置", "key": "Location", "value": loc_display},
            {"label": "报警/联锁", "key": "Alarms / Interlock", "value": alarms_and_il},
            {"label": "所属回路", "key": "Loop", "value": ", ".join(loop_ids) if loop_ids else None},
        ]
    elif category == "connector":
        dir_label = "流入 (FlowIn)" if "FlowIn" in dexpi_class else "流出 (FlowOut)"
        properties = [
            {"label": "位号/图号", "key": "PipeConnectorNumber", "value": tag or resolved_id},
            {"label": "方向", "key": "Direction", "value": dir_label},
            {"label": "所在管线", "key": "LineNumber", "value": line_number},
            {"label": "公称通径", "key": "DN", "value": dn_fmt},
            {"label": "管道等级", "key": "PipingClassCode", "value": piping_class},
            {"label": "备注", "key": "notes", "value": _from_ds("notes")},
        ]
    elif category == "annotation":
        texts = [
            t.strip()
            for t in (
                data_value(sub, "Text")
                for sub in element.iter("Object")
                if sub.get("type") == "Core/Diagram.Text"
            )
            if isinstance(t, str) and t.strip()
        ]
        properties = [
            {"label": "标注标识", "key": "Tag", "value": tag or resolved_id},
            {"label": "类型", "key": "DEXPI Class", "value": "尺寸标注"},
            {"label": "标注内容", "key": "texts", "value": " / ".join(texts) or None},
        ]
    else:
        properties = [
            {"label": "标识", "key": "id", "value": resolved_id},
            {"label": "类型", "key": "DEXPI Class", "value": dexpi_class},
            *({"label": k, "key": k, "value": v} for k, v in attributes.items()),
        ]

    span = sheet.span.get(element)
    xml_lines = [span[0], span[1]] if span else ([element.sourceline, element.sourceline] if element.sourceline else None)

    position: list[float] | None = None
    for rg in sheet.drawings.get(resolved_id, []):
        for sub in rg.iter("Object"):
            if "NodePosition" in (sub.get("type") or "") or (sub.get("id") or "").endswith("_Pos"):
                for agg in sub.iter("AggregatedDataValue"):
                    if agg.get("type") == "Core/Diagram.Point":
                        coords: dict[str, float] = {}
                        for d_el in agg.findall("Data"):
                            prop = d_el.get("property")
                            if prop in ("X", "Y"):
                                raw_c = next((d_el.findtext(tag_n) for tag_n in ("Double", "Integer", "String") if d_el.findtext(tag_n)), None)
                                if raw_c is not None:
                                    try:
                                        coords[prop] = float(raw_c.strip())
                                    except ValueError:
                                        pass
                        if "X" in coords and "Y" in coords:
                            position = [round(coords["X"], 3), round(coords["Y"], 3)]
                            break
            if position is not None:
                break
        if position is not None:
            break

    return {
        "id": resolved_id,
        "dexpi_class": dexpi_class,
        "category": category,
        "category_label": category_label,
        "tag": tag,
        "owner": owner,
        "position": position,
        "attributes": attributes,
        "properties": properties,
        "list_info": list_info,
        "parts": parts,
        "connections": connections_list,
        "controls": controls,
        "xml_lines": xml_lines,
    }


def create_app(
    repo_root: Path,
    ui_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    run_checks_allowed: bool = True,
) -> FastAPI:
    """The FastAPI app serving the plant repository at (or above) *repo_root*.

    *ui_dir*: a built single-page app served at ``/`` (unknown paths fall back to its
    ``index.html``). *allowed_hosts*: accepted ``Host`` headers (default: the local ones;
    ``["*"]`` accepts any, which :func:`serve` never passes). *run_checks_allowed*: whether
    ``GET /api/session`` hands out the token and ``POST /api/checks/run`` runs the checks
    (:func:`serve` turns it off on a non-local bind without ``--allow-remote-checks``).
    """
    repo = Repo(Workspace.find(repo_root).root)
    import_pydexpi()
    ui = ui_dir.resolve() if ui_dir else None
    if ui is not None and not (ui / "index.html").is_file():
        raise FileNotFoundError(f"{ui} has no index.html")
    svg_cache = _LRU(128)
    sheet_cache = _LRU(32)
    diff_cache = _LRU(64)
    tags_cache = _LRU(128)
    process_cache = _LRU(16)
    lca_cache = _LRU(8)
    tea_cache = _LRU(8)
    estimate_cache = _LRU(8)
    simulation_cache = _LRU(8)
    reactions_cache = _LRU(8)
    datasheet_cache = _LRU(8)
    brightway_lock = threading.Lock()
    maturity_lock = threading.Lock()

    app = FastAPI(
        title="cheme-as-code", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json"
    )
    hosts = {name.lower() for name in allowed_hosts or LOCAL_HOSTS}
    token = secrets.token_urlsafe(32)
    check_run = threading.Lock()

    @app.middleware("http")
    async def _check_host(request: Request, call_next):
        host = request.headers.get("host", "")
        if "*" not in hosts and _host_name(host).lower() not in hosts:
            return JSONResponse({"error": "invalid Host header"}, status_code=400)
        if request.url.path.startswith("/api/"):
            refusal = _cross_site(request, host)
            if refusal:
                return JSONResponse({"error": refusal}, status_code=403)
        return await call_next(request)

    app.state.repo = repo
    app.state.svg_cache = svg_cache
    app.state.diff_cache = diff_cache

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": error.message}, status_code=error.status)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(error.detail)}, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def _internal_error(_: Request, error: Exception) -> JSONResponse:
        # Logged by the server; the client gets no internals.
        return JSONResponse({"error": "internal error"}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in error.errors()
        )
        return JSONResponse({"error": problems or "bad request"}, status_code=400)

    def _record(path: str) -> None:
        """Record *path* as read for ``cheme export-static`` (see :attr:`Repo.reads`)."""
        if repo.reads is not None:
            # not a repository path (ApiError): it names no file of the plant
            with contextlib.suppress(ApiError):
                repo.reads.add(clean_path(path))

    def _uncommitted(snapshot: Snapshot, paths: Iterable[str]) -> tuple[str, ...]:
        """The *paths* (sorted) with uncommitted changes in the working tree *snapshot*
        serves: none for a commit or outside git, every one when git cannot tell."""
        from chemeascode.foundation.gitinfo import dirty_paths

        if snapshot.commit is not None or not repo.is_git:
            return ()
        found = dirty_paths(repo.root)
        return tuple(p for p in sorted(paths) if found is None or p in found)

    def _small(snapshot: Snapshot, path: str) -> tuple[bytes, str] | None:
        """``read_with_id``, refusing (400) a file larger than :data:`MAX_FILE_BYTES`."""
        try:
            size = snapshot.size(path)
        except ApiError:  # e.g. a symlink out of the repository: the refusal names the file
            _record(path)
            raise
        if size is None:
            return None
        _record(path)  # refused below or not, the answer is built from (or names) the file
        if size > MAX_FILE_BYTES:
            raise ApiError(400, f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        return snapshot.read_with_id(path)

    def _simulation_input(snapshot: Snapshot, path: str) -> tuple[bytes, str] | None:
        """A declared simulation input, read whatever its size: only its fingerprint is used,
        as ``cheme process simulate`` hashes it, so :data:`MAX_FILE_BYTES` does not apply
        (``None`` when absent or not served, e.g. a symlink out of the repository)."""
        try:
            found = snapshot.read_with_id(path)
        except ApiError:
            found = None
        _record(path)
        return found

    def _need(snapshot: Snapshot, path: str) -> tuple[bytes, str]:
        found = _small(snapshot, path)
        if found is None:
            raise ApiError(404, f"{path} does not exist at {snapshot.label}")
        return found

    # -- repository -------------------------------------------------------------

    def _has_layout(snapshot: Snapshot) -> bool:
        from chemeascode.foundation.workspace import LAYOUT_SUPPORT

        for kind, path in LAYOUT_FILES.items():
            if kind in LAYOUT_SUPPORT:
                continue
            try:
                if snapshot.size(path) is not None:
                    return True
            except ApiError:  # e.g. a symlink out of the repository: not served
                continue
        return False

    def _exists(snapshot: Snapshot, path: str) -> bool:
        try:
            return snapshot.size(path) is not None
        except ApiError:  # e.g. a symlink out of the repository: not served
            return False

    def _has_control(snapshot: Snapshot) -> bool:
        """Loops, sequences, or a narrative or philosophy changed from its template:
        ``GET /api/control`` has something to show."""
        from chemeascode.domains.control.report import CONTROL_FILES, has_control_texts

        texts = {}
        for kind, path in CONTROL_FILES.items():
            if kind == "interlocks":
                continue
            try:
                texts[path] = _text(snapshot, path)
            except ApiError:
                texts[path] = None
        return has_control_texts(texts)

    def _has_equipment(snapshot: Snapshot) -> bool:
        """The equipment list lists items (or does not load: the estimate then says why)."""
        try:
            found = _small(snapshot, LIST_FILES["equipment"])
        except ApiError:  # too large, or a symlink out of the repository: not served
            return False
        if found is None:
            return False
        try:
            data = yaml.safe_load(found[0])
        except yaml.YAMLError:
            return True
        return isinstance(data, dict) and bool(data.get("equipment"))

    def _datasheet_files(snapshot: Snapshot, suffixes: tuple[str, ...]) -> list[str]:
        """The files of ``equipment/datasheets/`` with one of *suffixes*, sorted."""
        from chemeascode.domains.equipment.datasheets import DATASHEET_DIR

        return _folder_files(snapshot, DATASHEET_DIR, suffixes)

    def _folder_files(snapshot: Snapshot, directory: str, suffixes: tuple[str, ...]) -> list[str]:
        """The files of the folder *directory* (not below it) with one of *suffixes*, sorted."""
        if snapshot.commit is None:  # the folder only, not a walk of the working tree
            folder = repo.root / directory
            names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
            candidates = [
                path
                for name in names
                if PurePosixPath(name).suffix in suffixes
                and _exists(snapshot, path := f"{directory}/{name}")
            ]
        else:
            candidates = sorted(
                entry.path
                for entry in repo.tree(snapshot.commit).values()
                if entry.type == "file" and entry.mode not in ("120000", "160000")
            )
        return [
            path
            for path in candidates
            if PurePosixPath(path).parent.as_posix() == directory
            and PurePosixPath(path).suffix in suffixes
        ]

    @app.get("/api/repo")
    def get_repo(rev: str | None = None) -> dict[str, Any]:
        snapshot = repo.snapshot(rev)
        config = snapshot.config()
        branch = None
        if repo.is_git:
            branch = repo.git("branch", "--show-current").decode().strip() or None
        dirty = bool(repo.is_git and repo.git("status", "--porcelain=v1", "-z").strip(b"\0"))
        return {
            "name": config.name,
            "root_name": repo.root.name,
            "branch": branch,
            "head": snapshot.commit or repo.head(),
            "stage": config.stage,
            "stages": [stage.model_dump() for stage in config.stages],
            "sheets": snapshot.sheets(config),
            "dirty": dirty,
            "has_layout": _has_layout(snapshot),
            "has_lca": _exists(snapshot, "lca/lca.yaml"),
            "has_tea": _exists(snapshot, LIST_FILES["tea"]),
            "has_equipment_estimate": _has_equipment(snapshot),
            "has_simulation": _exists(snapshot, "process/simulation.yaml"),
            "has_reactions": _exists(snapshot, REACTIONS_FILE),
            "has_datasheets": bool(_datasheet_files(snapshot, (".yaml", ".yml"))),
            "has_kinetic_data": bool(_folder_files(snapshot, KINETICS_DIR, (".yaml", ".yml"))),
            "has_control": _has_control(snapshot),
            "has_issues": config.issues is not None,
        }

    @app.get("/api/branches")
    def get_branches() -> list[dict[str, Any]]:
        if not repo.is_git:
            return []
        raw = repo.git(
            "for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(HEAD)", "refs/heads"
        )
        branches = []
        for line in raw.decode().splitlines():
            name, head, current = line.split("\0")
            branches.append({"name": name, "head": head, "current": current == "*"})
        return branches

    @app.get("/api/log")
    def get_log(
        limit: int = Query(50, ge=1, le=1000), path: str | None = None, rev: str | None = None
    ) -> list[dict[str, str]]:
        commit = repo.resolve(rev) if rev else repo.head()
        if commit is None:
            return []
        args = ["log", f"--max-count={limit}", "-z", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s"]
        args += [commit, "--"]
        if path:
            args.append(f":(literal){clean_path(path)}")
        elif repo.prefix:
            args.append(".")  # the plant is a subdirectory: only commits that touch it
        entries = []
        for record in repo.git(*args).decode(errors="replace").split("\0"):
            if not record.strip():
                continue
            sha, short, author, date, subject = record.strip("\n").split("\x1f", 4)
            entries.append(
                {"sha": sha, "short": short, "author": author, "date": date, "subject": subject}
            )
        return entries

    @app.get("/api/tree")
    def get_tree(rev: str | None = None) -> list[dict[str, str]]:
        snapshot = repo.snapshot(rev)
        try:
            sheet_files = {s["file"] for s in snapshot.sheets(snapshot.config())}
        except ApiError:
            sheet_files = set()
        return [
            {
                "path": e.path,
                "type": e.type,
                "kind": file_kind(e.path, sheet_files) if e.type == "file" else "other",
            }
            for e in snapshot.entries()
        ]

    # -- files ---------------------------------------------------------------------

    @app.get("/api/file")
    def get_file(path: str, rev: str | None = None) -> Response:
        snapshot = repo.snapshot(rev)
        size = snapshot.size(path)
        if size is None:
            raise ApiError(404, f"{path} does not exist at {snapshot.label}")
        if size > MAX_FILE_BYTES:
            raise ApiError(400, f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        content = snapshot.read(path) or b""
        if b"\0" in content[:8192]:
            raise ApiError(400, f"{path} is a binary file")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(400, f"{path} is a binary file (not UTF-8)") from None
        return PlainTextResponse(text, headers={"X-Content-Type-Options": "nosniff"})

    def _svg(path: str, content: bytes, sha: str) -> str:
        def render() -> tuple[bool, str]:
            try:
                return True, render_svg(load_model(content))
            except Exception as failure:  # noqa: BLE001 - any load or drawing failure is the file's problem
                return False, f"{path}: {type(failure).__name__}: {failure}".splitlines()[0]

        ok, value = svg_cache.get_or_compute((path, sha), render)
        if not ok:
            raise ApiError(422, value)
        return value

    @app.get("/api/pid.svg")
    def get_pid_svg(path: str, rev: str | None = None) -> Response:
        snapshot = repo.snapshot(rev)
        content, sha = _need(snapshot, path)
        return Response(_svg(path, content, sha), media_type="image/svg+xml", headers=_SVG_HEADERS)

    @app.get("/api/pid/object")
    def get_pid_object(path: str, id: str, rev: str | None = None) -> dict[str, Any]:
        """Inspect one object on a P&ID sheet: standard category properties, YAML list metadata,
        clickable upstream/downstream connections, nozzles/inline parts, and control/interlock
        records. Fast XML-only lookup cached by blob id."""
        snapshot = repo.snapshot(rev)
        content, sha = _need(snapshot, path)
        sheet: Sheet = sheet_cache.get_or_compute(
            (path, sha), lambda: _sheet_from_bytes(content, path)
        )
        lists = {
            kind: _safe_yaml_dict(snapshot, _small, LIST_FILES[kind])
            for kind in ("equipment", "lines", "piping_items", "instruments", "loops", "interlocks")
        }
        pipe_specs = _safe_yaml_dict(snapshot, _small, LAYOUT_FILES["pipe_specs"])
        if pipe_specs:
            lists["pipe_specs"] = pipe_specs
        # Optional equipment datasheet enrichment if an equipment tag matches
        datasheet: dict[str, Any] | None = None
        el = _resolve_pid_element(sheet, id)
        if is_equipment(el):
            eq_tag = _clean_tag(sheet.tag(el))
            if eq_tag and "/" not in eq_tag and "\\" not in eq_tag and ".." not in eq_tag:
                for ext in (".yaml", ".yml"):
                    ds_path = f"equipment/datasheets/{eq_tag}{ext}"
                    ds_data = _safe_yaml_dict(snapshot, _small, ds_path)
                    if ds_data:
                        datasheet = ds_data
                        break
        return _inspect_pid_object(sheet, id, lists, datasheet=datasheet)

    @app.get("/api/diff")
    def get_diff(path: str, old: str = "HEAD", new: str | None = None) -> dict[str, Any]:
        clean_path(path)
        old = old or "HEAD"  # an empty old= means the default, never the working tree
        old_snapshot = repo.snapshot(old)
        new_snapshot = repo.snapshot(new)
        old_found = _small(old_snapshot, path)
        new_found = _small(new_snapshot, path)
        if old_found is None and new_found is None:
            raise ApiError(404, f"{path} exists in neither revision")
        new_label = new or WORKING_TREE
        key = (
            path,
            old_found[1] if old_found else None,
            new_found[1] if new_found else None,
            old,
            new_label,
        )

        def compute() -> tuple[bool, Any]:
            try:
                result = diff_sheet(
                    old_found[0] if old_found else None,
                    new_found[0] if new_found else None,
                    path,
                    old,
                    new_label,
                )
            except Exception as failure:  # noqa: BLE001 - a sheet pyDEXPI cannot load
                return False, f"{path}: {type(failure).__name__}: {failure}".splitlines()[0]
            if result.error:  # a side that does not load or draw: 422, as before
                return False, f"{path}: {result.error}"
            return True, asdict(result)

        ok, value = diff_cache.get_or_compute(key, compute)
        if not ok:
            raise ApiError(422, value)
        return value

    @app.get("/api/yaml")
    def get_yaml(path: str, rev: str | None = None) -> dict[str, Any]:
        if PurePosixPath(clean_path(path)).suffix.lower() not in (".yaml", ".yml"):
            raise ApiError(422, f"{path} is not a YAML file (.yaml or .yml)")
        snapshot = repo.snapshot(rev)
        content, _ = _need(snapshot, path)
        try:
            data = json_safe(yaml.safe_load(content))
        except yaml.YAMLError as failure:
            raise ApiError(422, f"{path}: {_first_line(failure)}") from failure
        except ApiError as failure:
            raise ApiError(failure.status, f"{path}: {failure.message}") from None
        if data is not None and not isinstance(data, (dict, list)):
            raise ApiError(
                422, f"{path} holds a single {type(data).__name__}, not a mapping or list"
            )
        if path == PLANT_FILE:
            kind, model = "plant", PlantConfig
        elif path in _PROCESS_SCHEMAS:
            kind, model = _PROCESS_SCHEMAS[path]
        elif path in _KIND_BY_LAYOUT_PATH:
            kind = _KIND_BY_LAYOUT_PATH[path]
            model = LAYOUT_SCHEMAS[kind]
        elif path == LAYOUT_MODEL_FILE:
            kind, model = "layout_model", LayoutModelFile
        else:
            kind = _KIND_BY_LIST_PATH.get(path)
            model = LIST_SCHEMAS.get(kind) if kind else None
        schema = model.model_json_schema(by_alias=True) if model else None
        return {"data": data, "kind": kind, "schema": schema}

    # -- layout ---------------------------------------------------------------------

    def _text(snapshot: Snapshot, path: str) -> str | None:
        found = _small(snapshot, path)
        if found is None:
            return None
        try:
            return found[0].decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(422, f"{path} is not UTF-8 text") from None

    def _list_model(snapshot: Snapshot, kind: str) -> Any:
        """The parsed list of *kind* at *snapshot*, or None (absent or invalid)."""
        try:
            text = _text(snapshot, LIST_FILES[kind])
        except ApiError:
            return None
        if text is None:
            return None
        try:
            return LIST_SCHEMAS[kind].model_validate(yaml.safe_load(text) or {})
        except (ValidationError, yaml.YAMLError):
            return None

    def _drawn_tags(snapshot: Snapshot, files: list[str]) -> set[str]:
        """The tags drawn on the sheets (not line numbers), cached per file content."""
        from chemeascode.domains.piping.sheet_query import sheet_items

        tags: set[str] = set()
        for path in files:
            try:
                found = _small(snapshot, path)
            except ApiError:
                continue
            if found is None:
                continue
            content, sha = found

            def compute(content: bytes = content, path: str = path) -> frozenset[str]:
                try:
                    items = sheet_items(load_model(content), path)
                except Exception:  # noqa: BLE001 - an unloadable sheet draws nothing here; validate says why
                    return frozenset()
                return frozenset(i.tag.strip() for i in items if i.tag and i.kind != "line")

            tags |= tags_cache.get_or_compute((path, sha), compute)
        return tags

    def _nozzle_names(snapshot: Snapshot, files: list[str]) -> dict[str, set[str]]:
        """The nozzle names of each equipment tag on the sheets, cached per file content."""
        from chemeascode.domains.spatial.pid_nozzles import sheet_equipment

        found: dict[str, set[str]] = {}
        for path in files:
            try:
                small = _small(snapshot, path)
            except ApiError:
                continue
            if small is None:
                continue
            content, sha = small

            def compute(content: bytes = content, path: str = path) -> dict[str, frozenset]:
                try:
                    items = sheet_equipment(load_model(content), path)
                except Exception:  # noqa: BLE001 - an unloadable sheet draws nothing here; validate says why
                    return {}
                return {tag: item.names for tag, item in items.items()}

            for tag, names in tags_cache.get_or_compute(("nozzles", path, sha), compute).items():
                found.setdefault(tag, set(names))
        return found

    @app.get("/api/layout")
    def get_layout(rev: str | None = None) -> dict[str, Any]:
        snapshot = repo.snapshot(rev)
        texts = {}
        unreadable: dict[str, tuple[str, str]] = {}
        failures: list[ApiError] = []
        for kind, path in LAYOUT_FILES.items():
            try:
                text = _text(snapshot, path)
            except ApiError as failure:  # one bad file does not hide the others
                failures.append(failure)
                unreadable[kind] = (path, failure.message)
                continue
            if text is not None:
                texts[kind] = (path, text)
        if not texts:
            if failures:
                raise failures[0]
            raise ApiError(404, f"no layout files (layout/*.yaml) at {snapshot.label}")
        equipment_path = LIST_FILES["equipment"]
        equipment_text = _text(snapshot, equipment_path)
        equipment = None
        if equipment_text is not None:
            try:
                equipment = EquipmentList.model_validate(yaml.safe_load(equipment_text) or {})
            except (ValidationError, yaml.YAMLError):
                equipment = None
        piping_items = _list_model(snapshot, "piping_items")
        lines = _list_model(snapshot, "lines")
        lines_text = _text(snapshot, LIST_FILES["lines"]) if lines is not None else None
        try:
            listed_sheets = snapshot.sheets(snapshot.config())
        except ApiError:
            listed_sheets = []
        sheet_files = {s["id"]: s["file"] for s in listed_sheets}
        drawn = _drawn_tags(snapshot, list(sheet_files.values())) if lines is not None else None
        pid_files = [s["file"] for s in listed_sheets if s.get("kind", "pid") == "pid"]
        nozzles = _nozzle_names(snapshot, pid_files) if pid_files else None
        layout = check_layout(
            texts,
            equipment,
            (equipment_path, equipment_text) if equipment_text else None,
            unreadable,
            piping_items=piping_items,
            lines=lines,
            lines_text=(LIST_FILES["lines"], lines_text) if lines_text else None,
            drawn_tags=drawn,
            nozzles=nozzles,
        )
        refs = layout.clash_refs
        listed: dict[str, Any] = (
            {item.tag: item for item in equipment.equipment} if equipment else {}
        )
        for special in piping_items.piping_items if piping_items else []:
            listed.setdefault(special.tag, special)
        plan = layout.site_plan
        placed = []
        for index, item in enumerate(layout.placement.equipment if layout.placement else []):
            resolved = layout.resolved[index]
            entry = listed.get(item.tag)
            sheet = entry.sheet if entry else None
            service = ""
            if entry is not None:
                service = entry.service if hasattr(entry, "service") else entry.notes
            placed.append(
                {
                    "tag": item.tag,
                    "type": entry.type if entry else None,
                    "service": service,
                    "position": list(item.position),
                    "shape": resolved.body.shape,
                    "family": resolved.body.family,
                    "size": list(resolved.body.extent),
                    "size_from": resolved.size_from,
                    "rotation_deg": item.rotation_deg,
                    "mount": item.mount.model_dump(exclude_none=True) if item.mount else None,
                    "building": item.building,
                    "level": item.level,
                    "sheet": sheet,
                    "sheet_file": sheet_files.get(sheet) if sheet else None,
                    "bbox": list(layout.box(index)),
                    "clash": item.tag in layout.clashing,
                    "notes": item.notes,
                }
            )
        buildings = []
        for building in layout.buildings.buildings if layout.buildings else []:
            clear = {level.id: height for level, height, _ in level_heights(building)}
            dumped = building.model_dump()
            for level in dumped["levels"]:
                level["clear_height"] = clear[level["id"]]
            buildings.append(
                {
                    **dumped,
                    "footprint": list(building_footprint(building)),
                    "heights": list(building_heights(building)),
                    "clash": ("building", building.id) in refs,
                }
            )
        notes = [
            model.notes
            for model in (plan, layout.buildings, layout.placement)
            if model is not None and model.notes
        ]
        model = _layout_model(snapshot)
        model_findings = []
        if model is not None:
            from chemeascode.domains.spatial.model_rules import model_rules, zone_findings

            try:
                found = zone_findings(model, layout.zones) + model_rules(
                    model, layout.routing, plan
                )
            except (KeyError, TypeError, ValueError):  # a model of another shape: validate says
                found = []
            model_findings = [asdict(finding) for finding in found]
        return {
            "rev": snapshot.commit,
            "files": {kind: texts[kind][0] for kind in texts},
            "site": plan.site.model_dump() if plan else None,
            "areas": [
                {**area.model_dump(), "clash": area.kind == "road" and ("road", area.id) in refs}
                for area in (plan.areas if plan else [])
            ],
            "roads": [
                {**road.model_dump(), "clash": ("road", road.id) in refs}
                for road in (plan.roads if plan else [])
            ],
            "racks": [
                {**rack.model_dump(), "clash": ("rack", rack.id) in refs}
                for rack in (plan.racks if plan else [])
            ],
            "buildings": buildings,
            "equipment": placed,
            "notes": notes,
            "lines": [route.as_dict() for route in layout.lines],
            "separations": [
                rule.model_dump(by_alias=True, exclude_none=True)
                for rule in (plan.separations if plan else [])
            ],
            "model": model,
            "zones": (model or {}).get("zones", []),
            "model_findings": model_findings,
            "findings": [asdict(finding) for finding in layout.findings],
        }

    def _layout_model(snapshot: Snapshot) -> dict[str, Any] | None:
        """The generated ``layout/plant-model.yaml`` at *snapshot*, as data; None without one
        (or when it is not a YAML mapping: validate reports it)."""
        from chemeascode.domains.spatial.model import read

        try:
            text = _text(snapshot, LAYOUT_MODEL_FILE)
        except ApiError:
            return None
        return read(text) if text is not None else None

    def _drawing_meta(snapshot: Snapshot) -> dict[str, str]:
        try:
            config = snapshot.config()
        except ApiError:
            return {}
        meta = {"plant": config.name}
        if config.project is not None:
            project = config.project
            meta.update(
                project=" ".join(v for v in (project.number, project.name) if v),
                client=project.client,
                site=project.site,
                revision=project.revision,
            )
        return meta

    def _model_or_404(snapshot: Snapshot) -> dict[str, Any]:
        model = _layout_model(snapshot)
        if model is None:
            raise ApiError(404, f"no {LAYOUT_MODEL_FILE} at {snapshot.label}")
        return model

    @app.get("/api/layout/plan.svg")
    def get_layout_plan(
        rev: str | None = None, level: str | None = None, z: str | None = None
    ) -> Response:
        """The plot plan, or a level plan (``level=`` id, or ``z=a,b``), of the committed
        model at *rev*."""
        from chemeascode.domains.spatial.drawing2d import level_range, render_plan

        snapshot = repo.snapshot(rev)
        model = _model_or_404(snapshot)
        cut = None
        if level is not None:
            cut = level_range(model, level)
            if cut is None:
                raise ApiError(404, f"no structure or building level {level}")
        elif z is not None:
            try:
                low, high = (float(v) for v in z.split(","))
            except ValueError:
                raise ApiError(400, "z: give two numbers a,b") from None
            cut = (low, high)
        svg = render_plan(model, _drawing_meta(snapshot), cut)
        return Response(svg, media_type="image/svg+xml", headers=_SVG_HEADERS)

    @app.get("/api/mto")
    def get_mto(rev: str | None = None) -> dict[str, Any]:
        """The material take-off of the committed model at *rev* (``cheme assess mto
        --json`` without the source), and its estimate when ``estimate/rates.yaml`` exists."""
        from chemeascode.domains.assessment.estimate import (
            RATES_FILE,
            estimate_payload,
            texts_estimate,
        )
        from chemeascode.domains.assessment.mto import model_takeoff, mto_payload

        snapshot = repo.snapshot(rev)
        text = _text(snapshot, LAYOUT_MODEL_FILE)
        if text is None:
            raise ApiError(404, f"no {LAYOUT_MODEL_FILE} at {snapshot.label}")
        takeoff = model_takeoff(text)
        if any(f.level == "error" for f in takeoff.findings):
            raise ApiError(422, takeoff.findings[0].message)
        payload: dict[str, Any] = {"rev": snapshot.commit, **mto_payload(takeoff)}
        try:
            rates = _text(snapshot, RATES_FILE)
        except ApiError:
            rates = None
        payload["estimate"] = (
            estimate_payload(texts_estimate(text, rates)) if rates is not None else None
        )
        return payload

    @app.get("/api/estimate/equipment")
    def get_equipment_estimate(rev: str | None = None) -> dict[str, Any]:
        """The equipment estimate at *rev* (``cheme assess equipment --json``): the equipment
        list costed from the published correlations, with ``estimate/cost-basis.yaml`` and the
        datasheets. 404 without an equipment list or with no items; 422 for a list that does
        not load."""
        from chemeascode.domains.assessment.equipment_cost import (
            BASIS_FILE,
            CATALYST_INPUTS,
            EQUIPMENT_FILE,
            catalyst_fills_of,
            estimate_equipment,
            load_basis,
        )
        from chemeascode.domains.equipment.datasheets import DATASHEET_DIR
        from chemeascode.domains.equipment.schemas.datasheet import Datasheet
        from chemeascode.foundation.provenance import SourceRevision, content_fingerprint

        snapshot = repo.snapshot(rev)
        found = _small(snapshot, EQUIPMENT_FILE)
        if found is None:
            raise ApiError(404, f"no {EQUIPMENT_FILE} at {snapshot.label}")
        texts = {EQUIPMENT_FILE: found}
        sheet_paths = _datasheet_files(snapshot, (".yaml", ".yml"))
        for path in [BASIS_FILE, *CATALYST_INPUTS, *sheet_paths]:
            try:
                optional = _small(snapshot, path)
            except ApiError:
                continue  # an optional input that cannot be served counts as absent
            if optional is not None:
                texts[path] = optional

        def compute() -> tuple[dict[str, Any] | None, int, str]:
            try:
                equipment = LIST_SCHEMAS["equipment"].model_validate(
                    yaml.safe_load(texts[EQUIPMENT_FILE][0]) or {}
                )
            except (ValidationError, yaml.YAMLError) as failure:
                return None, 422, f"{EQUIPMENT_FILE}: {_first_line(failure)}"
            if not equipment.equipment:
                return None, 404, f"nothing to estimate: no items in {EQUIPMENT_FILE}"
            basis_text = texts.get(BASIS_FILE)
            basis, problems = load_basis(
                basis_text[0].decode("utf-8", errors="replace") if basis_text else None
            )
            sheets = {}
            for path, (content, _) in texts.items():
                if not path.startswith(DATASHEET_DIR + "/"):
                    continue
                try:
                    sheet = Datasheet.model_validate(yaml.safe_load(content) or {})
                except (ValidationError, yaml.YAMLError):
                    continue  # cheme validate reports it; the CLI leaves it out too
                sheets[sheet.tag.strip()] = sheet
            result = estimate_equipment(equipment, sheets, basis)
            # the reactors' first catalyst fills, as cheme assess equipment adds them
            try:
                process = _process(snapshot) if FLOWSHEET_FILE in texts else None
            except ApiError:
                process = None  # a process that does not load costs no catalyst
            lines, found = catalyst_fills_of(process, sheets, result.index)
            result.lines += lines
            result.findings = problems + result.findings + found
            return result.payload(), 200, ""

        key = tuple((path, sha) for path, (_, sha) in sorted(texts.items()))
        payload, status, problem = estimate_cache.get_or_compute(key, compute)
        if payload is None:
            raise ApiError(status, problem)
        # The source of these inputs as served (the cached payload is not changed).
        source = SourceRevision(
            commit=snapshot.commit or repo.head(),
            dirty=_uncommitted(snapshot, {EQUIPMENT_FILE, BASIS_FILE, *texts}),
            fingerprints={path: content_fingerprint(c) for path, (c, _) in sorted(texts.items())},
        )
        return {"rev": snapshot.commit, **payload, "source": source.as_dict()}

    # -- datasheets ------------------------------------------------------------------------

    def _datasheet_report(snapshot: Snapshot) -> tuple[Any, dict[str, Any]]:
        """``project.datasheets.datasheet_report`` of *snapshot* (computed on a private copy
        of its inputs, cached by their blob ids) and the source of the inputs as served."""
        from chemeascode.foundation.provenance import SourceRevision, content_fingerprint
        from chemeascode.project.datasheets import datasheet_report

        files = _datasheet_files(snapshot, (".yaml", ".yml", ".svg"))
        if not any(PurePosixPath(f).suffix in (".yaml", ".yml") for f in files):
            raise ApiError(404, f"no datasheets in equipment/datasheets at {snapshot.label}")
        texts: dict[str, tuple[bytes, str]] = {}
        try:
            sheets = [s["file"] for s in snapshot.sheets(snapshot.config()) if s.get("file")]
        except ApiError:
            sheets = []
        for path in (
            PLANT_FILE,
            LIST_FILES["equipment"],
            LIST_FILES["lines"],
            LAYOUT_MODEL_FILE,
            *sheets,
            *files,
        ):
            try:
                found = _small(snapshot, path)
            except ApiError:
                continue  # an input that cannot be served counts as absent
            if found is not None:
                texts[path] = found

        def compute() -> Any:
            # The report reads files: a private temporary copy, never the repository.
            with tempfile.TemporaryDirectory() as tmp:
                for path, (content, _) in texts.items():
                    target = Path(tmp, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                return datasheet_report(Path(tmp))

        key = tuple((path, sha) for path, (_, sha) in sorted(texts.items()))
        report = datasheet_cache.get_or_compute(key, compute)
        source = SourceRevision(
            commit=snapshot.commit or repo.head(),
            dirty=_uncommitted(snapshot, texts),
            fingerprints={path: content_fingerprint(c) for path, (c, _) in sorted(texts.items())},
        )
        return report, source.as_dict()

    def _datasheet_entry(snapshot: Snapshot, tag: str) -> tuple[Any, Any, dict[str, Any]]:
        report, source = _datasheet_report(snapshot)
        entry = report.entry(tag)
        if entry is None:
            raise ApiError(404, f"no datasheet of {tag} at {snapshot.label}")
        return report, entry, source

    @app.get("/api/datasheets")
    def get_datasheets(rev: str | None = None) -> dict[str, Any]:
        """The datasheets at *rev*: per datasheet its tag, class, service, draft, the counts of
        assumed, TBD, supplier and not_applicable fields, the outline drawing's state and the
        findings by level; the listed items without a datasheet. 404 without datasheets."""
        snapshot = repo.snapshot(rev)
        report, source = _datasheet_report(snapshot)
        return {"rev": snapshot.commit, **report.index(), "source": source}

    @app.get("/api/datasheets/{tag}.svg")
    def get_datasheet_drawing(
        tag: str, rev: str | None = None, generated: bool = False
    ) -> Response:
        """The outline drawing of *tag* at *rev*: the committed
        ``equipment/datasheets/<TAG>.svg``, or (when there is none, or with ``generated=true``)
        the one ``cheme datasheets drawing`` would write. ``X-Cheme-Drawing`` says which
        (``committed`` or ``generated``), ``X-Cheme-Drawing-Current`` whether the committed one
        is what the datasheet gives now."""
        snapshot = repo.snapshot(rev)
        _, entry, _ = _datasheet_entry(snapshot, tag)
        committed = None
        if not generated:
            try:
                committed = _small(snapshot, clean_path(entry.drawing))
            except ApiError:
                committed = None
        headers = dict(_SVG_HEADERS)
        if committed is not None:
            headers["X-Cheme-Drawing"] = "committed"
            headers["X-Cheme-Drawing-Current"] = str(entry.drawing_state == "current").lower()
            return Response(committed[0], media_type="image/svg+xml", headers=headers)
        if entry.drawing_text is None:
            raise ApiError(422, f"{entry.file} does not validate: no drawing (cheme validate)")
        headers["X-Cheme-Drawing"] = "generated"
        return Response(entry.drawing_text, media_type="image/svg+xml", headers=headers)

    @app.get("/api/datasheets/{tag}")
    def get_datasheet(tag: str, rev: str | None = None) -> dict[str, Any]:
        """One datasheet at *rev*: the file as data, the parsed datasheet (None when it does not
        validate), its summary as in ``/api/datasheets`` and the findings on it and its
        drawing (schema, markers, against the equipment list, the layout model, the P&IDs and
        the line list, drawing drift)."""
        snapshot = repo.snapshot(rev)
        _, entry, source = _datasheet_entry(snapshot, tag)
        return {
            "rev": snapshot.commit,
            "tag": entry.tag,
            "file": entry.file,
            "summary": entry.summary(),
            "datasheet": entry.sheet.model_dump(mode="json") if entry.sheet else None,
            "data": json_safe(entry.data) if entry.data is not None else None,
            "drawing": {
                "file": entry.drawing,
                "state": entry.drawing_state,
                "current": entry.drawing_state == "current",
            },
            "findings": [{**asdict(f), "id": f.id} for f in entry.findings],
            "source": source,
        }

    @app.get("/api/lca")
    def get_lca(rev: str | None = None) -> dict[str, Any]:
        """The LCA at *rev* (``cheme assess lca --json`` without the source): inventory,
        impacts by scenario, category, phase and contributor, and the findings. 404 without
        ``lca/lca.yaml``; 422 when the LCA does not compute (its first error)."""
        from chemeascode.domains.assessment.lca.inventory import LCA_FILE
        from chemeascode.domains.assessment.lca.report import (
            REPORT_INPUTS,
            dataset_paths,
            lca_report,
        )

        snapshot = repo.snapshot(rev)
        for path in (LCA_FILE, PLANT_FILE):
            if not _exists(snapshot, path):
                raise ApiError(404, f"no {path} at {snapshot.label}")
        texts = {}
        for path in (PLANT_FILE, *REPORT_INPUTS):
            try:
                found = _small(snapshot, path)
            except ApiError:
                if path in (LCA_FILE, PLANT_FILE):
                    raise
                continue  # an optional input that cannot be served counts as absent
            if found is not None:
                texts[path] = found

        def read(path: str) -> str | None:
            try:
                found = _small(snapshot, clean_path(path))
            except ApiError:
                return None  # outside the repository or too large: the report says so
            if found is None:
                return None
            texts[clean_path(path)] = found
            return found[0].decode("utf-8", errors="replace")

        # the LCI datasets the study references (recursively), so the copy resolves them
        dataset_paths(texts[LCA_FILE][0].decode("utf-8", errors="replace"), read)

        def compute() -> tuple[dict[str, Any] | None, str]:
            # The report reads files: a private temporary copy, never the repository.
            with tempfile.TemporaryDirectory() as tmp:
                for path, (content, _) in texts.items():
                    target = Path(tmp, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                report = lca_report(Path(tmp))
            findings = [{**asdict(f), "id": f.id} for f in report.findings]
            if not report.loaded:
                first = next((f for f in report.findings if f.level == "error"), None)
                if first is None:
                    return None, f"{LCA_FILE}: does not compute"
                where = f"{first.file}:{first.line}" if first.line else first.file
                message = first.message
                return None, message if message.startswith(where) else f"{where}: {message}"
            return {**report.payload(), "findings": findings}, ""

        try:
            backend = (yaml.safe_load(texts[LCA_FILE][0]) or {}).get("backend")
        except (yaml.YAMLError, UnicodeDecodeError, AttributeError):
            backend = None
        if backend == "brightway":
            # The impacts also depend on the Brightway project outside the repository: computed
            # every time (no cache), one at a time (bw2data's current project is global).
            with brightway_lock:
                payload, problem = compute()
        else:
            key = tuple((path, sha) for path, (_, sha) in sorted(texts.items()))
            payload, problem = lca_cache.get_or_compute(key, compute)
        if payload is None:
            raise ApiError(422, problem)
        return {"rev": snapshot.commit, **payload}

    @app.get("/api/tea")
    def get_tea(rev: str | None = None) -> dict[str, Any]:
        """The TEA at *rev* (``cheme assess tea --json`` without the source): premises, capital
        cost, flows, cash flow, results, scenarios, tornado, break-even values, the Monte Carlo
        and the findings. 404 without ``estimate/tea.yaml``; 422 when the TEA does not compute
        (its first error)."""
        from chemeascode.domains.assessment.tea.report import REPORT_INPUTS, TEA_FILE, tea_report

        snapshot = repo.snapshot(rev)
        for path in (TEA_FILE, PLANT_FILE):
            if not _exists(snapshot, path):
                raise ApiError(404, f"no {path} at {snapshot.label}")
        texts = {}
        for path in (*REPORT_INPUTS, *_datasheet_files(snapshot, (".yaml", ".yml"))):
            try:
                found = _small(snapshot, path)
            except ApiError:
                if path in (TEA_FILE, PLANT_FILE):
                    raise
                continue  # an optional input that cannot be served counts as absent
            if found is not None:
                texts[path] = found

        def compute() -> tuple[dict[str, Any] | None, str]:
            # The report reads files: a private temporary copy, never the repository.
            with tempfile.TemporaryDirectory() as tmp:
                for path, (content, _) in texts.items():
                    target = Path(tmp, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                report = tea_report(Path(tmp))
            findings = [{**asdict(f), "id": f.id} for f in report.findings]
            if not report.loaded:
                first = next((f for f in report.findings if f.level == "error"), None)
                if first is None:
                    return None, f"{TEA_FILE}: does not compute"
                where = f"{first.file}:{first.line}" if first.line else first.file
                message = first.message
                return None, message if message.startswith(where) else f"{where}: {message}"
            return {**report.payload(), "findings": findings}, ""

        key = tuple((path, sha) for path, (_, sha) in sorted(texts.items()))
        payload, problem = tea_cache.get_or_compute(key, compute)
        if payload is None:
            raise ApiError(422, problem)
        return {"rev": snapshot.commit, **payload}

    @app.get("/api/layout/section.svg")
    def get_layout_section(
        rev: str | None = None,
        x: float | None = None,
        y: float | None = None,
        depth: float = 6.0,
    ) -> Response:
        """A section at ``x=`` or ``y=`` (``depth`` m thick) of the committed model at *rev*."""
        from chemeascode.domains.spatial.drawing2d import render_section

        if (x is None) == (y is None):
            raise ApiError(400, "give x= or y= (one of them)")
        if not 0 < depth <= 1000:
            raise ApiError(400, "depth must be between 0 and 1000 m")
        snapshot = repo.snapshot(rev)
        model = _model_or_404(snapshot)
        axis, at = ("x", x) if x is not None else ("y", y)
        svg = render_section(model, axis, float(at), depth, _drawing_meta(snapshot))  # type: ignore[arg-type]
        return Response(svg, media_type="image/svg+xml", headers=_SVG_HEADERS)

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        """``cheme status --json`` of the working tree, without running the gates (they may
        run commands); the gates are ``POST /api/checks/run``."""
        from chemeascode.project.status import plant_status

        try:
            return plant_status(repo.workspace, run_gates=False)
        except (ValidationError, yaml.YAMLError) as failure:
            raise ApiError(422, f"cannot summarize: {_first_line(failure)}") from failure

    @app.get("/api/impact")
    def get_impact() -> dict[str, Any]:
        """``cheme impact --json`` of the working tree: review-needed findings (info)."""
        from chemeascode.project.impact import impact_report

        return impact_report(repo.workspace)

    @app.get("/api/interlocks")
    def get_interlocks(rev: str | None = None) -> dict[str, Any]:
        from chemeascode.domains.control.interlocks import matrix

        snapshot = repo.snapshot(rev)
        path = LIST_FILES["interlocks"]
        text = _text(snapshot, path)
        if text is None:
            raise ApiError(404, f"no {path} at {snapshot.label}")
        try:
            model = LIST_SCHEMAS["interlocks"].model_validate(yaml.safe_load(text) or {})
        except (ValidationError, yaml.YAMLError) as failure:
            raise ApiError(422, f"{path}: {_first_line(failure)}") from failure
        return {
            "rev": snapshot.commit,
            "file": path,
            "interlocks": [i.model_dump(by_alias=True) for i in model.interlocks],
            **matrix(model),
        }

    @app.get("/api/issues")
    def get_issues() -> dict[str, Any]:
        """``cheme issues --json`` of the working tree's plant: its GitHub issues as engineering
        items (read-only, cached as for the gates: project.issues.CACHE_S); 404 when plant.yaml
        declares no issues, 503 when they cannot be read (no token, no network)."""
        from chemeascode.project.issues import IssuesUnavailable, issues_report

        if repo.workspace.config().issues is None:
            raise ApiError(404, "plant.yaml declares no issues")
        try:
            return issues_report(repo.workspace)
        except IssuesUnavailable as failure:
            raise ApiError(503, f"issues not read: {failure}") from failure

    @app.get("/api/control")
    def get_control() -> dict[str, Any]:
        """``cheme control report --json`` of the working tree: loops, sequences, the
        narrative and the control philosophy with their review status and findings; 404 when
        the plant has none of them (control documentation is optional)."""
        from chemeascode.domains.control.report import control_report

        try:
            report = control_report(repo.root)
        except (ValidationError, yaml.YAMLError, UnicodeDecodeError) as failure:
            raise ApiError(422, f"cannot report: {_first_line(failure)}") from failure
        if not report.present:
            raise ApiError(404, "no control records (loops, sequences, narrative, philosophy)")
        return json.loads(json.dumps(report.as_dict(), default=str))

    # -- process ---------------------------------------------------------------------

    def _worker_fingerprints(snapshot: Snapshot, sim_bytes: bytes) -> dict[str, str] | None:
        from chemeascode.domains.process.simulation.backends import declared_inputs

        names = declared_inputs(sim_bytes, workers_only=True)
        if not names:
            return None
        hashes = {}
        for path in names:
            _record(path)
            try:
                digest = snapshot.raw_fingerprint(path)
            except (ApiError, OSError):
                continue  # Missing/refused inputs make the model unavailable, never current.
            if digest is not None:
                hashes[path] = digest
        return hashes

    def _process(snapshot: Snapshot) -> Any:
        """``process/*.yaml`` of *snapshot*, loaded as ``cheme process`` loads them (422 when
        the flowsheet does not load), cached by blob ids."""
        from chemeascode.domains.process.model import load
        from chemeascode.domains.process.simulation import SIMULATION_FILE
        from chemeascode.domains.process.simulation.translate import prime

        texts = {}
        for path in (
            MATERIALS_FILE,
            FLOWSHEET_FILE,
            REACTIONS_FILE,
            CATALYSTS_FILE,
            SIMULATION_FILE,
        ):
            found = _small(snapshot, path)
            if found is not None:
                texts[path] = found
        worker_hashes = (
            _worker_fingerprints(snapshot, texts[SIMULATION_FILE][0])
            if SIMULATION_FILE in texts
            else None
        )
        if SIMULATION_FILE in texts and worker_hashes is None:
            from chemeascode.domains.process.simulation.backends import declared_inputs

            for path in declared_inputs(texts[SIMULATION_FILE][0]):
                if path in texts:
                    continue
                found = _simulation_input(snapshot, path)
                if found is not None:
                    texts[path] = found
        if FLOWSHEET_FILE not in texts:
            raise ApiError(404, f"no {FLOWSHEET_FILE} at {snapshot.label}")

        def compute() -> tuple[Any, str]:
            # The loader reads files: a private temporary copy, never the repository.
            with tempfile.TemporaryDirectory() as tmp:
                for path, (content, _) in texts.items():
                    target = Path(tmp, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                process, findings = load(Path(tmp))
                if process is not None:  # the staleness of simulated values reads simulation.yaml
                    prime(process, worker_hashes)
            # reactions.yaml and catalysts.yaml are optional: their errors leave the
            # flowsheet without them
            optional = (REACTIONS_FILE, CATALYSTS_FILE)
            errors = [f for f in findings if f.level == "error" and f.file not in optional]
            if process is None or errors:
                first = errors[0] if errors else None
                where = f"{first.file}:{first.line}" if first and first.line else FLOWSHEET_FILE
                return None, f"{where}: {first.message if first else 'does not load'}"
            return process, ""

        key = (
            tuple((path, sha) for path, (_, sha) in sorted(texts.items())),
            tuple(sorted((worker_hashes or {}).items())),
        )
        process, problem = process_cache.get_or_compute(key, compute)
        if process is None:
            raise ApiError(422, problem)
        return process

    @app.get("/api/streams")
    def get_streams(case: str | None = None, rev: str | None = None) -> dict[str, Any]:
        from chemeascode.domains.process.sheets import represented
        from chemeascode.domains.process.streams import stream_table

        snapshot = repo.snapshot(rev)
        process = _process(snapshot)
        cases = [c.id for c in process.flowsheet.cases]
        case = case or process.flowsheet.default_case()
        if case not in cases:
            raise ApiError(422, f"case {case} is not declared (cases: {', '.join(cases)})")
        try:
            sheet_ids = {s["id"] for s in snapshot.sheets(snapshot.config())}
        except ApiError:
            sheet_ids = set()
        lists = {
            k: m for k in ("equipment", "lines", "instruments") if (m := _list_model(snapshot, k))
        }
        table = stream_table(process, case, lists, sheet_ids)
        # the drill-down follows the drawn BFD: its connections' represents: lines, where built
        drawn: dict[str, list[str]] = {}
        try:
            for sheet in snapshot.sheets(snapshot.config()):
                file = sheet.get("file")
                if sheet.get("kind") != "bfd" or not file:
                    continue
                try:
                    found = _small(snapshot, file)
                except ApiError:  # too large, or not served: the drawn BFD is left out
                    continue
                if found is not None:
                    drawn.update(represented(found[0]))
        except ApiError:
            pass
        if drawn:
            table["bfd_connections"] = drawn
        table["bfd_connections_from"] = "sheet" if drawn else "flowsheet"
        return {"rev": snapshot.commit, **table}

    def _case_of(process: Any, case: str | None) -> str:
        cases = [c.id for c in process.flowsheet.cases]
        case = case or process.flowsheet.default_case()
        if case not in cases:
            raise ApiError(422, f"case {case} is not declared (cases: {', '.join(cases)})")
        return case

    @app.get("/api/simulation")
    def get_simulation(case: str | None = None, rev: str | None = None) -> dict[str, Any]:
        """``process/simulation.yaml`` parsed, the run ``cheme process simulate --fill``
        recorded in ``process/simulation-run.json`` (null without one), whether that record is
        stale against the inputs now, and one merged row per unit. 404 without the simulation
        file; 422 when it does not load or for an unknown case. A flowsheet that does not load
        leaves the spec and the record, with ``stale`` null and ``problem`` saying why."""
        from chemeascode.domains.process.simulation import SIMULATION_FILE, record
        from chemeascode.domains.process.simulation.translate import load as load_simulation
        from chemeascode.foundation.provenance import content_fingerprint

        snapshot = repo.snapshot(rev)
        sim_text = _need(snapshot, SIMULATION_FILE)
        texts = {}
        from chemeascode.domains.process.simulation.backends import declared_inputs

        # a worker's inputs are fingerprinted without reading them; another simulator's declared
        # inputs are read whatever their size (only their fingerprint is used, #318)
        worker_hashes = _worker_fingerprints(snapshot, sim_text[0])
        for path in () if worker_hashes is not None else declared_inputs(sim_text[0]):
            found = _simulation_input(snapshot, path)
            if found is not None:
                texts[path] = found
        try:
            found = _small(snapshot, record.RUN_FILE)
        except ApiError:
            found = None  # too large or not served: counts as absent
        if found is not None:
            texts[record.RUN_FILE] = found
        try:
            process, problem = _process(snapshot), ""
        except ApiError as failure:
            process, problem = None, failure.message
        if process is not None:
            case = _case_of(process, case)

        def compute() -> tuple[Any, dict[str, Any] | None, str]:
            # The loader reads the file: a private temporary copy, never the repository.
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp, SIMULATION_FILE)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sim_text[0])
                sim, doc, found = load_simulation(Path(tmp))
            if sim is None:
                first = found[0] if found else None
                where = (
                    f"{SIMULATION_FILE}:{first.line}" if first and first.line else SIMULATION_FILE
                )
                return process, None, f"{where}: {first.message if first else 'does not load'}"
            fingerprints = (
                worker_hashes
                if worker_hashes is not None
                else {
                    p: content_fingerprint(c) for p, (c, _) in texts.items() if p != record.RUN_FILE
                }
            )
            data = record.parse(texts[record.RUN_FILE][0]) if record.RUN_FILE in texts else None
            view = record.view(process, sim, doc, data, case, fingerprints, problem)
            return process, view, ""

        # the process object is cached by its blob ids: holding it keeps its id unique
        key = (
            id(process),
            case,
            tuple((p, sha) for p, (_, sha) in sorted(texts.items())),
            tuple(sorted((worker_hashes or {}).items())),
        )
        _, payload, failed = simulation_cache.get_or_compute((key, sim_text[1]), compute)
        if payload is None:
            raise ApiError(422, failed)
        return {"rev": snapshot.commit, **payload}

    def _chemistry(snapshot: Snapshot, lenient: bool = False) -> Any:
        """The plant's components, reactions and catalysts at *snapshot*: its process when it has a
        flowsheet, else its materials, reactions and catalysts alone (None: no process files)."""
        from chemeascode.domains.process.microkinetic_report import load_chemistry

        if _exists(snapshot, FLOWSHEET_FILE):
            try:
                return _process(snapshot)
            except ApiError:
                if not lenient:
                    raise
                # *lenient*: a flowsheet that does not load leaves the chemistry without it
        texts = {}
        for path in (MATERIALS_FILE, REACTIONS_FILE, CATALYSTS_FILE):
            found = _small(snapshot, path)
            if found is not None:
                texts[path] = found
        if not texts:
            return None

        def compute() -> tuple[Any, str]:
            with tempfile.TemporaryDirectory() as tmp:
                for path, (content, _) in texts.items():
                    target = Path(tmp, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                return load_chemistry(Path(tmp)), ""

        key = ("chemistry", *((path, sha) for path, (_, sha) in sorted(texts.items())))
        chemistry, _ = process_cache.get_or_compute(key, compute)
        return chemistry

    @app.get("/api/kinetics")
    def get_kinetics(rev: str | None = None) -> dict[str, Any]:
        """The kinetic data files of ``experiments/kinetics/`` with their points and default
        fits (``cheme experiments fit-rate``). 404 without any."""
        from chemeascode.project.kinetics import kinetic_data_view

        snapshot = repo.snapshot(rev)
        paths = _folder_files(snapshot, KINETICS_DIR, (".yaml", ".yml"))
        if not paths:
            raise ApiError(404, f"no {KINETICS_DIR}/*.yaml at {snapshot.label}")
        texts = {}
        for path in paths:
            found = _small(snapshot, path)
            if found is not None:
                texts[path] = found
        chemistry = _chemistry(snapshot, lenient=True)  # the fits need no flowsheet
        if chemistry is None:
            raise ApiError(404, f"no process files at {snapshot.label}")

        def compute() -> tuple[Any, dict[str, Any]]:
            decoded = {p: c.decode("utf-8", errors="replace") for p, (c, _) in texts.items()}
            return chemistry, kinetic_data_view(chemistry, decoded)

        # the chemistry object is cached by its blob ids: holding it keeps its id unique
        key = ("kinetics", id(chemistry), *((p, sha) for p, (_, sha) in sorted(texts.items())))
        _, payload = reactions_cache.get_or_compute(key, compute)
        return {"rev": snapshot.commit, **payload}

    @app.get("/api/library")
    def get_library(rev: str | None = None) -> dict[str, Any]:
        """The reaction library (``cheme chemistry library``), each item marked when the plant
        has it."""
        from chemeascode.domains.process.reaction_library import library_view

        snapshot = repo.snapshot(rev)
        chemistry = _chemistry(snapshot, lenient=True)
        return {"rev": snapshot.commit, **library_view(chemistry)}

    @app.get("/api/reactions")
    def get_reactions(case: str | None = None, rev: str | None = None) -> dict[str, Any]:
        """``process/reactions.yaml`` with what the flowsheet says about each reaction, the
        ``cheme process reactors`` rows, the reactive balances with their findings, each
        rate law with its units and k(T), and the catalysts of ``process/catalysts.yaml``. 404
        without the reactions file or the flowsheet; 422 when either does not load or for an
        unknown case."""
        from chemeascode.domains.process.reaction_view import reactions_payload

        snapshot = repo.snapshot(rev)
        content, _ = _need(snapshot, REACTIONS_FILE)
        process = _process(snapshot)
        if process.reactions is None:
            try:
                Reactions.model_validate(yaml.safe_load(content) or {})
                problem = "does not load"
            except yaml.YAMLError as failure:
                problem = _first_line(failure)
            except ValidationError as failure:
                e = failure.errors()[0]
                problem = f"{'.'.join(str(p) for p in e['loc']) or '(file)'}: {e['msg']}"
            raise ApiError(422, f"{REACTIONS_FILE}: {problem}")
        case = _case_of(process, case)
        # the process object is cached by its blob ids: holding it keeps its id unique
        _, payload = reactions_cache.get_or_compute(
            (id(process), case), lambda: (process, reactions_payload(process, case))
        )
        return {"rev": snapshot.commit, **payload}

    # -- checks ---------------------------------------------------------------------

    @app.get("/api/validate")
    def get_validate() -> dict[str, Any]:
        try:
            return json.loads(validate_workspace(repo.workspace).to_json())
        except (ValidationError, yaml.YAMLError) as failure:
            raise ApiError(422, f"cannot validate: {_first_line(failure)}") from failure

    @app.get("/api/maturity")
    def get_maturity(through: str | None = None) -> dict[str, Any]:
        """``cheme maturity --json`` of the working tree: the levels, requirement by
        requirement, the model basis, limitations and findings. Evaluated on every request
        (:func:`~chemeascode.project.maturity.maturity_payload`), one at a time; nothing is
        written (``checked_at`` is the time of this evaluation, in the answer only; ``cheme
        maturity --write`` writes ``checks/maturity.json``). Without *through* the engineering
        level runs the whole-repository validation, the dominant cost (some 30 s on a plant
        like ASU-1); ``through=predictive`` (or a lower level) skips it and reports the levels
        above as ``not-evaluated``. 400 for an unknown level; 422 when the plant does not load."""
        from chemeascode.project.maturity import LEVELS, maturity_payload

        ids = [level[0] for level in LEVELS]
        if through is not None and through not in ids:
            raise ApiError(400, f"through: one of {', '.join(ids)}")
        with maturity_lock:
            try:
                return maturity_payload(repo.workspace.root, through=through)
            except (ValidationError, yaml.YAMLError) as failure:
                raise ApiError(422, f"cannot evaluate: {_first_line(failure)}") from failure

    def _refuse_remote_checks() -> None:
        if not run_checks_allowed:
            raise ApiError(
                403,
                "running the checks is turned off: the server is reachable from the network "
                "(start cheme serve with --allow-remote-checks to allow it)",
            )

    @app.get("/api/session")
    def get_session() -> dict[str, str]:
        # Readable only by the served origin: no CORS headers, cross-site requests refused.
        _refuse_remote_checks()
        return {"token": token, "header": TOKEN_HEADER}

    @app.post("/api/checks/run")
    def post_checks_run(request: Request) -> dict[str, Any]:
        _refuse_remote_checks()
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            raise ApiError(403, f"missing or wrong {TOKEN_HEADER} (see GET /api/session)")
        if not check_run.acquire(blocking=False):
            raise ApiError(409, "the checks are already running")
        try:
            results = run_checks(repo.workspace, timeout=CHECK_TIMEOUT)
        except (ValidationError, yaml.YAMLError) as failure:
            raise ApiError(422, f"checks/checks.yaml: {_first_line(failure)}") from failure
        finally:
            check_run.release()
        return {"checks": [asdict(r) for r in results]}

    @app.get("/api/checks")
    def get_checks(run: bool = False, rev: str | None = None) -> Any:
        if run:
            # Running executes commands from checks.yaml: never on a plain GET.
            raise ApiError(405, "running the checks is POST /api/checks/run")
        found = _small(repo.snapshot(rev), "checks/status.json")
        if found is None:
            return {"checks": []}
        try:
            return json.loads(found[0])
        except ValueError as failure:
            raise ApiError(422, f"checks/status.json: {failure}") from failure

    # -- the viewer --------------------------------------------------------------------

    @app.get("/{full_path:path}", include_in_schema=False)
    def get_ui(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise ApiError(404, f"no endpoint /{full_path}")
        if ui is None:
            raise ApiError(404, "no viewer: start `cheme serve --ui DIST_DIR`; API at /api/")
        candidate = (ui / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(ui):
            return FileResponse(candidate, headers=_UI_HEADERS)
        if full_path.startswith("assets/"):
            # A missing bundle file must not come back as index.html (a confusing MIME error).
            raise ApiError(404, f"no file /{full_path}")
        return FileResponse(ui / "index.html", headers=_UI_HEADERS)

    return app


def is_local_bind(host: str) -> bool:
    """Whether binding *host* keeps the server on this machine (a loopback address)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard(host: str) -> bool:
    """Whether binding *host* means every interface (``0.0.0.0``, ``::``): it names no host a
    request could use."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def accepted_hosts(host: str, extra: Iterable[str] = ()) -> list[str]:
    """The ``Host`` header names the server accepts when bound to *host*: the local names, the
    bound address unless it is a wildcard (:func:`_is_wildcard`), and the *extra* names
    (``--allow-host``, an IPv6 address with or without brackets). Never ``*``: a page whose
    own name was rebound to this machine must not be able to read the repository (DNS
    rebinding). :class:`ValueError` for an *extra* wildcard, or one with a port, which no
    ``Host`` header could match."""
    names = set(LOCAL_HOSTS)
    for name in extra:
        if not name or "*" in name:
            raise ValueError("--allow-host takes a host name or address, not a wildcard")
        bare = name[1:-1] if name.startswith("[") and name.endswith("]") else name
        if _host_name(name) != bare or "/" in bare:
            raise ValueError(f"--allow-host {name!r}: give the host name or address alone, no port")
        names.add(bare.lower())
    if not _is_wildcard(host):
        names.add(host.lower())
    return sorted(names)


def serve(
    repo_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    ui_dir: Path | None = None,
    allow_hosts: Iterable[str] = (),
    allow_remote_checks: bool = False,
    library: bool = False,
) -> None:
    """Run the API (and the viewer, with *ui_dir*) until interrupted.

    *allow_hosts*: further names the server accepts in the ``Host`` header (see
    :func:`accepted_hosts`). *allow_remote_checks*: let ``POST /api/checks/run`` run the checks
    although *host* is not a local address. *library*: *repo_root* is a folder of plants, each
    served under ``/p/<id>/`` (:mod:`.library`)."""
    import uvicorn

    make: Callable[..., Any] = create_app
    if library:
        from chemeascode.interfaces.api.library import create_library_app

        make = create_library_app
    app = make(
        repo_root,
        ui_dir,
        allowed_hosts=accepted_hosts(host, allow_hosts),
        run_checks_allowed=is_local_bind(host) or allow_remote_checks,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
