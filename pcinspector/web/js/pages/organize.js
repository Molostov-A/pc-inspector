import { api } from "../api.js";
import { addToPlan } from "../components/planDialog.js";
import { reportTable } from "../components/reportTable.js";
import { button, el, heading, href, link, message } from "../ui.js";

const tabs = {
  downloads_sort: { title: "Разложить загрузки", param: "path", label: "Папка загрузок" },
  installed_installers: { title: "Установщики", param: "path", label: "Папка загрузок" },
  empty_dirs: { title: "Пустые папки", param: "root", label: "Папка поиска" },
  scattered_projects: { title: "Разрозненные проекты", param: "root", label: "Папка поиска" },
  big_media: { title: "Крупные медиа", param: "min_size", label: "Минимальный размер", initial: "1G" },
  growth: { title: "Рост папок", param: "min_delta", label: "Минимальное изменение", initial: "500M" },
};

export async function render(container, params, signal) {
  const requested = params.get("tab");
  const active = Object.hasOwn(tabs, requested) ? requested : "downloads_sort";
  const { param, label, initial, title } = tabs[active];
  const preview = active === "downloads_sort";
  const nav = el("nav", "row-actions");
  nav.setAttribute("aria-label", "Вкладки раздела «Порядок»");
  for (const [id, tab] of Object.entries(tabs)) {
    const entry = link(tab.title, "/organize", { tab: id }, `tab${id === active ? " selected" : ""}`);
    if (id === active) entry.setAttribute("aria-current", "page");
    nav.append(entry);
  }
  const input = el("input");
  input.value = params.get(param) ?? initial ?? "";
  if (param === "path") input.placeholder = "%USERPROFILE%\\Downloads";
  const apply = el("button", "secondary", "Показать");
  apply.type = "submit";
  const form = el("form", "row-actions", el("label", "field", label, input), apply);
  form.addEventListener("submit", event => {
    event.preventDefault();
    location.hash = href("/organize", { tab: active, [param]: input.value.trim() });
  });
  const results = el("section", "panel", message("Читаем индекс…"));
  container.replaceChildren(
    heading("Разбор файлов", "Порядок", "Проверьте, что можно упорядочить и как изменились размеры папок."),
    nav, form, results,
  );
  const report = await api(`analyzers/${active}`, { [param]: input.value.trim(), limit: 100 }, { signal });
  const feedback = el("div");
  feedback.hidden = true;
  feedback.setAttribute("role", "status");
  const selection = new Map();
  const planItem = item => ({
    path: item.path, source: active, safety: item.safety, reason: item.reason ?? "",
    action: item.meta.action, target: item.meta.target,
  });
  const addSelected = button("В план", "primary", () => addToPlan([...selection.values()].map(planItem), feedback));
  addSelected.disabled = true;
  const control = item => {
    if (!preview) return button("В план", "secondary small", () => addToPlan([planItem(item)], feedback));
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Выбрать ${item.title}`);
    checkbox.checked = selection.has(item.path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selection.set(item.path, item);
      else selection.delete(item.path);
      addSelected.disabled = !selection.size;
      addSelected.textContent = selection.size ? `В план · ${selection.size}` : "В план";
    });
    return checkbox;
  };
  results.replaceChildren(
    el("div", "section-head", el("h2", "", title), preview && report.findings.length ? addSelected : null),
    ...(preview ? [message("Отметьте строки в предпросмотре. Перемещения выполняются после подтверждения плана.", "note")] : []),
    feedback,
    reportTable(report, { preview, delta: active === "growth", planControl: active === "growth" ? null : control }),
  );
}
