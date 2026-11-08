import { date, el, link, message, number, sized } from "../ui.js";
import { safetyBadge, safetyLabels } from "./safetyBadge.js";
import { sizeBar } from "./sizeBar.js";

const label = item => item.title.startsWith("[") ? item.title : item.path || item.title;

export function reportTable(report, { explorer = false, openPath = null, showFiles = true, planControl = null, appControl = null } = {}) {
  const panel = el("div", "report");
  for (const note of report.notes ?? []) panel.append(message(note, "note"));
  const rows = report.findings ?? [];
  if (!rows.length) {
    panel.append(message("По этим параметрам ничего не найдено."));
    return panel;
  }
  const total = report.summary?.total_size ?? report.total_size;
  const cols = [
    { name: appControl ? "Программа" : explorer ? "Папка или файл" : "Путь или название", value: item => appControl ? item.title : label(item),
      cell: item => explorer && item.kind === "dir" && item.path
        ? link(item.title, "/explorer", { path: item.path, files: showFiles ? "1" : "0" }, "text-link")
        : appControl ? el("div", "", item.title, item.meta.version ? el("p", "muted", item.meta.version) : null)
          : explorer ? item.title : label(item) },
    { name: "Размер", value: item => item.size, cell: item => sized(item.size) },
    ...(explorer ? [
      { name: "Доля", value: item => item.size, cell: item => sizeBar(item.size, total) },
      { name: "Файлов", value: item => item.meta.files ?? 0, cell: item => number(item.meta.files) },
      { name: "Последнее изменение", value: item => item.meta.newest_mtime ?? 0,
        cell: item => date(item.meta.newest_mtime) },
    ] : appControl ? [
      { name: "Издатель", value: item => item.meta.publisher ?? "", cell: item => item.meta.publisher ?? "—" },
      { name: "Последний запуск", value: item => item.meta.last_run ?? 0, cell: item => date(item.meta.last_run) },
      { name: "Достоверность", value: item => item.meta.confidence ?? "", cell: item => ({
        prefetch: "Высокая · Prefetch", userassist: "Средняя · UserAssist", atime: "Низкая · дата доступа",
      })[item.meta.source] ?? "Нет данных" },
    ] : [
      { name: "Оценка", value: item => safetyLabels[item.safety], cell: item => safetyBadge(item.safety) },
      { name: "Причина", value: item => item.reason ?? "", cell: item => item.reason ?? "—" },
    ]),
  ];
  const input = el("input", "filter");
  input.type = "search";
  input.placeholder = "Фильтр по результатам";
  input.setAttribute("aria-label", input.placeholder);
  const count = el("span", "muted");
  const head = el("tr");
  const body = el("tbody");
  let selected = 1;
  let direction = -1;
  const headers = cols.map((col, index) => {
    const control = el("button", "sort", col.name);
    control.type = "button";
    control.addEventListener("click", () => {
      direction = selected === index ? -direction : (index === 0 ? 1 : -1);
      selected = index;
      update();
    });
    const header = el("th", "", control);
    header.scope = "col";
    head.append(header);
    return header;
  });
  function update() {
    const search = input.value.trim().toLocaleLowerCase("ru");
    const visible = rows.filter(item =>
      `${item.title} ${item.path ?? ""} ${item.reason ?? ""} ${safetyLabels[item.safety] ?? ""} ${item.meta?.publisher ?? ""}`
        .toLocaleLowerCase("ru").includes(search),
    );
    visible.sort((a, b) => {
      const x = cols[selected].value(a);
      const y = cols[selected].value(b);
      return direction * (typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ru"));
    });
    headers.forEach((header, index) => header.setAttribute(
      "aria-sort", selected === index ? (direction === 1 ? "ascending" : "descending") : "none",
    ));
    count.textContent = `${number(visible.length)} из ${number(rows.length)}`;
    body.replaceChildren(...visible.map(item => {
      let action;
      if (openPath && item.path && (item.kind === "dir" || (item.kind === "file" && item.path !== report.summary?.path))) {
        action = el("button", "secondary small", "Открыть в Проводнике");
        action.type = "button";
        action.addEventListener("click", () => openPath(item.path));
      }
      const control = item.path && item.safety !== "keep" && item.path !== report.summary?.path
        ? planControl?.(item) : null;
      return el("tr", "", ...cols.map(col => el("td", "", col.cell(item))),
        openPath || planControl || appControl
          ? el("td", "", el("div", "row-actions", action, control, appControl?.(item))) : null);
    }));
  }
  if (openPath || planControl || appControl) {
    const actionHeader = el("th", "", "Действие");
    actionHeader.scope = "col";
    head.append(actionHeader);
  }
  input.addEventListener("input", update);
  update();
  panel.append(el("div", "table-tools", input, count),
    el("div", "table-scroll", el("table", "", el("thead", "", head), body)));
  return panel;
}
