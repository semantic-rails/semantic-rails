"""Cross-process transactional writes for Semantic Rails projects.

Every Architect authoring mutation passes through this module. The transaction
boundary owns deterministic project revisions, optimistic concurrency,
idempotency receipts, per-project locks, atomic file replacement, parse-gated
rollback, and write-free previews.
"""

from __future__ import annotations

import base64
import contextlib
import difflib
import errno
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_validation import PackageReference, parse_config_report
from .errors import SemanticLayerError

ABSENT_PROJECT_REVISION = "absent"
PROJECT_REVISION_FORMAT = 1
TRANSACTION_RECEIPT_FORMAT = 1

_INTERNAL_ROOTS = {
    ".architect",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".semantic-rails",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_GENERATED_SUFFIXES = {
    ".db",
    ".duckdb",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".tmp",
    ".wal",
}
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _try_file_lock(descriptor: int) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                1,
            )
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _release_file_lock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(  # type: ignore[attr-defined]
            descriptor,
            msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
            1,
        )
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def _digest(content: bytes | None) -> str:
    return hashlib.sha256(content or b"").hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _atomic_write_bytes(path: Path, content: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is None:
            current_umask = os.umask(0)
            os.umask(current_umask)
            mode = 0o666 & ~current_umask
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _is_authored_relative(relative_path: str) -> bool:
    path = Path(relative_path)
    if not path.parts or path.parts[0] in _INTERNAL_ROOTS:
        return False
    if any(part == "__pycache__" for part in path.parts):
        return False
    name = path.name.lower()
    if name == ".ds_store" or name.endswith(("-wal", "-shm")):
        return False
    return path.suffix.lower() not in _GENERATED_SUFFIXES


def _authored_project_files(project_path: Path) -> dict[str, bytes]:
    if not project_path.exists():
        return {}
    if not project_path.is_dir():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Architect project path must be a directory",
            details={"project_path": str(project_path)},
        )
    files: dict[str, bytes] = {}
    for root, dirnames, filenames in os.walk(project_path, followlinks=False):
        root_path = Path(root)
        retained_dirs: list[str] = []
        for name in sorted(dirnames):
            path = root_path / name
            relative_dir = path.relative_to(project_path)
            if relative_dir.parts and relative_dir.parts[0] in _INTERNAL_ROOTS:
                continue
            if path.is_symlink():
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect revisions refuse symlinked project directories",
                    details={"path": str(path)},
                )
            retained_dirs.append(name)
        dirnames[:] = retained_dirs
        for name in sorted(filenames):
            path = root_path / name
            relative_file = path.relative_to(project_path).as_posix()
            if not _is_authored_relative(relative_file):
                continue
            if path.is_symlink():
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect revisions refuse symlinked project files",
                    details={"path": str(path)},
                )
            if path.is_file():
                files[relative_file] = path.read_bytes()
    return files


def _revision_from_files(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(f"semantic-rails-project-revision-v{PROJECT_REVISION_FORMAT}\0".encode())
    for relative_path in sorted(files):
        path_bytes = relative_path.encode("utf-8")
        content = files[relative_path]
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def project_revision(project_path: str | os.PathLike[str]) -> str:
    """Return the deterministic revision of authored project source files."""

    project = Path(project_path).expanduser()
    if not project.is_absolute():
        project = Path.cwd() / project
    project = Path(os.path.abspath(project))
    if not project.exists():
        return ABSENT_PROJECT_REVISION
    return _revision_from_files(_authored_project_files(project))


@dataclass(frozen=True)
class ProjectFileUpdate:
    """One project-relative file replacement or deletion."""

    relative_path: str
    content: bytes | None
    mode: int | None = None


@dataclass(frozen=True)
class ProjectFileSnapshot:
    path: Path
    relative_path: str
    existed: bool
    content: bytes | None
    mode: int | None
    after_digest: str = ""

    def with_after(self, content: bytes | None) -> ProjectFileSnapshot:
        return ProjectFileSnapshot(
            path=self.path,
            relative_path=self.relative_path,
            existed=self.existed,
            content=self.content,
            mode=self.mode,
            after_digest=_digest(content),
        )


@dataclass(frozen=True)
class ProjectTransactionOutcome:
    report: dict[str, Any]
    snapshots: tuple[ProjectFileSnapshot, ...] = ()


def restore_project_snapshots(snapshots: Iterable[ProjectFileSnapshot]) -> None:
    """Restore snapshots with atomic replacements."""

    for snapshot in reversed(tuple(snapshots)):
        if snapshot.existed:
            _atomic_write_bytes(snapshot.path, snapshot.content or b"", mode=snapshot.mode)
        elif snapshot.path.exists():
            if snapshot.path.is_symlink():
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Refusing to remove a symlink while rolling back an Architect transaction",
                    details={"path": str(snapshot.path)},
                )
            snapshot.path.unlink()


