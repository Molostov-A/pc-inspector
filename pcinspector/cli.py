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
    hashes = commands.add_parser("hash", help="Вычислить хэши для поиска дубликатов")
    hashes.add_argument("--root", default="", help="Ограничить поиск папкой")
    hashes.add_argument("--min-size", default="1M", help="Минимальный размер файла")
    _global_options(hashes)
    applications = commands.add_parser("apps", help="Собрать список программ и сведения об использовании")
    _global_options(applications)
    server = commands.add_parser("serve", help="Запустить локальный веб-интерфейс")
    server.add_argument("--port", type=int, default=8765, help="Порт локального сервера")
    server.add_argument("--open", action="store_true", help="Открыть приложение в браузере")
    _global_options(server)
    brief = commands.add_parser("brief", help="Вывести Markdown-сводку по индексу")
    _global_options(brief)
    mcp = commands.add_parser("mcp", help="Запустить локальный MCP-сервер через stdio")
    _global_options(mcp)
    plan = commands.add_parser("plan", help="Просмотреть, создать или выполнить план")
    _global_options(plan)
    plan_commands = plan.add_subparsers(dest="operation", required=True)
    for name in ("list", "show", "create", "add", "execute"):
        entry = plan_commands.add_parser(name)
        _global_options(entry)
        if name in ("show", "add", "execute"):
            entry.add_argument("plan_id", type=int)
        if name == "create":
            entry.add_argument("title")
            entry.add_argument("--note", default="")
        if name == "add":
            entry.add_argument("path")
            entry.add_argument(
                "--action", choices=("recycle", "quarantine", "move", "delete_empty_dir"), default="recycle"
            )
            entry.add_argument("--target")
            entry.add_argument("--reason", default="")
        if name == "execute":
            entry.add_argument("--yes", action="store_true")

    quarantine = commands.add_parser("quarantine", help="Просмотреть, восстановить или очистить карантин")
    _global_options(quarantine)
    quarantine_commands = quarantine.add_subparsers(dest="operation", required=True)
    for name in ("list", "restore", "purge"):
        entry = quarantine_commands.add_parser(name)
        _global_options(entry)
        if name == "restore":
            entry.add_argument("action_id", type=int)
        if name == "purge":
            entry.add_argument("--older-than", type=int, default=30)
        if name != "list":
            entry.add_argument("--yes", action="store_true")
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
    return _wait_job(services.start_scan(paths, db_path=db_path), as_json)


def _wait_job(job_id: str, as_json: bool) -> int:
    previous = None
    try:
        while (status := services.job_status(job_id)).state in ("queued", "running"):
            progress = status.hash_progress or status.progress
            if progress is not None and progress != previous:
                width = max(20, shutil.get_terminal_size().columns - 1)
                if status.hash_progress is not None:
                    info = status.hash_progress
                    step = {
                        "candidates": "Отбираем кандидатов",
                        "partial": "Частичные хэши",
                        "full": "Полные хэши",
                    }
                    line = f"{step[info.phase]} · {info.processed:,}/{info.total:,} · из кэша {info.cached:,}"
                else:
                    info = status.progress
                    line = (
                        f"{info.files:,} файлов, {info.dirs:,} папок, {info.errors:,} ошибок, "
                        f"{info.files_per_second:,.0f} файлов/с | {info.path}"
                    )
                line = _shorten(line, width)
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
    elif status.apps_result is not None:
        print(f"Записей о программах: {status.apps_result['count']:,}")
        for note in status.apps_result["notes"]:
            print(note)
    elif status.hash_result is not None:
        result = status.hash_result
        print(
            f"Кандидатов {result['candidates']:,} · частичных чтений {result['partial_read']:,} · "
            f"полных чтений {result['full_read']:,} · попаданий в кэш {result['cached']:,} · "
            f"жёстких ссылок {result['hardlinks']:,} · ошибок {result['errors']:,}"
        )
    else:
        for result in status.results:
            print(
                f"{result['root']}: {result['files']:,} файлов, {result['dirs']:,} папок, "
                f"{human_size(result['size'])}, {result['errors']:,} ошибок за {result['seconds']:.2f} с"
            )
    for error in status.errors:
        print(error, file=sys.stderr)
    if status.state == "cancelled":
        message = {
            "hash": "Поиск дубликатов отменён.",
            "apps": "Сбор программ отменён.",
            "scan": "Сканирование отменено.",
        }
        print(message[status.kind], file=sys.stderr)
        return 130
    return 0 if status.state == "completed" else 1


