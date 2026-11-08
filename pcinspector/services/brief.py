"""Компактная Markdown-сводка о состоянии ПК для ИИ."""

from __future__ import annotations

import time
from datetime import datetime

from ..analyzers import get
from ..core.fmt import DAY, human_size
from .workspace import DbPath, read


def _markdown_cell(value: str) -> str:
    return (
        " ".join(value.splitlines())
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace(chr(96), "&#96;")
    )


def ai_brief(max_items: int = 15, *, db_path: DbPath = None) -> str:
    """Сформировать краткую Markdown-сводку из одного снимка SQLite."""
    if not 1 <= max_items <= 200:
        raise ValueError("Число строк сводки должно быть от 1 до 200")
    limit = min(max_items, 10)
    with read(db_path) as con:
        roots = get("roots")(con)
        indexed = [item for item in roots.findings if item.kind == "stat"]
        lines = ["# PC Inspector — сводка по индексу", ""]
        if not indexed:
            return "\n".join([*lines, "Индекс пуст. Запустите сканирование нужной папки или диска.", ""])
        lines.extend(
            [
                "## Просканированные диски и папки",
                "",
                "| Корень | В индексе | Свободно | Ёмкость | Последний скан |",
                "| --- | ---: | ---: | ---: | --- |",
            ]
        )
        stale = []
        now = time.time()
        for item in indexed:
            timestamp = item.meta.get("scanned_at")
            date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "—"
            if not timestamp or now - timestamp > 7 * DAY:
                stale.append(_markdown_cell(item.title))
            lines.append(
                f"| {_markdown_cell(item.title)} | {human_size(item.size)} | "
                f"{human_size(item.meta.get('disk_free'))} | "
                f"{human_size(item.meta.get('disk_total'))} | {date} |"
            )
        if stale:
            lines.extend(["", "Данные старше 7 дней или дата неизвестна — " + ", ".join(stale) + "."])
        junk = get("junk")(con, limit=min(5, max_items))
        lines.extend(
            [
                "",
                "## Что можно освободить",
                "",
                f"- Можно освободить (safe) — {human_size(junk.summary['safe_size'])}.",
                f"- Требуется проверка (review) — {human_size(junk.summary['review_size'])}.",
            ]
        )
        lines.extend(
            f"- {_markdown_cell(item.title)} — {human_size(item.size)} ({item.safety})."
            for item in junk.findings
        )
        for depth in (1, 2):
            lines.extend(["", f"## Крупные папки уровня {depth}", ""])
            folders = con.execute(
                "SELECT path, total_size FROM dirs WHERE depth = ? AND total_size > 0 "
                "ORDER BY total_size DESC, path LIMIT ?",
                (depth, limit),
            ).fetchall()
            lines.extend(
                f"- {_markdown_cell(row['path'])} — {human_size(row['total_size'])}." for row in folders
            )
            if not folders:
                lines.append("Подходящих папок нет.")
        forgotten = get("forgotten_dirs")(con, limit=limit)
        lines.extend(["", "## Забытые папки", ""])
        lines.extend(
            f"- {_markdown_cell(item.path or item.title)} — {human_size(item.size)}. {item.reason}"
            for item in forgotten.findings
        )
        if not forgotten.findings:
            lines.append("Подходящих папок нет.")
        lines.extend(["", "## Дополнительные данные", ""])
        duplicates = get("duplicates")(con, limit=min(5, max_items))
        if duplicates.findings:
            lines.append("### Дубликаты")
            lines.extend(
                f"- {_markdown_cell(item.title)} — {human_size(item.size)} потенциально, {item.meta['count']} копий."
                for item in duplicates.findings
            )
        elif not duplicates.notes:
            lines.append("Групп дубликатов в выбранном размере не найдено.")
        lines.extend(duplicates.notes)
        applications = get("unused_apps")(con, limit=limit)
        lines.extend(["", "### Неиспользуемые программы"])
        lines.extend(
            f"- {_markdown_cell(item.title)} — {human_size(item.size)}. {item.reason}."
            for item in applications.findings
        )
        lines.extend(applications.notes)
        if not applications.findings and "collected_at" in applications.summary:
            lines.append("В выбранном диапазоне подходящих программ нет.")
        growth = get("growth")(con, limit=limit)
        lines.extend(["", "### Рост папок"])
        for item in growth.findings:
            sign = "+" if item.meta["delta"] > 0 else "−"
            lines.append(f"- {_markdown_cell(item.title)} — {sign}{human_size(item.size)}.")
        lines.extend(growth.notes)

    text = "\n".join(lines) + "\n"
    if len(text) > 12_000:
        note = "\n\nСводка сокращена до 12 000 символов.\n"
        return text[: 12_000 - len(note)].rsplit("\n", 1)[0] + note
    return text
