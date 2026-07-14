import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
import frontmatter
from .models import FileInfo, FileContent
from .tenancy import BASE_DATA_DIR, user_root

logger = logging.getLogger("kiwiki.storage")

# Backwards-compat: external imports of DATA_DIR still resolve, but only point
# at the base (shared) data directory. All storage functions below operate on
# the *current user's* root via user_root().
DATA_DIR = BASE_DATA_DIR

# Max bytes to read for frontmatter-only extraction (YAML frontmatter is
# always at the top of the file and typically < 1 KB).
_FRONTMATTER_READ_LIMIT = 8192
_MAX_TENANT_FILES = int(os.getenv("KIWIKI_MAX_TENANT_FILES", "10000"))
_MAX_TENANT_BYTES = int(os.getenv("KIWIKI_MAX_TENANT_BYTES", str(1024 * 1024 * 1024)))
_MAX_LIST_ITEMS = int(os.getenv("KIWIKI_MAX_LIST_ITEMS", "1000"))
_MAX_RECURSIVE_LIST_ITEMS = int(os.getenv("KIWIKI_MAX_RECURSIVE_LIST_ITEMS", "10000"))

# ── Frontmatter cache (A3) ────────────────────────────────────────────────────
_fm_cache_lock = threading.Lock()
_fm_cache: dict[tuple, dict] = {}


def _invalidate_fm_cache(path: str = "") -> None:
    """Invalidate frontmatter cache entries. Empty path clears entire cache."""
    with _fm_cache_lock:
        _fm_cache.clear()


# ── list_all_files cache (A2) ────────────────────────────────────────────────
_list_cache_lock = threading.Lock()
_list_cache: dict[str, tuple[float, list[dict]]] = {}
_LIST_CACHE_TTL = 5

# Feste Lock-Striping-Tabelle: derselbe aufgeloeste Pfad landet immer auf
# demselben RLock, ohne dass ein unbegrenztes Pfad->Lock-Dictionary waechst.
_PATH_LOCKS = tuple(threading.RLock() for _ in range(128))
_TENANT_WRITE_LOCKS = tuple(threading.RLock() for _ in range(32))


@contextmanager
def _path_lock(path: str):
    resolved = str(safe_path(path))
    lock = _PATH_LOCKS[hash(resolved) % len(_PATH_LOCKS)]
    with lock:
        yield


@contextmanager
def _locked_paths(*paths: str):
    locks = {
        _PATH_LOCKS[hash(str(safe_path(path))) % len(_PATH_LOCKS)]
        for path in paths
    }
    ordered = sorted(locks, key=id)
    for lock in ordered:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(ordered):
            lock.release()


def _invalidate_list_cache() -> None:
    """Invalidate the list_all_files cache."""
    with _list_cache_lock:
        _list_cache.clear()


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.replace("\\", "/").strip("/").split("/") if part)


def validate_markdown_content_path(path: str) -> None:
    """Validate paths accepted from public write APIs."""
    parts = _path_parts(path)
    if not parts:
        raise ValueError("Empty path")
    if ".kiwiki" in parts:
        raise ValueError("System paths under .kiwiki are not writable")
    if not path.endswith(".md"):
        raise ValueError("Only .md files may be written")


def validate_content_folder_path(path: str) -> None:
    parts = _path_parts(path)
    if not parts:
        raise ValueError("Empty path")
    if ".kiwiki" in parts:
        raise ValueError("System paths under .kiwiki are not writable")


def _atomic_write_text(file_path: Path, content: str) -> None:
    """UTF-8-Inhalt crash-sicher im selben Verzeichnis ersetzen."""
    tenant_lock = _TENANT_WRITE_LOCKS[hash(str(user_root().resolve())) % len(_TENANT_WRITE_LOCKS)]
    with tenant_lock:
        _atomic_write_text_locked(file_path, content)


