"""Локальный MCP-интерфейс для чтения индекса и запуска скана."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import services
from .core.models import Report

MAX_FINDINGS = 200


def _limited_report(report: Report) -> dict[str, Any]:
    if len(report.findings) > MAX_FINDINGS:
        report.findings = report.findings[:MAX_FINDINGS]
        report.notes.append(f"Показаны первые {MAX_FINDINGS} находок.")
    return report.to_dict()


def create_server(*, db_path: Path | str | None = None) -> MCPServer:
    server = MCPServer("PC Inspector")

    @server.tool(description="Read a Russian Markdown summary of disk use, cleanup candidates, and scan age.")
    def get_brief() -> str:
        return services.ai_brief(db_path=db_path)

    @server.tool(description="List registered analyzers, their descriptions, and parameter schemas.")
    def list_analyzers() -> list[dict[str, Any]]:
        return services.analyzer_catalog()

    @server.tool(description="Run an analyzer on the local index. Returns up to 200 findings.")
    def run_analyzer(analyzer_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(params or {})
        if options.keys() & {"db_path", "analyzer_id"}:
            raise ValueError("Недопустимый параметр анализатора")
        if options.get("limit") is not None:
            options["limit"] = min(int(options["limit"]), MAX_FINDINGS + 1)
        return _limited_report(services.run_analyzer(analyzer_id, db_path=db_path, **options))

    @server.tool(description="List indexed child directories or scan roots. Limit: 1 to 200.")
    def tree(path: str = "", limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= MAX_FINDINGS:
            raise ValueError("Лимит дерева должен быть от 1 до 200 строк")
        return _limited_report(services.tree(path, limit, db_path=db_path))

    @server.tool(
        description=(
            "Run read-only SQLite SELECT, WITH, or EXPLAIN. "
            "Schema: roots (scan totals), dirs (paths, parent_id, total_size, protected), "
            "files_v (path, name, ext, size, mtime, atime, attrs, protected), errors (path, message). "
            "Returns at most 200 rows."
        )
    )
    def sql_query(query: str, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= MAX_FINDINGS:
            raise ValueError("Лимит SQL должен быть от 1 до 200 строк")
        return services.sql_readonly(query, limit, db_path=db_path)

    @server.tool(
        description="Start a local read-only filesystem scan; it writes metadata to the SQLite index."
    )
    def start_scan(paths: list[str]) -> dict[str, str]:
        return {"job_id": services.start_scan(paths, db_path=db_path)}

    @server.tool(description="Get scan job state, progress, and errors by job ID.")
    def job_status(job_id: str) -> dict[str, Any]:
        return asdict(services.job_status(job_id))

    return server
