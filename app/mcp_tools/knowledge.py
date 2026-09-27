"""Knowledge-Werkzeuge: Suche, Entitaeten, Status."""

from __future__ import annotations

from .common import McpContext, tool
import json


@tool("knowledge_search")
async def _tool_knowledge_search(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import search_knowledge

        return json.dumps(
            search_knowledge(ctx.args["query"], ctx.args.get("limit", 20)),
            ensure_ascii=False,
        )


@tool("knowledge_status")
async def _tool_knowledge_status(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import knowledge_status

        return json.dumps(knowledge_status(), ensure_ascii=False)


@tool("knowledge_reindex")
async def _tool_knowledge_reindex(ctx: McpContext) -> str:
        ctx.need_write()
        from ..knowledge.service import rebuild_current_workspace

        return json.dumps(rebuild_current_workspace(), ensure_ascii=False)


@tool("entity_details")
async def _tool_entity_details(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import entity_details

        return json.dumps(entity_details(ctx.args["entity_id"]), ensure_ascii=False)


@tool("entity_neighbors")
async def _tool_entity_neighbors(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import entity_neighbors

        return json.dumps(
            entity_neighbors(ctx.args["entity_id"], ctx.args.get("depth", 1), ctx.args.get("limit", 20)),
            ensure_ascii=False,
        )


@tool("fact_timeline")
async def _tool_fact_timeline(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import fact_timeline

        return json.dumps(
            fact_timeline(ctx.args["entity_id"], ctx.args.get("limit", 20)),
            ensure_ascii=False,
        )


@tool("explain_relation")
async def _tool_explain_relation(ctx: McpContext) -> str:
        ctx.need_read()
        from ..knowledge.service import explain_relation

        return json.dumps(explain_relation(ctx.args["relation_id"]), ensure_ascii=False)