def _atomic_write_text_locked(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    _enforce_tenant_quota(file_path, len(content.encode("utf-8")))
    mode = file_path.stat().st_mode & 0o777 if file_path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file_path.name}-", dir=str(file_path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)
        try:
            dir_fd = os.open(file_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _enforce_tenant_quota(target_path: Path, new_size: int) -> None:
    """Dateianzahl und Gesamtgroesse des aktuellen Namespaces begrenzen."""
    root = user_root()
    file_count = 0
    total_bytes = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except (FileNotFoundError, PermissionError):
            continue
        with entries:
            for entry in entries:
                if entry.name == ".kiwiki":
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                    file_count += 1
                    try:
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass

    existing_size = target_path.stat().st_size if target_path.exists() else 0
    projected_count = file_count + (0 if target_path.exists() else 1)
    projected_bytes = total_bytes - existing_size + new_size
    if projected_count > _MAX_TENANT_FILES:
        raise ValueError(f"Tenant file quota exceeded (max {_MAX_TENANT_FILES})")
    if projected_bytes > _MAX_TENANT_BYTES:
        raise ValueError(f"Tenant storage quota exceeded (max {_MAX_TENANT_BYTES} bytes)")


def safe_path(path: str) -> Path:
    """
    Validate and resolve path safely within the *current user's* data root.
    Prevents path traversal by resolving relative to user_root().
    Raises ValueError if path escapes that root.
    """
    if not path:
        raise ValueError("Empty path")
    clean = path.lstrip("/")
    root = user_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    normalized = (root / clean).resolve()
    if normalized != root and not str(normalized).startswith(str(root) + os.sep):
        raise ValueError(f"Path traversal detected: {path!r}")
    return normalized


def _read_frontmatter_only(path: str) -> dict:
    """Extract frontmatter metadata without reading the full file content.

    Reads only the first _FRONTMATTER_READ_LIMIT bytes, which is sufficient
    for YAML frontmatter (always at the top, typically < 1 KB).
    Returns the metadata dict; content is not extracted.

    Cached by (resolved path, mtime) so unchanged files are only parsed
    once per write. Writes invalidate the whole cache via
    _invalidate_fm_cache(), so an mtime bump alone is enough to bypass
    stale entries left by out-of-band edits (e.g. a mounted volume).
    """
    file_path = safe_path(path)
    if not file_path.exists() or not file_path.is_file():
        return {}
    try:
        mtime_ns = file_path.stat().st_mtime_ns
    except OSError:
        return {}
    cache_key = (str(file_path), mtime_ns)
    with _fm_cache_lock:
        cached = _fm_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read(_FRONTMATTER_READ_LIMIT)
        post = frontmatter.loads(raw)
        metadata = post.metadata
    except Exception:
        logger.warning("Failed to parse frontmatter for %r", path, exc_info=True)
        return {}
    with _fm_cache_lock:
        _fm_cache[cache_key] = metadata
    return metadata


def read_file(path: str) -> FileContent:
    """
    Read markdown file with frontmatter.
    Raises FileNotFoundError if file doesn't exist.
    """
    file_path = safe_path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Not a file: {path}")
    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)
    return FileContent(
        path=path,
        content=post.content,
        frontmatter=post.metadata,
        revision=file_path.stat().st_mtime_ns,
    )


def write_file(path: str, content: str, expected_revision: int | None = None) -> FileContent:
    """
    Write markdown file.
    Creates directories if needed.
    Updates 'updated' timestamp in frontmatter.
    """
    validate_markdown_content_path(path)
    with _path_lock(path):
        file_path = safe_path(path)
        current_revision = file_path.stat().st_mtime_ns if file_path.exists() else None
        if expected_revision is not None and current_revision != expected_revision:
            raise ValueError(
                f"Write conflict for {path!r}: expected revision {expected_revision}, "
                f"current revision is {current_revision}"
            )
        post = frontmatter.loads(content) if content else frontmatter.Post("")
        post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
        _atomic_write_text(file_path, frontmatter.dumps(post))
        revision = file_path.stat().st_mtime_ns
    _invalidate_fm_cache(path)
    _invalidate_list_cache()
    return FileContent(path=path, content=post.content, frontmatter=post.metadata, revision=revision)


def append_file(path: str, content: str) -> FileContent:
    """
    Append content to markdown file.
    Updates 'updated' timestamp.
    """
    validate_markdown_content_path(path)
    with _path_lock(path):
        file_path = safe_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        with open(file_path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)
        post.content += "\n" + content
        post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
        _atomic_write_text(file_path, frontmatter.dumps(post))
        revision = file_path.stat().st_mtime_ns
    _invalidate_fm_cache(path)
    _invalidate_list_cache()
    return FileContent(path=path, content=post.content, frontmatter=post.metadata, revision=revision)