def _canonical_json_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _content_payload(content: bytes | None) -> tuple[str, str | None]:
    if content is None:
        return "none", None
    try:
        return "utf-8", content.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(content).decode("ascii")


def _file_change_row(
    relative_path: str,
    before: bytes | None,
    after: bytes | None,
) -> dict[str, Any]:
    before_encoding, before_content = _content_payload(before)
    after_encoding, after_content = _content_payload(after)
    if before is None:
        operation = "create"
    elif after is None:
        operation = "delete"
    else:
        operation = "update"
    diff = ""
    if before_encoding in {"none", "utf-8"} and after_encoding in {"none", "utf-8"}:
        diff = "".join(
            difflib.unified_diff(
                (before_content or "").splitlines(keepends=True),
                (after_content or "").splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )
    return {
        "path": relative_path,
        "operation": operation,
        "before_sha256": f"sha256:{_digest(before)}" if before is not None else None,
        "after_sha256": f"sha256:{_digest(after)}" if after is not None else None,
        "before_bytes": len(before or b""),
        "after_bytes": len(after or b""),
        "content_encoding": after_encoding,
        "proposed_content": after_content,
        "diff": diff,
    }


class ProjectTransaction:
    """One workspace-scoped, optimistic project transaction boundary."""

    def __init__(
        self,
        project_path: str | os.PathLike[str],
        *,
        workspace_root: str | os.PathLike[str],
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        raw_project = Path(project_path).expanduser()
        if not raw_project.is_absolute():
            raw_project = root / raw_project
        project = Path(os.path.abspath(raw_project))
        if not _within(project, root):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect transaction project is outside its workspace root",
                details={"workspace_root": str(root), "project_path": str(project)},
            )
        self.workspace_root = root
        self.project_path = project
        self.lock_timeout_seconds = max(0.1, float(lock_timeout_seconds))
        identity = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
        self._lock_path = root / ".semantic-rails" / "architect-locks" / f"{identity}.lock"
        self._receipt_root = root / ".semantic-rails" / "architect-transactions" / identity

    def current_revision(self) -> str:
        return project_revision(self.project_path)

    def apply(
        self,
        updates: Iterable[ProjectFileUpdate],
        *,
        expected_revision: str,
        idempotency_key: str,
        intent: Mapping[str, Any],
        dry_run: bool = False,
        validate_after: bool = True,
        allow_internal_paths: bool = False,
        success_status: str = "committed",
        metadata: Mapping[str, Any] | None = None,
    ) -> ProjectTransactionOutcome:
        """Apply a parse-gated optimistic transaction or return its preview."""

        expected = str(expected_revision or "").strip()
        key = str(idempotency_key or "").strip()
        if not expected:
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "expected_revision is required for every Architect mutation",
                details={"argument": "expected_revision"},
            )
        if not key:
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "idempotency_key is required for every Architect mutation",
                details={"argument": "idempotency_key"},
            )
        normalized_updates = self._normalize_updates(
            updates,
            allow_internal_paths=allow_internal_paths,
        )
        intent_hash = _canonical_json_digest(dict(intent))
        with self._exclusive_lock():
            receipt = self._load_receipt(key)
            if receipt is not None:
                if str(receipt.get("intent_hash", "")) != intent_hash:
                    raise SemanticLayerError(
                        "CONFIG_CONFLICT",
                        "The idempotency key was already used for a different Architect mutation",
                        details={
                            "conflict_kind": "idempotency_key_reuse",
                            "idempotency_key": key,
                            "project_path": str(self.project_path),
                        },
                    )
                replay = deepcopy(dict(receipt.get("report", {}) or {}))
                original_status = str(replay.get("status", ""))
                replay.update(
                    {
                        "status": "replayed",
                        "original_status": original_status,
                        "idempotency_key": key,
                        "idempotent_replay": True,
                        "current_revision": self.current_revision(),
                    }
                )
                return ProjectTransactionOutcome(report=replay)

            current = self.current_revision()
            if expected != current:
                raise SemanticLayerError(
                    "CONFIG_CONFLICT",
                    "Architect project revision is stale",
                    details={
                        "conflict_kind": "stale_revision",
                        "project_path": str(self.project_path),
                        "expected_revision": expected,
                        "current_revision": current,
                        "retry": "Read project_status, review intervening changes, and retry with a new idempotency_key.",
                    },
                )

            snapshots = tuple(self._snapshot(update.relative_path) for update in normalized_updates)
            effective = tuple(
                update
                for update, snapshot in zip(normalized_updates, snapshots, strict=True)
                if snapshot.content != update.content
            )
            effective_snapshots = tuple(
                snapshot
                for update, snapshot in zip(normalized_updates, snapshots, strict=True)
                if snapshot.content != update.content
            )
            changes = [
                _file_change_row(snapshot.relative_path, snapshot.content, update.content)
                for update, snapshot in zip(effective, effective_snapshots, strict=True)
            ]
            proposed_revision = self._proposed_revision(effective)
            base_report: dict[str, Any] = {
                "ok": True,
                "status": "preview" if dry_run else success_status,
                "project_path": str(self.project_path),
                "workspace_root": str(self.workspace_root),
                "expected_revision": expected,
                "base_revision": current,
                "current_revision": current,
                "revision": current,
                "proposed_revision": proposed_revision,
                "idempotency_key": key,
                "idempotent_replay": False,
                "dry_run": bool(dry_run),
                "changed_files": [row["path"] for row in changes],
                "changes": changes,
            }
            if metadata:
                base_report.update(deepcopy(dict(metadata)))

            if dry_run:
                if validate_after:
                    preview_parse = self._validate_virtual(effective)
                    base_report["parse"] = preview_parse
                    base_report["ok"] = bool(preview_parse.get("ok"))
                    if not base_report["ok"]:
                        base_report["status"] = "preview_invalid"
                        base_report["errors"] = list(preview_parse.get("errors", []) or [])
                return ProjectTransactionOutcome(report=base_report)

            applied = False
            try:
                # Mark the transaction as needing restoration before the first
                # replacement: a later write in the same multi-file batch may
                # fail after earlier paths were already committed.
                applied = True
                self._apply_updates(effective, effective_snapshots)
                if validate_after:
                    parse_report, _ = parse_config_report(
                        PackageReference(source_path=str(self.project_path))
                    )
                    base_report["parse"] = parse_report
                    if not parse_report.get("ok"):
                        restore_project_snapshots(effective_snapshots)
                        self._cleanup_new_project()
                        applied = False
                        base_report.update(
                            {
                                "ok": False,
                                "status": "rolled_back_after_parse_error",
                                "rolled_back": True,
                                "revision": current,
                                "current_revision": current,
                                "errors": list(parse_report.get("errors", []) or []),
                            }
                        )
                        self._store_receipt(key, intent_hash, base_report)
                        return ProjectTransactionOutcome(report=base_report)

                committed_revision = self.current_revision()
                if committed_revision != proposed_revision:
                    raise SemanticLayerError(
                        "CONFIG_CONFLICT",
                        "Architect transaction produced an unexpected project revision",
                        details={
                            "conflict_kind": "revision_mismatch",
                            "expected_revision": proposed_revision,
                            "current_revision": committed_revision,
                        },
                    )
                base_report.update(
                    {
                        "revision": committed_revision,
                        "current_revision": committed_revision,
                    }
                )
                completed_snapshots = tuple(
                    snapshot.with_after(_read_bytes(snapshot.path))
                    for snapshot in effective_snapshots
                )
                self._store_receipt(key, intent_hash, base_report)
                return ProjectTransactionOutcome(
                    report=base_report,
                    snapshots=completed_snapshots,
                )
            except BaseException:
                if applied:
                    restore_project_snapshots(effective_snapshots)
                    self._cleanup_new_project()
                raise

    def _normalize_updates(
        self,
        updates: Iterable[ProjectFileUpdate],
        *,
        allow_internal_paths: bool,
    ) -> tuple[ProjectFileUpdate, ...]:
        by_path: dict[str, ProjectFileUpdate] = {}
        for update in updates:
            relative = str(update.relative_path or "").strip().replace("\\", "/").lstrip("/")
            if not relative:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect transaction updates require a relative path",
                )
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect transaction paths must stay inside the project",
                    details={"relative_path": relative},
                )
            if path.parts[0] in {".git", ".semantic-rails"}:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect transaction path is reserved for internal state",
                    details={"relative_path": relative},
                )
            if path.parts[0] == ".architect" and not (
                allow_internal_paths and len(path.parts) > 1 and path.parts[1] == "archive"
            ):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect transaction path is reserved for internal state",
                    details={"relative_path": relative},
                )
            target = self._target_path(relative)
            mode = update.mode
            if mode is None and target.exists():
                mode = target.stat().st_mode & 0o777
            by_path[relative] = ProjectFileUpdate(relative, update.content, mode)
        return tuple(by_path[name] for name in sorted(by_path))

    def _target_path(self, relative_path: str) -> Path:
        path = Path(os.path.abspath(self.project_path / relative_path))
        if not _within(path, self.project_path):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect transaction target is outside the project",
                details={"relative_path": relative_path},
            )
        current = path
        while _within(current, self.project_path):
            if current.is_symlink():
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    "Architect transactions refuse symlinked paths",
                    details={"path": str(current)},
                )
            if current == self.project_path:
                break
            current = current.parent
        return path

    def _snapshot(self, relative_path: str) -> ProjectFileSnapshot:
        path = self._target_path(relative_path)
        existed = path.exists()
        if existed and not path.is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Architect transaction target must be a regular file",
                details={"relative_path": relative_path},
            )
        return ProjectFileSnapshot(
            path=path,
            relative_path=relative_path,
            existed=existed,
            content=_read_bytes(path),
            mode=(path.stat().st_mode & 0o777) if existed else None,
        )

    def _proposed_revision(self, updates: Iterable[ProjectFileUpdate]) -> str:
        files = _authored_project_files(self.project_path)
        for update in updates:
            if not _is_authored_relative(update.relative_path):
                continue
            if update.content is None:
                files.pop(update.relative_path, None)
            else:
                files[update.relative_path] = update.content
        return _revision_from_files(files)

    def _apply_updates(
        self,
        updates: tuple[ProjectFileUpdate, ...],
        snapshots: tuple[ProjectFileSnapshot, ...],
    ) -> None:
        snapshot_by_path = {snapshot.relative_path: snapshot for snapshot in snapshots}
        # Materialize replacements before deletions so archive-like operations
        # never discard their source before the destination is durable.
        for update in [row for row in updates if row.content is not None]:
            snapshot = snapshot_by_path[update.relative_path]
            _atomic_write_bytes(
                snapshot.path,
                update.content or b"",
                mode=update.mode,
            )
        for update in [row for row in updates if row.content is None]:
            snapshot = snapshot_by_path[update.relative_path]
            if snapshot.path.exists():
                snapshot.path.unlink()

    def _validate_virtual(self, updates: tuple[ProjectFileUpdate, ...]) -> dict[str, Any]:
        files = _authored_project_files(self.project_path)
        for update in updates:
            if not _is_authored_relative(update.relative_path):
                continue
            if update.content is None:
                files.pop(update.relative_path, None)
            else:
                files[update.relative_path] = update.content
        with tempfile.TemporaryDirectory(prefix="semantic-rails-architect-preview-") as temporary:
            project = Path(temporary) / self.project_path.name
            for relative_path, content in files.items():
                _atomic_write_bytes(project / relative_path, content)
            parse, _ = parse_config_report(PackageReference(source_path=str(project)))
        return parse

    def _cleanup_new_project(self) -> None:
        if not self.project_path.exists():
            return
        for root, _dirnames, filenames in os.walk(self.project_path, topdown=False):
            if filenames:
                continue
            path = Path(root)
            with contextlib.suppress(OSError):
                path.rmdir()

    @contextlib.contextmanager
    def _exclusive_lock(self):
        key = str(self._lock_path)
        with _LOCAL_LOCKS_GUARD:
            local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
        acquired = local.acquire(timeout=self.lock_timeout_seconds)
        if not acquired:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                "Timed out waiting for the Architect project lock",
                details={
                    "conflict_kind": "lock_timeout",
                    "project_path": str(self.project_path),
                },
            )
        descriptor: int | None = None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                if _try_file_lock(descriptor):
                    break
                if time.monotonic() >= deadline:
                    raise SemanticLayerError(
                        "CONFIG_CONFLICT",
                        "Timed out waiting for the Architect project lock",
                        details={
                            "conflict_kind": "lock_timeout",
                            "project_path": str(self.project_path),
                        },
                    )
                time.sleep(0.025)
            yield
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    _release_file_lock(descriptor)
                os.close(descriptor)
            local.release()

    def _receipt_path(self, idempotency_key: str) -> Path:
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._receipt_root / f"{key_hash}.json"

    def _load_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        path = self._receipt_path(idempotency_key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                "Architect idempotency receipt is unreadable",
                details={"receipt_path": str(path)},
            ) from exc
        if int(payload.get("format_version", 0) or 0) != TRANSACTION_RECEIPT_FORMAT:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                "Architect idempotency receipt has an unsupported format",
                details={"receipt_path": str(path)},
            )
        return dict(payload)

    def _store_receipt(
        self,
        idempotency_key: str,
        intent_hash: str,
        report: Mapping[str, Any],
    ) -> None:
        path = self._receipt_path(idempotency_key)
        stored_report = dict(report)
        stored_report.pop("idempotency_key", None)
        payload = {
            "format_version": TRANSACTION_RECEIPT_FORMAT,
            "project_path": str(self.project_path),
            "intent_hash": intent_hash,
            "report": stored_report,
        }
        content = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(path, content, mode=0o600)


__all__ = [
    "ABSENT_PROJECT_REVISION",
    "PROJECT_REVISION_FORMAT",
    "ProjectFileSnapshot",
    "ProjectFileUpdate",
    "ProjectTransaction",
    "ProjectTransactionOutcome",
    "project_revision",
    "restore_project_snapshots",
]