def _confirm(assume_yes: bool, prompt: str, as_json: bool) -> None:
    if assume_yes:
        return
    if as_json:
        raise ValueError("Для выполнения с --json укажите --yes")
    print(prompt, file=sys.stderr)
    if input("Введите yes для подтверждения: ").strip().lower() != "yes":
        raise ValueError("Действие отменено")


def _plan_command(args: dict[str, Any], db_path: str | None, as_json: bool) -> int:
    operation = args["operation"]
    if operation == "list":
        result = services.list_plans(db_path=db_path)
        if not as_json:
            for plan in result:
                print(f"{plan['id']} · {plan['title']} · {plan['status']} · {plan['item_count']} пунктов")
            return 0
    elif operation == "create":
        result = services.create_plan(args["title"], note=args["note"], db_path=db_path)
    elif operation == "add":
        item = services.PlanItemIn(args["path"], args["action"], args["reason"], args["target"])
        result = services.add_plan_items(args["plan_id"], [item], db_path=db_path)
        if not as_json:
            for rejected in result["rejected"]:
                print(rejected["reason"], file=sys.stderr)
            if result["accepted"]:
                print("Пункт добавлен")
            return int(bool(result["rejected"]))
    else:
        plan = services.get_plan(args["plan_id"], db_path=db_path)
        summary = f"План {plan['id']} · {plan['title']} · {plan['selected_count']} пунктов · {human_size(plan['selected_size'])}"
        if operation == "execute":
            _confirm(args["yes"], summary, as_json)
            token = services.plan_confirmation(plan["id"], plan["revision"], db_path=db_path)
            result = services.execute_plan(plan["id"], token, db_path=db_path)
            if not as_json:
                print(
                    f"Обработано {result['done']} пунктов ({human_size(result['processed_size'])}), "
                    f"пропущено {result['skipped']}, ошибок {result['failed']}. "
                    f"Освобождено на диске {human_size(result['freed_size'])}."
                )
                for item in result["issues"]:
                    print(f"{item['path']} · {item['error']}")
                return int(result["status"] == "partially")
        else:
            result = plan
            if not as_json:
                print(summary)
                for item in plan["items"]:
                    print(f"{item['id']} · {item['action']} · {human_size(item['size'])} · {item['path']}")
                return 0
    if as_json:
        _json(result)
    elif operation == "create":
        print(f"Создан план {result['id']} · {result['title']}")
    return int(bool(result.get("rejected"))) if operation == "add" else 0


def _quarantine_command(args: dict[str, Any], db_path: str | None, as_json: bool) -> int:
    operation = args["operation"]
    if operation == "list":
        result = services.list_quarantine(db_path=db_path)
        if not as_json:
            print(f"Объём карантина · {human_size(result['size'])}")
            for item in result["items"]:
                print(f"{item['id']} · {human_size(item['size'])} · {item['src']}")
    elif operation == "restore":
        _confirm(args["yes"], f"Восстановить запись {args['action_id']}?", as_json)
        result = services.restore_action(args["action_id"], db_path=db_path)
        if not as_json:
            print(f"Восстановлено · {result['path']}" if result["restored"] else result["message"])
    else:
        days = args["older_than"]
        _confirm(args["yes"], f"Переместить в Корзину записи карантина старше {days} дней?", as_json)
        result = services.purge_quarantine(days, confirmed=True, db_path=db_path)
        if not as_json:
            print(f"В Корзину перемещено {result['count']} элементов")
            for error in result["errors"]:
                print(error["reason"], file=sys.stderr)
    if as_json:
        _json(result)
    return int(operation == "purge" and bool(result["errors"]))


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
        if command == "hash":
            return _wait_job(services.start_hash(args["root"], args["min_size"], db_path=db_path), as_json)
        if command == "apps":
            if not as_json:
                print("Собираем записи реестра и сведения о запусках…", file=sys.stderr)
            return _wait_job(services.collect_apps(db_path=db_path), as_json)
        if command == "plan":
            return _plan_command(args, db_path, as_json)
        if command == "quarantine":
            return _quarantine_command(args, db_path, as_json)
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
    except (KeyError, ValueError, OverflowError, OSError, sqlite3.Error, EOFError) as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 1
    return 0