def list_files(path: str = ".") -> list[FileInfo]:
    """
    List files and directories in path (within the current user's namespace).
    Returns sorted list with directories first.
    Uses lightweight frontmatter extraction (first 8 KB only) instead of
    full file reads for metadata.
    """
    root = user_root()
    if path == ".":
        root.mkdir(parents=True, exist_ok=True)
        dir_path = root
    else:
        dir_path = safe_path(path)
    if not dir_path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    items = []
    for item in sorted(dir_path.iterdir(), key=lambda x: (not (not x.is_symlink() and x.is_dir()), x.name)):
        if item.name == ".kiwiki":
            continue
        rel_path = str(item.relative_to(root))
        if not item.is_symlink() and item.is_dir():
            has_children = any(True for _ in item.iterdir())
            items.append(
                FileInfo(
                    path=rel_path,
                    name=item.name,
                    is_dir=True,
                    size=0,
                    has_children=has_children,
                )
            )
        elif not item.is_symlink() and item.is_file():
            stat = item.stat()
            mtime_str = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().split("T")[0]
            try:
                meta = _read_frontmatter_only(rel_path)
                updated = meta.get("updated", mtime_str)
            except Exception:
                updated = mtime_str
            items.append(
                FileInfo(
                    path=rel_path,
                    name=item.name,
                    is_dir=False,
                    size=stat.st_size,
                    updated_at=updated,
                )
            )
        if len(items) >= _MAX_LIST_ITEMS:
            break
    return items


def delete_file(path: str) -> None:
    """
    Delete a markdown file.
    Raises FileNotFoundError if the file does not exist.
    Raises ValueError if the path is not a .md file or not a regular file.
    """
    validate_markdown_content_path(path)
    with _path_lock(path):
        file_path = safe_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        file_path.unlink()
    _invalidate_fm_cache(path)
    _invalidate_list_cache()


def create_folder(path: str) -> None:
    """Create a folder."""
    validate_content_folder_path(path)
    with _path_lock(path):
        dir_path = safe_path(path)
        dir_path.mkdir(parents=True, exist_ok=True)
    _invalidate_list_cache()


def delete_folder(path: str) -> None:
    """
    Recursively delete a folder and all its contents.
    Raises FileNotFoundError if not found, ValueError if not a directory
    or if path is a top-level protected folder.
    """
    import shutil

    if not path:
        raise ValueError("Cannot delete the data root directory")
    validate_content_folder_path(path)
    with _path_lock(path):
        dir_path = safe_path(path)
        if not dir_path.exists():
            raise FileNotFoundError(f"Folder not found: {path}")
        if not dir_path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        if dir_path.resolve() == user_root().resolve():
            raise ValueError("Cannot delete the data root directory")
        shutil.rmtree(dir_path)
    _invalidate_fm_cache()
    _invalidate_list_cache()


def create_note(title: str, content: str, tags: list[str], owner: str, folder: str = "notes") -> str:
    """
    Create a new note file with frontmatter.
    Slug is based on title. Folder can be any sub-path (e.g. 'notes/python').
    Returns relative path to created file.
    """
    folder = folder.strip("/") or "notes"
    validate_content_folder_path(folder)
    slug = title.lower().replace(" ", "-").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:60]
    if not slug.strip("-_"):
        raise ValueError("Title does not produce a valid slug")
    # Auswahl und Schreiben muessen denselben Ordner-Lock halten, sonst
    # waehlen parallele Agenten vor dem ersten Replace denselben freien Slug.
    with _path_lock(folder):
        i = 2
        path = f"{folder}/{slug}.md"
        while safe_path(path).exists():
            path = f"{folder}/{slug}-{i}.md"
            i += 1
        now = datetime.now(timezone.utc).isoformat().split("T")[0]
        fm = {
            "title": title,
            "type": "note",
            "created": now,
            "updated": now,
            "tags": tags,
            "owner": owner,
        }
        post = frontmatter.Post(content, **fm)
        validate_markdown_content_path(path)
        file_path = safe_path(path)
        _atomic_write_text(file_path, frontmatter.dumps(post))
    _invalidate_fm_cache(path)
    _invalidate_list_cache()
    return path


def edit_file(path: str, new_str: str, old_str: str = "") -> FileContent:
    """
    Edit a file's content without touching frontmatter.
    If old_str is provided: find-and-replace first occurrence.
    If old_str is empty/omitted: append new_str to the end.
    """
    validate_markdown_content_path(path)
    with _path_lock(path):
        file_path = safe_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        with open(file_path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)
        if old_str:
            if old_str not in post.content:
                raise ValueError(f"String not found in {path!r}")
            post.content = post.content.replace(old_str, new_str, 1)
        else:
            post.content = post.content.rstrip("\n") + "\n\n" + new_str
        post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
        _atomic_write_text(file_path, frontmatter.dumps(post))
        revision = file_path.stat().st_mtime_ns
    _invalidate_fm_cache(path)
    _invalidate_list_cache()
    return FileContent(path=path, content=post.content, frontmatter=post.metadata, revision=revision)


