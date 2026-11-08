import { api } from "../api.js";
import { reportTable } from "../components/reportTable.js";
import { el, heading, href, link, message, sized } from "../ui.js";

function inputFor(name, schema) {
  let input;
  if (schema.enum || schema.type === "boolean") {
    input = el("select");
    for (const value of schema.enum ?? [true, false]) {
      const option = el("option", "", typeof value === "boolean" ? (value ? "Да" : "Нет") : String(value));
      option.value = String(value);
      input.append(option);
    }
  } else {
    input = el("input");
    input.type = schema.type === "integer" ? "number" : "text";
    if (input.type === "number") input.step = "1";
  }
  input.name = name;
  input.id = `parameter-${name}`;
  input.value = schema.default ?? "";
  const label = el("label", "", schema.description || name);
  label.htmlFor = input.id;
  return el("div", "field", label, input);
}

export async function render(container, params, signal) {
  const catalog = await api("analyzers", {}, { signal });
  const active = catalog.find(item => item.id === params.get("id")) ?? catalog[0];
  const nav = el("nav", "analyzer-nav");
  nav.setAttribute("aria-label", "Список анализаторов");
  for (const item of catalog) {
    const entry = link(item.title, "/analyzers", { id: item.id });
    if (item === active) entry.setAttribute("aria-current", "page");
    nav.append(entry);
  }
  const fields = Object.entries(active.schema.properties);
  const run = el("button", "primary", "Показать отчёт");
  run.type = "submit";
  const result = el("section", "panel", message("Здесь появится отчёт после запуска анализа."));
  const form = el("form", "panel analyzer-form",
    el("h2", "", active.title), el("p", "muted", active.description),
    el("div", "form-grid", ...fields.map(([name, schema]) => inputFor(name, schema))), run,
  );
  form.addEventListener("submit", async event => {
    event.preventDefault();
    run.disabled = true;
    result.replaceChildren();
    try {
      const report = await api(`analyzers/${encodeURIComponent(active.id)}`,
        Object.fromEntries(new FormData(form)), { signal, activity: `Формируем отчёт «${active.title}»…` });
      result.replaceChildren(el("div", "section-head", el("h2", "", "Результат"), sized(report.total_size)),
        reportTable(report));
    } catch (error) {
      if (!signal.aborted) result.replaceChildren(message(error.message, "error"));
    } finally {
      run.disabled = false;
    }
  });
  const selector = el("select", "analyzer-select");
  selector.setAttribute("aria-label", "Выбор анализатора");
  for (const item of catalog) {
    const option = el("option", "", item.title);
    option.value = item.id;
    selector.append(option);
  }
  selector.value = active.id;
  selector.addEventListener("change", () => { location.hash = href("/analyzers", { id: selector.value }); });
  container.replaceChildren(
    heading("Запросы к индексу", "Анализаторы", "Выберите отчёт и задайте его параметры."),
    el("div", "analyzer-layout", el("aside", "panel", el("h2", "", "Отчёты"), nav, selector),
      el("div", "analyzer-content", form, result)),
  );
}
