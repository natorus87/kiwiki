import os
from pathlib import Path
from datetime import datetime, timezone
import frontmatter
from .models import FileInfo, FileContent
from .tenancy import BASE_DATA_DIR, user_root

# Backwards-compat: external imports of DATA_DIR still resolve, but only point
# at the base (shared) data directory. All storage functions below operate on
# the *current user's* root via user_root().
DATA_DIR = BASE_DATA_DIR

# Max bytes to read for frontmatter-only extraction (YAML frontmatter is
# always at the top of the file and typically < 1 KB).
_FRONTMATTER_READ_LIMIT = 8192


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
    """
    file_path = safe_path(path)
    if not file_path.exists() or not file_path.is_file():
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read(_FRONTMATTER_READ_LIMIT)
        post = frontmatter.loads(raw)
        return post.metadata
    except Exception:
        return {}


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
    return FileContent(path=path, content=post.content, frontmatter=post.metadata)


def write_file(path: str, content: str) -> FileContent:
    """
    Write markdown file.
    Creates directories if needed.
    Updates 'updated' timestamp in frontmatter.
    """
    file_path = safe_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.loads(content) if content else frontmatter.Post("")
    post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return FileContent(path=path, content=post.content, frontmatter=post.metadata)


def append_file(path: str, content: str) -> FileContent:
    """
    Append content to markdown file.
    Updates 'updated' timestamp.
    """
    file_path = safe_path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)
    post.content += "\n" + content
    post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return FileContent(path=path, content=post.content, frontmatter=post.metadata)


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
    for item in sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
        if item.name == ".kiwiki":
            continue
        rel_path = str(item.relative_to(root))
        if item.is_dir():
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
        else:
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
    return items


def delete_file(path: str) -> None:
    """
    Delete a markdown file.
    Raises FileNotFoundError if the file does not exist.
    Raises ValueError if the path is not a .md file or not a regular file.
    """
    file_path = safe_path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Not a file: {path}")
    if not path.endswith(".md"):
        raise ValueError("Only .md files may be deleted")
    file_path.unlink()


def create_folder(path: str) -> None:
    """Create a folder."""
    dir_path = safe_path(path)
    dir_path.mkdir(parents=True, exist_ok=True)


def delete_folder(path: str) -> None:
    """
    Recursively delete a folder and all its contents.
    Raises FileNotFoundError if not found, ValueError if not a directory
    or if path is a top-level protected folder.
    """
    import shutil

    if not path:
        raise ValueError("Cannot delete the data root directory")
    dir_path = safe_path(path)
    if not dir_path.exists():
        raise FileNotFoundError(f"Folder not found: {path}")
    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    # Prevent deleting the user's data root itself
    if dir_path.resolve() == user_root().resolve():
        raise ValueError("Cannot delete the data root directory")
    shutil.rmtree(dir_path)


def create_note(title: str, content: str, tags: list[str], owner: str, folder: str = "notes") -> str:
    """
    Create a new note file with frontmatter.
    Slug is based on title. Folder can be any sub-path (e.g. 'notes/python').
    Returns relative path to created file.
    """
    folder = folder.strip("/").replace("..", "").strip() or "notes"
    slug = title.lower().replace(" ", "-").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:60]
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
    file_path = safe_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return path


def edit_file(path: str, new_str: str, old_str: str = "") -> FileContent:
    """
    Edit a file's content without touching frontmatter.
    If old_str is provided: find-and-replace first occurrence.
    If old_str is empty/omitted: append new_str to the end.
    """
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
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return FileContent(path=path, content=post.content, frontmatter=post.metadata)


def update_frontmatter(path: str, updates: dict) -> FileContent:
    """
    Merge updates into the frontmatter of an existing file without touching its content.
    Always sets 'updated' to today.
    """
    file_path = safe_path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)
    post.metadata.update(updates)
    post.metadata["updated"] = datetime.now(timezone.utc).isoformat().split("T")[0]
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return FileContent(path=path, content=post.content, frontmatter=post.metadata)


def move_folder(src: str, dst: str) -> None:
    """Move/rename a folder within DATA_DIR."""
    import shutil

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


def move_file(src: str, dst: str) -> FileContent:
    """
    Move/rename a markdown file within DATA_DIR.
    Creates destination directories if needed.
    Updates the search index automatically.
    """
    src_path = safe_path(src)
    dst_path = safe_path(dst)
    if not src_path.exists():
        raise FileNotFoundError(f"Source not found: {src}")
    if not src_path.is_file():
        raise ValueError(f"Source is not a file: {src}")
    if not src.endswith(".md") or not dst.endswith(".md"):
        raise ValueError("Only .md files may be moved")
    if dst_path.exists():
        raise ValueError(f"Destination already exists: {dst}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.rename(dst_path)
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
    items = []
    for item in sorted(dir_path.rglob("*.md"), key=lambda x: str(x)):
        if ".kiwiki" in str(item):
            continue
        rel_path = str(item.relative_to(root))
        try:
            meta = _read_frontmatter_only(rel_path)
            title = meta.get("title", item.stem)
            updated = meta.get("updated", "")
            tags = meta.get("tags", [])
        except Exception:
            title = item.stem
            updated = ""
            tags = []
        items.append({"path": rel_path, "title": title, "updated": updated, "tags": tags})
    return items
