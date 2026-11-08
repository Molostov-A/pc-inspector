"""Приёмка MCP через stdio, ограничения инструментов и сводка (F7, F8)."""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from pcinspector import cli, services
from pcinspector.core import db, scanner


def test_brief(index: Path, fake_tree: Path, tmp_path: Path) -> None:
    with closing(db.connect(index)) as con, con:
        con.execute("UPDATE roots SET scanned_at = ?", (time.time() - 8 * 86_400,))
    report = services.ai_brief(db_path=index)
    assert str(fake_tree) in report
    assert "старше 7 дней" in report and "(safe)" in report and "(review)" in report
    assert "Крупные папки уровня 2" in report and "Забытые папки" in report
    assert len(report) <= 12_000
    assert "Индекс пуст" in services.ai_brief(db_path=tmp_path / "missing.db")


def test_cli_brief(index: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["brief", "--db", str(index)]) == 0
    assert "# PC Inspector" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_tool_contract_and_readonly(fake_tree: Path, index: Path, tmp_path: Path, mode: str) -> None:
    for n in range(205):
        (fake_tree / f"data-{n:03}.bin").write_bytes(b"x")
    with closing(db.connect(index)) as con:
        scanner.scan(str(fake_tree), con)
    other = tmp_path / "Другой корень"
    other.mkdir()
    runner = (
        "import runpy,sys; from pathlib import Path; from pcinspector import config; "
        "config.DATA_DIR=Path(sys.argv.pop(1)); runpy.run_module('pcinspector',run_name='__main__')"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", runner, str(tmp_path / "data"), "mcp", "--db", str(index)],
        cwd=Path(__file__).resolve().parents[1],
    )

    async def check() -> None:
        async with Client(params, read_timeout_seconds=10, mode=mode) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "get_brief",
                "list_analyzers",
                "run_analyzer",
                "tree",
                "sql_query",
                "start_scan",
                "job_status",
            }
            summary = await client.call_tool("get_brief")
            assert not summary.is_error
            assert "сводка" in str(summary.content)
            catalog = await client.call_tool("list_analyzers")
            assert not catalog.is_error and "top_files" in str(catalog.structured_content)
            tree = await client.call_tool("tree", {"path": str(fake_tree)})
            assert not tree.is_error and tree.structured_content["findings"]
            report = await client.call_tool(
                "run_analyzer", {"analyzer_id": "top_files", "params": {"limit": 900}}
            )
            assert not report.is_error
            assert len(report.structured_content["findings"]) == 200
            assert "200" in report.structured_content["notes"][-1]
            for query in (
                "DELETE FROM files",
                "WITH x AS (SELECT 1) DELETE FROM files",
                "SELECT * FROM pragma_wal_checkpoint",
                "ATTACH DATABASE ':memory:' AS other",
                "SELECT 1; DELETE FROM files",
            ):
                result = await client.call_tool("sql_query", {"query": query})
                assert result.is_error
            read = await client.call_tool("sql_query", {"query": "SELECT count(*) AS total FROM files"})
            assert not read.is_error and "211" in str(read.structured_content)
            assert not (await client.call_tool("sql_query", {"query": "EXPLAIN SELECT 1"})).is_error
            started = await client.call_tool("start_scan", {"paths": [str(other)]})
            assert not started.is_error
            for _ in range(100):
                status = await client.call_tool("job_status", started.structured_content)
                assert not status.is_error
                if status.structured_content["state"] == "completed":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Сканирование не завершилось")

    asyncio.run(asyncio.wait_for(check(), timeout=30))
