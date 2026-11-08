import { api } from "../api.js";
import { reportTable } from "../components/reportTable.js";
import { addToPlan } from "../components/planDialog.js";
import { treemap } from "../components/treemap.js";
import { button, el, heading, link, message, number, sized } from "../ui.js";

function breadcrumbs(path, roots, files) {
  const mode = { files: files ? "1" : "0" };
  const nav = el("nav", "breadcrumbs", link("Все корни", "/explorer", mode, "text-link"));
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
      ? el("span", "path", part) : link(part, "/explorer", { ...mode, path: current }, "text-link path"));
  }
  return nav;
}

export async function render(container, params, signal) {
  const path = params.get("path") ?? "";
  const files = params.get("files") !== "0";
  const options = { signal };
  const [tree, nodes, roots] = await Promise.all([
    api("tree", { path, files }, options),
    api("treemap", { path, depth: 2, files }, options),
    api("roots", {}, options),
  ]);
  const chartPanel = el("section", "panel",
    el("div", "section-head", el("h2", "", files ? "Карта папок и файлов" : "Карта папок"),
      link(files ? "Только папки" : "Показать файлы", "/explorer", { path, files: files ? "0" : "1" }, "text-link")),
  );
  const chart = el("div", "treemap");
  chart.setAttribute("aria-label", "Распределение размера папок и файлов");
  chartPanel.append(nodes.length ? chart : message("Для этой папки пока нет данных карты."));
  const current = tree.summary.path ?? path;
  const details = tree.summary.total_size === undefined ? "Папки отсортированы по размеру."
    : el("span", "", sized(tree.summary.total_size), " · ", number(tree.summary.total_files), " файлов");
  const notice = el("p", "note");
  const feedback = el("div", "", "");
  feedback.hidden = true;
  notice.hidden = true;
  notice.setAttribute("role", "status");
  async function openPath(path) {
    notice.hidden = true;
    try {
      await api("open", {}, { method: "POST", body: { path }, signal, activity: "Открываем Проводник…" });
      notice.textContent = "Запрос передан Проводнику.";
      notice.className = "note";
    } catch (error) {
      if (signal.aborted) return;
      notice.textContent = error.message;
      notice.className = "error";
    }
    notice.hidden = false;
  }
  container.replaceChildren(
    heading("Распределение места", "Обзор", details, tree.summary.path
      ? button("Открыть папку в Проводнике", "secondary", () => openPath(tree.summary.path)) : null),
    breadcrumbs(current, roots.findings, files),
    chartPanel,
    el("section", "panel", el("h2", "", current ? "Содержимое папки" : "Просканированные корни"),
      notice, feedback, reportTable(tree, {
        explorer: true, openPath, showFiles: files,
        planControl: item => button("В план", "secondary small", () => addToPlan([{
          path: item.path, reason: item.reason ?? "", safety: "review", source: "tree",
        }], feedback)),
      })),
  );
  if (nodes.length && globalThis.echarts) return treemap(chart, nodes, openPath, files);
  if (nodes.length) chart.replaceWith(message("Карту не удалось загрузить. Данные доступны в таблице."));
}
