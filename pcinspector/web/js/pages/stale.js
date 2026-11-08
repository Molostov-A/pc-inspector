import { beginActivity } from "../activity.js";
import { api } from "../api.js";
import { addToPlan, modal } from "../components/planDialog.js";
import { reportTable } from "../components/reportTable.js";
import { button, date, el, heading, href, link, message } from "../ui.js";

const tabs = {
  forgotten_dirs: "Забытые папки",
  stale_files: "Давно не открытые файлы",
  unused_apps: "Неиспользуемые программы",
  app_leftovers: "Возможные остатки",
};
let collection = null;

function updateInventory() {
  if (collection) return collection;
  const finish = beginActivity("Собираем список программ и признаки их запуска…");
  collection = (async () => {
    try {
      const { job_id: id } = await api("apps/collect", {}, { method: "POST" });
      while (true) {
        const status = await api(`jobs/${id}`);
        if (status.state === "completed") return status.apps_result;
        if (status.state === "failed" || status.state === "cancelled") {
          throw new Error(status.errors[0] || "Сбор сведений о программах не завершён.");
        }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    } finally {
      finish();
      collection = null;
    }
  })();
  return collection;
}

export async function render(container, params, signal) {
  const requested = params.get("tab");
  const active = Object.hasOwn(tabs, requested) ? requested : "unused_apps";
  const isApps = active === "unused_apps";
  const inventory = isApps || active === "app_leftovers";
  const nav = el("nav", "row-actions");
  nav.setAttribute("aria-label", "Вкладки раздела «Забытое»");
  for (const [id, title] of Object.entries(tabs)) {
    const tab = link(title, "/stale", { tab: id }, `tab${id === active ? " selected" : ""}`);
    if (id === active) tab.setAttribute("aria-current", "page");
    nav.append(tab);
  }
  const results = el("section", "panel");
  const feedback = el("div");
  feedback.setAttribute("role", "status");
  const error = el("p", "error");
  error.setAttribute("role", "alert");
  const defaultDays = isApps ? "180" : "365";
  const days = params.get("days") ?? defaultDays;
  const controls = el("div", "row-actions");
  if (active !== "app_leftovers") {
    const input = el("input");
    input.type = "number";
    input.min = "0";
    input.step = "1";
    input.required = true;
    input.value = days;
    const apply = el("button", "secondary", "Применить");
    apply.type = "submit";
    const form = el("form", "row-actions", el("label", "field", "Дней без запуска или изменений", input), apply);
    form.addEventListener("submit", event => {
      event.preventDefault();
      location.hash = href("/stale", { tab: active, days: input.value });
    });
    controls.append(form);
  }
  if (isApps) controls.append(link("Все программы", "/stale", { tab: active, days: "0" }, "text-link"));
  const reload = inventory ? button("Обновить список", "primary", async () => {
    reload.disabled = true;
    error.textContent = "";
    try {
      await updateInventory();
      if (!signal.aborted) await load();
    } catch (problem) {
      if (!signal.aborted) error.textContent = problem.message;
    } finally {
      reload.disabled = false;
    }
  }) : null;
  function appAction(item) {
    if (!item.meta.can_uninstall) return el("span", "muted", "Недоступен");
    return button("Открыть деинсталлятор", "secondary small", () => {
      modal("Открыть деинсталлятор", [
        el("p", "", item.title),
        message("Windows запустит команду из реестра. Дальнейшие шаги зависят от деинсталлятора программы.", "muted"),
      ], "Открыть", async () => {
        await api(`apps/${item.meta.app_id}/uninstall`, {}, {
          method: "POST", body: { confirmed: true }, activity: "Открываем штатный деинсталлятор…",
        });
        feedback.replaceChildren(message("Команда передана деинсталлятору.", "note"));
      });
    });
  }
  async function load() {
    const report = await api(`analyzers/${active}`, {
      ...(active === "app_leftovers" ? {} : { days }), limit: isApps ? 500 : 100,
    }, { signal });
    results.replaceChildren(
      el("div", "section-head", el("h2", "", isApps && days === "0" ? "Все программы" : tabs[active]),
        report.summary.collected_at ? el("span", "muted", `Собрано · ${date(report.summary.collected_at)}`) : null),
      feedback, reportTable(report, isApps ? { appControl: appAction } : {
        planControl: item => button("В план", "secondary small", () => addToPlan([{
          path: item.path, reason: item.reason ?? "", safety: item.safety, source: active,
        }], feedback)),
      }),
    );
  }
  container.replaceChildren(
    heading("Использование", "Забытое", "Файлы, папки и программы, которые стоит проверить.", reload),
    nav, controls, error, results,
  );
  await load();
}
