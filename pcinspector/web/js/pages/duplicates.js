import { api } from "../api.js";
import { addToPlan } from "../components/planDialog.js";
import { startHash } from "../hash.js";
import { button, date, el, heading, href, message, number, sized } from "../ui.js";

function groupCard(group, index) {
  const copies = group.meta.copies;
  let keep = copies.find(copy => copy.keep_suggested)?.path;
  const feedback = el("div");
  feedback.setAttribute("role", "status");
  const rows = copies.map(copy => {
    const radio = el("input");
    radio.type = "radio";
    radio.name = `duplicate-${index}`;
    radio.checked = copy.path === keep;
    radio.addEventListener("change", () => { keep = copy.path; });
    const inspect = button("Показать файл", "secondary small", async () => {
      try {
        await api("open", {}, { method: "POST", body: { path: copy.path }, activity: "Открываем Проводник…" });
        feedback.replaceChildren(message("Запрос передан Проводнику.", "note"));
      } catch (error) {
        feedback.replaceChildren(message(error.message, "error"));
      }
    });
    return el("li", "duplicate-copy",
      el("label", "", radio, el("span", "", "Оставить")), el("span", "path", copy.path),
      copy.keep_suggested ? el("span", "badge safe", "Рекомендовано") : null,
      el("span", "muted", date(copy.mtime)), inspect,
    );
  });
  const add = button("Добавить остальные в план", "secondary", () => {
    const items = copies.filter(copy => copy.path !== keep).map(copy => ({
      path: copy.path, safety: "review", source: "duplicates", reason: `Совпадает с копией ${keep}`,
    }));
    addToPlan(items, feedback, keep);
  });
  const groupLabel = el("h2", "", group.title);
  groupLabel.id = `duplicates-group-${index}`;
  const list = el("ul", "duplicate-copies", ...rows);
  list.setAttribute("aria-labelledby", groupLabel.id);
  return el("section", "panel",
    el("div", "section-head", groupLabel, el("span", "", "Потенциально можно освободить ", sized(group.size))),
    el("p", "muted", `Копий ${number(group.meta.count)} · размер файла `, sized(group.meta.file_size)),
    list, add, feedback,
  );
}

export async function render(container, params, signal) {
  const root = el("input");
  root.placeholder = "Весь индекс";
  root.value = params.get("root") ?? "";
  const minSize = el("input");
  minSize.value = params.get("min_size") ?? "1M";
  minSize.required = true;
  const controls = el("form", "panel hash-form",
    el("label", "field", "Корень поиска", root),
    el("label", "field", "Минимальный размер", minSize),
  );
  const start = el("button", "primary", "Найти дубликаты");
  start.type = "submit";
  const feedback = el("p", "error");
  feedback.setAttribute("role", "alert");
  const results = el("div", "page");
  controls.append(start, feedback);
  controls.addEventListener("submit", async event => {
    event.preventDefault();
    start.disabled = true;
    feedback.textContent = "";
    try {
      const scope = root.value.trim();
      const threshold = minSize.value.trim();
      await startHash(scope, threshold);
      if (!signal.aborted) {
        history.replaceState(null, "", href("/duplicates", { root: scope, min_size: threshold }));
      }
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      start.disabled = false;
    }
  });
  container.replaceChildren(
    heading("Оценка места", "Дубликаты", "Выберите копию, которую хотите оставить, и добавьте остальные в план."),
    controls, results,
  );
  const report = await api("analyzers/duplicates", { root: root.value, min_size: minSize.value }, { signal });
  results.replaceChildren(
    ...report.notes.map(note => message(note, "note")),
    ...(report.findings.length ? report.findings.map(groupCard) : [message("Готовых групп для этого фильтра нет.")]),
  );
}
