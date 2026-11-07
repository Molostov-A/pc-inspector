import { api } from "../api.js";
import { reportTable } from "../components/reportTable.js";
import { treemap } from "../components/treemap.js";
import { el, heading, link, message, number, sized } from "../ui.js";

function breadcrumbs(path, roots) {
  const nav = el("nav", "breadcrumbs", link("Все корни", "/explorer", {}, "text-link"));
  nav.setAttribute("aria-label", "Путь к папке");
  if (!path) return nav;
  const root = roots.filter(item => item.path && item.kind === "stat" &&
    (path === item.path || path.startsWith(item.path.replace(/\\$/, "") + "\\"))
  ).sort((a, b) => b.path.length - a.path.length)[0]?.path;
  if (!root) {
    nav.append(el("span", "path", path));
    return nav;
  }
  const parts = path.slice(root.length).split("\\").filter(Boolean);
  let current = root;
  for (const part of [root, ...parts]) {
    if (part !== root) current = current.replace(/\\$/, "") + "\\" + part;
    nav.append(el("span", "crumb-divider", "/"), current === path
      ? el("span", "path", part) : link(part, "/explorer", { path: current }, "text-link path"));
  }
  return nav;
}

export async function render(container, params, signal) {
  const path = params.get("path") ?? "";
  const options = { signal };
  const [tree, nodes, roots] = await Promise.all([
    api("tree", { path }, options),
    api("treemap", { path, depth: 2 }, options),
    api("roots", {}, options),
  ]);
  const chartPanel = el("section", "panel",
    el("div", "section-head", el("h2", "", "Карта папок"),
      el("span", "muted", "Нажмите на область, чтобы открыть папку")),
  );
  const chart = el("div", "treemap");
  chart.setAttribute("aria-label", "Распределение размера папок");
  chartPanel.append(nodes.length ? chart : message("Для этой папки пока нет данных карты."));
  const current = tree.summary.path ?? path;
  const details = tree.summary.total_size === undefined ? "Папки отсортированы по размеру."
    : el("span", "", sized(tree.summary.total_size), " · ", number(tree.summary.total_files), " файлов");
  const notice = el("p", "note");
  notice.hidden = true;
  notice.setAttribute("role", "status");
  async function openPath(path) {
    notice.hidden = true;
    try {
      await api("open", {}, { method: "POST", body: { path }, signal });
      notice.textContent = "Путь открыт в Проводнике.";
      notice.className = "note";
    } catch (error) {
      if (signal.aborted) return;
      notice.textContent = error.message;
      notice.className = "error";
    }
    notice.hidden = false;
  }
  container.replaceChildren(
    heading("Распределение места", "Обзор", details),
    breadcrumbs(current, roots.findings),
    chartPanel,
    el("section", "panel", el("h2", "", current ? "Содержимое папки" : "Просканированные корни"),
      notice, reportTable(tree, { explorer: true, openPath })),
  );
  if (nodes.length && globalThis.echarts) return treemap(chart, nodes);
  if (nodes.length) chart.replaceWith(message("Карту не удалось загрузить. Данные доступны в таблице."));
}