def update_frontmatter(path: str, updates: dict) -> FileContent:
    """
    Merge updates into the frontmatter of an existing file without touching its content.
    Always sets 'updated' to today.
    """
    validate_markdown_content_path(path)
    with _path_lock(path):
        file_path = safe_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        with open(file_path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)
        post.metadata.update(updates)
        post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
        _atomic_write_text(file_path, frontmatter.dumps(post))
        revision = file_path.stat().st_mtime_ns
    _invalidate_fm_cache(path)
    _invalidate_list_cache()
    return FileContent(path=path, content=post.content, frontmatter=post.metadata, revision=revision)


def move_folder(src: str, dst: str) -> None:
    """Move/rename a folder within DATA_DIR."""
    import shutil

    validate_content_folder_path(src)
    validate_content_folder_path(dst)
    with _locked_paths(src, dst):
        src_path = safe_path(src)
        dst_path = safe_path(dst)
        if not src_path.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        if not src_path.is_dir():
            raise ValueError(f"Source is not a directory: {src}")
        if dst_path.exists():
            raise ValueError(f"Destination already exists: {dst}")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
    _invalidate_fm_cache()
    _invalidate_list_cache()


def move_file(src: str, dst: str) -> FileContent:
    """
    Move/rename a markdown file within DATA_DIR.
    Creates destination directories if needed.
    Updates the search index automatically.
    """
    validate_markdown_content_path(src)
    validate_markdown_content_path(dst)
    with _locked_paths(src, dst):
        src_path = safe_path(src)
        dst_path = safe_path(dst)
        if not src_path.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        if not src_path.is_file():
            raise ValueError(f"Source is not a file: {src}")
        if dst_path.exists():
            raise ValueError(f"Destination already exists: {dst}")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.rename(dst_path)
    _invalidate_fm_cache(src)
    _invalidate_fm_cache(dst)
    _invalidate_list_cache()
    return read_file(dst)


def list_all_files(path: str = ".") -> list[dict]:
    """
    Recursively list all markdown files under path (within the user's namespace).
    Returns list of {path, title, updated, tags} dicts for the AI to navigate.
    Uses lightweight frontmatter extraction (first 8 KB only) instead of
    full file reads for metadata.
    """
    root = user_root()
    if path == ".":
        root.mkdir(parents=True, exist_ok=True)
        dir_path = root
    else:
        dir_path = safe_path(path)

    # Check cache (A2)
    ns = ""
    try:
        from .tenancy import current_user_ns
        ns = current_user_ns()
    except RuntimeError:
        pass
    cache_key = f"{root}:{ns}:{path}"
    now = time.time()
    with _list_cache_lock:
        cached = _list_cache.get(cache_key)
        if cached and now - cached[0] < _LIST_CACHE_TTL:
            return cached[1]

    # Use os.scandir for faster recursive traversal (A5)
    items = []
    _scan_markdown_recursive(dir_path, root, items)
    items.sort(key=lambda x: x["path"])

    with _list_cache_lock:
        _list_cache[cache_key] = (now, items)
    return items


def _scan_markdown_recursive(dir_path: Path, root: Path, items: list) -> None:
    """Recursively scan for .md files using os.scandir (A5)."""
    if len(items) >= _MAX_RECURSIVE_LIST_ITEMS:
        return
    if not dir_path.exists() or not dir_path.is_dir():
        return
    try:
        entries = list(os.scandir(dir_path))
    except (PermissionError, FileNotFoundError):
        return
    for entry in entries:
        if len(items) >= _MAX_RECURSIVE_LIST_ITEMS:
            return
        if entry.name == ".kiwiki":
            continue
        if entry.is_dir(follow_symlinks=False):
            _scan_markdown_recursive(Path(entry.path), root, items)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
            rel_path = os.path.relpath(entry.path, root)
            try:
                meta = _read_frontmatter_only(rel_path)
                title = meta.get("title", Path(entry.name).stem)
                updated = meta.get("updated", "")
                created = meta.get("created", "")
                tags = meta.get("tags", [])
            except Exception:
                title = Path(entry.name).stem
                updated = ""
                created = ""
                tags = []
            items.append({"path": rel_path, "title": title, "updated": updated, "created": created, "tags": tags})
