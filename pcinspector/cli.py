"""Разбор команд и представление результатов сервисов в терминале."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict
from typing import Any

from . import services
from .core.fmt import human_size, ts_date
from .core.models import Report


def _global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS, help="Путь к SQLite-индексу")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Вывести JSON")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcinspector", description="Локальный анализ дискового пространства"
    )
    _global_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Просканировать указанные папки или локальные диски NTFS")
    scan.add_argument("paths", nargs="*", metavar="PATH")
    _global_options(scan)
    server = commands.add_parser("serve", help="Запустить локальный веб-интерфейс")
    server.add_argument("--port", type=int, default=8765, help="Порт локального сервера")
    server.add_argument("--open", action="store_true", help="Открыть приложение в браузере")
    _global_options(server)
    brief = commands.add_parser("brief", help="Вывести Markdown-сводку по индексу")
    _global_options(brief)
    mcp = commands.add_parser("mcp", help="Запустить локальный MCP-сервер через stdio")
    _global_options(mcp)
    listing = commands.add_parser("analyzers", help="Показать список анализаторов")
    _global_options(listing)
    sql = commands.add_parser("sql", help="Выполнить SQL-запрос только для чтения")
    sql.add_argument("query", metavar="QUERY")
    sql.add_argument("--limit", type=int, default=500, help="Максимум строк результата")
    _global_options(sql)

    for info in services.list_analyzers():
        analyzer = commands.add_parser(info["id"], help=info["title"], description=info["description"])
        _global_options(analyzer)
        if info["id"] == "tree":
            analyzer.add_argument("path", nargs="?", default=argparse.SUPPRESS, help="Папка в индексе")
        for param in info["params"]:
            options: dict[str, Any] = {"help": param["help"], "default": argparse.SUPPRESS}
            if param["type"] == "choice":
                options["choices"] = param["choices"]
            if param["type"] == "bool":
                options.update(nargs="?", const="true", metavar="true|false")
            analyzer.add_argument("--" + param["name"].replace("_", "-"), **options)
    return parser


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _shorten(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    left = (width - 1) // 2
    right = width - 1 - left
    return f"{value[:left]}…{value[-right:]}"


def _report(report: Report, as_json: bool) -> None:
    if as_json:
        _json(report.to_dict())
        return
    width = max(40, shutil.get_terminal_size().columns)
    path_width = max(12, (width - 26) * 3 // 5)
    reason_width = max(8, width - 26 - path_width)
    print(report.title)
    print(f"{'Размер':>10} | {'Оценка':<7} | {'Путь или название':<{path_width}} | Причина")
    for item in report.findings:
        label = item.title if item.title.startswith("[") else item.path or item.title
        print(
            f"{human_size(item.size):>10} | {item.safety:<7} | "
            f"{_shorten(label, path_width):<{path_width}} | {_shorten(item.reason or '', reason_width)}"
        )
        if report.analyzer == "roots" and item.kind == "stat":
            scanned = ts_date(item.meta.get("scanned_at"))
            free = human_size(item.meta.get("disk_free"))
            capacity = human_size(item.meta.get("disk_total"))
            print(f"             Скан: {scanned}, свободно {free} из {capacity}")
    print(f"Итого по показанным строкам: {human_size(report.total_size)}")
    for note in report.notes:
        print(note)


def _scan(paths: list[str], db_path: str | None, as_json: bool) -> int:
    job_id = services.start_scan(paths, db_path=db_path)
    previous = None
    try:
        while (status := services.job_status(job_id)).state in ("queued", "running"):
            progress = status.progress
            if progress is not None and progress != previous:
                width = max(20, shutil.get_terminal_size().columns - 1)
                stats = (
                    f"{progress.files:,} файлов, {progress.dirs:,} папок, {progress.errors:,} ошибок, "
                    f"{progress.files_per_second:,.0f} файлов/с"
                )
                line = _shorten(f"{stats} | {progress.path}", width)
                print(f"\r{line:<{width}}", end="", file=sys.stderr, flush=True)
                previous = progress
            time.sleep(0.1)
    except KeyboardInterrupt:
        services.cancel_job(job_id)
        while (status := services.job_status(job_id)).state in ("queued", "running"):
            time.sleep(0.1)
    finally:
        if previous is not None:
            print(file=sys.stderr)

    if as_json:
        _json(asdict(status))
    else:
        for result in status.results:
            print(
                f"{result['root']}: {result['files']:,} файлов, {result['dirs']:,} папок, "
                f"{human_size(result['size'])}, {result['errors']:,} ошибок за {result['seconds']:.2f} с"
            )
    for error in status.errors:
        print(error, file=sys.stderr)
    if status.state == "cancelled":
        print("Сканирование отменено.", file=sys.stderr)
        return 130
    return 0 if status.state == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = vars(_parser().parse_args(argv))
    command = args.pop("command")
    as_json = args.pop("json", False)
    db_path = args.pop("db", None)

    try:
        if command == "scan":
            return _scan(args["paths"], db_path, as_json)
        if command == "serve":
            from .api.app import serve

            serve(args["port"], args["open"], db_path)
            return 0
        if command == "brief":
            summary = services.ai_brief(db_path=db_path)
            if as_json:
                _json({"markdown": summary})
            else:
                print(summary, end="")
            return 0
        if command == "mcp":
            from .mcp_server import create_server

            create_server(db_path=db_path).run(transport="stdio")
            return 0
        if command == "analyzers":
            analyzers = services.list_analyzers()
            if as_json:
                _json(analyzers)
            else:
                for info in analyzers:
                    print(f"{info['id']} — {info['title']}")
                    for param in info["params"]:
                        flag = param["name"].replace("_", "-")
                        print(f"  --{flag}: {param['help']} (по умолчанию {param['default']!r})")
        elif command == "sql":
            rows = services.sql_readonly(args["query"], args["limit"], db_path=db_path)
            if as_json:
                _json(rows)
            elif rows:
                print(" | ".join(rows[0]))
                for row in rows:
                    print(" | ".join(str(value) if value is not None else "—" for value in row.values()))
        else:
            _report(services.run_analyzer(command, db_path=db_path, **args), as_json)
    except (KeyError, ValueError, OverflowError, OSError, sqlite3.Error) as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 1
    return 0
