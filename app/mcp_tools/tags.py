"""Tag-Werkzeuge."""

from __future__ import annotations

from ..storage import _read_frontmatter_only
from ..storage import fm_tags
from ..storage import update_frontmatter
from .common import McpContext, tool
from .fileutil import _frontmatter_title_and_tags, _index_markdown, _markdown_paths, _rel_path
import json


@tool("tag_index")
async def _tool_tag_index(ctx: McpContext) -> str:
        ctx.need_read()
        tags: dict[str, list[str]] = {}
        for filepath in _markdown_paths(ctx.args.get("path", ".")):
            rel = _rel_path(filepath)
            _, file_tags, _ = _frontmatter_title_and_tags(rel)
            for tag in file_tags:
                tags.setdefault(str(tag), []).append(rel)
        result = [{"tag": tag, "count": len(files), "files": sorted(files)} for tag, files in sorted(tags.items())]
        return json.dumps({"items": result}, ensure_ascii=False, indent=2)


@tool("batch_tag")
async def _tool_batch_tag(ctx: McpContext) -> str:
        ctx.need_write()
        files = ctx.bounded_list("files", required=True)
        tags = ctx.bounded_list("tags", required=True)
        mode = ctx.args.get("mode", "merge")
        updated = []
        for path in files:
            meta = _read_frontmatter_only(path)
            # list() auf einen Skalar ergaebe aus "python" sechs einzelne
            # Buchstaben — und schriebe sie als Tags in die Notiz zurueck.
            existing_tags = fm_tags(meta.get("tags"))
            if mode == "replace":
                new_tags = tags
            else:
                new_tags = list(dict.fromkeys(existing_tags + tags))
            update_frontmatter(path, {"tags": new_tags})
            _index_markdown(path)
            updated.append({"path": path, "tags": new_tags})
        return json.dumps({"updated": updated, "count": len(updated)}, ensure_ascii=False, indent=2)


