import { api } from "../api.js";
import { modal } from "../components/planDialog.js";
import { safetyBadge } from "../components/safetyBadge.js";
import { button, date, el, heading, href, link, message, number, sized } from "../ui.js";

export const actions = {
  recycle: "В Корзину", quarantine: "В карантин", move: "Переместить", delete_empty_dir: "Пустую папку в Корзину",
  clear_contents: "Очистить содержимое",
};
export const states = {
  draft: "Черновик", executing: "Выполняется", executed: "Выполнен", partially: "Выполнен частично",
  cancelled: "Отменён", pending: "Ожидает", running: "Начат", done: "Выполнено",
  skipped: "Пропущено", failed: "Ошибка",
};

function author(plan) {
  return el("span", `badge ${plan.author === "ai" ? "review" : "info"}`, plan.author === "ai" ? "ИИ" : "Пользователь");
}

export async function render(container, params, signal) {
  const id = params.get("id");
  if (!id) {
    const records = await api("plans", {}, { signal });
    const create = button("Создать план", "primary", () => {
      const title = el("input");
      title.required = true;
      title.maxLength = 160;
      const note = el("textarea");
      note.maxLength = 2000;
      modal("Новый план", [el("label", "field", "Название", title), el("label", "field", "Заметка", note)],
        "Создать", async () => {
          const plan = await api("plans", {}, { method: "POST", body: { title: title.value, note: note.value } });
          location.hash = href(`/plans/${plan.id}`);
        });
    });
    container.replaceChildren(
      heading("Уборка", "Планы", "Проверьте пункты перед выполнением.", create),
      el("section", "panel", records.length ? el("ul", "ranking", ...records.map(plan =>
        el("li", "", link(plan.title, `/plans/${plan.id}`, {}, "text-link"),
          author(plan), el("span", "muted", date(plan.created_at)), el("span", "", states[plan.status]),
          el("span", "muted", `${number(plan.item_count)} пунктов`)),
      )) : message("Пока нет планов. Создайте черновик или добавьте находки из отчёта.")),
    );
    return;
  }

  const endpoint = `plans/${id}`;
  const report = el("section", "panel");
  report.hidden = true;
  function show(plan) {
    const draft = plan.status === "draft";
    const feedback = el("p", "error");
    function save(item, body, control, method = "PATCH") {
      control.disabled = true;
      start.disabled = true;
      feedback.textContent = "";
      api(`${endpoint}/items/${item.id}`, {}, { signal, method, body, activity: "Сохраняем изменения плана…" })
        .then(show).catch(error => {
          feedback.textContent = error.message;
          control.disabled = false;
          start.disabled = !plan.selected_count;
          if (control.type === "checkbox") control.checked = Boolean(item.selected);
        });
    }
    const rows = plan.items.map(item => {
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(item.selected);
      checkbox.disabled = !draft;
      checkbox.setAttribute("aria-label", `Выполнить ${item.path}`);
      checkbox.addEventListener("change", () => save(item, { selected: checkbox.checked }, checkbox));
      const target = el("input");
      target.placeholder = "Полный путь назначения";
      target.setAttribute("aria-label", "Путь назначения");
      target.value = item.target ?? "";
      const action = el("select");
      action.setAttribute("aria-label", "Действие");
      for (const [key, label] of Object.entries(actions)) {
        const option = el("option", "", label);
        option.value = key;
        action.append(option);
      }
      action.value = item.action;
      target.hidden = action.value !== "move";
      action.addEventListener("change", () => { target.hidden = action.value !== "move"; });
      const apply = button("Сохранить", "secondary small", () =>
        save(item, { action: action.value, target: action.value === "move" ? target.value : null }, apply));
      const remove = button("Убрать из плана", "secondary small", () => save(item, undefined, remove, "DELETE"));
      const row = el("tr", "", el("td", "", checkbox),
        el("td", "path", item.path, item.reason ? el("p", "muted", item.reason) : null),
        el("td", "", sized(item.size)), el("td", "", safetyBadge(item.safety)),
        el("td", "", draft ? el("div", "field", action, target, el("div", "row-actions", apply, remove))
          : el("div", "", actions[item.action], item.target ? el("p", "path muted", item.target) : null)),
        el("td", "", states[item.status], item.error ? el("p", "error", item.error) : null));
      return { item, row };
    });
    const start = button("Выполнить…", "primary", () => {
      modal("Подтвердить выполнение", [
        el("p", "", "Выбрано ", number(plan.selected_count), " пунктов · ", sized(plan.selected_size)),
        el("p", "muted", "Перед каждым действием приложение сверит путь и состояние файлов. Корзина и карантин оставляют данные на диске."),
      ], "Подтверждаю выполнение", async () => {
        const approval = await api(`${endpoint}/confirm-token`, { revision: plan.revision }, { method: "POST", signal });
        const outcome = await api(`${endpoint}/execute`, {}, {
          method: "POST", body: approval, activity: "Проверяем и выполняем пункты плана…",
        });
        report.replaceChildren(
          el("h2", "", "Итог выполнения"),
          el("p", "", `Выполнено ${outcome.done} · Пропущено ${outcome.skipped} · Ошибок ${outcome.failed}`),
          el("p", "", "Обработано ", sized(outcome.processed_size), " · Освобождено ", sized(outcome.freed_size)),
          el("p", "muted", "Место освободится после очистки карантина и Корзины."),
          outcome.index_updated === false ? message("Индекс занят сканом. Данные обновятся после пересканирования.", "note") : null,
          link("Открыть историю →", "/history", {}, "text-link"),
        );
        report.hidden = false;
        if (!signal.aborted) show(await api(endpoint, {}, { signal }));
      });
    });
    start.disabled = !plan.selected_count;
    const cancel = button("Отменить черновик", "secondary", () => {
      modal("Отменить черновик", [message(plan.title, "muted")], "Отменить план", async () => {
        show(await api(endpoint, {}, { signal, method: "PATCH", body: { cancel: true } }));
      });
    });
    const edit = button("Изменить название и заметку", "secondary", () => {
      const title = el("input");
      title.required = true;
      title.maxLength = 160;
      title.value = plan.title;
      const note = el("textarea");
      note.maxLength = 2000;
      note.value = plan.note;
      modal("Изменить план", [el("label", "field", "Название", title), el("label", "field", "Заметка", note)],
        "Сохранить", async () => {
          show(await api(endpoint, {}, { signal, method: "PATCH", body: { title: title.value, note: note.value } }));
        });
    });
    const filter = el("input", "filter");
    filter.type = "search";
    filter.placeholder = "Фильтр по пути или причине";
    filter.setAttribute("aria-label", filter.placeholder);
    const body = el("tbody");
    let order = 0;
    const sizeSort = button("Размер", "sort", () => {
      order = order === -1 ? 1 : -1;
      sizeSort.textContent = order === -1 ? "Размер ↓" : "Размер ↑";
      updateRows();
    });
    function updateRows() {
      const query = filter.value.trim().toLocaleLowerCase("ru");
      const visible = rows.filter(({ item }) => `${item.path} ${item.reason}`.toLocaleLowerCase("ru").includes(query));
      if (order) visible.sort((a, b) => order * (a.item.size - b.item.size));
      body.replaceChildren(...visible.map(({ row }) => row));
    }
    filter.addEventListener("input", updateRows);
    updateRows();
    container.replaceChildren(
      link("← Все планы", "/plans", {}, "text-link"),
      heading("План уборки", plan.title, el("span", "", author(plan), " · ", states[plan.status]),
        draft ? el("div", "row-actions", edit, cancel, start) : null),
      plan.note ? message(plan.note, "note") : null,
      plan.status === "executing" ? message("Если приложение было закрыто во время выполнения, проверьте пути в Истории. Начатые пункты автоматически не повторяются.") : null,
      report,
      el("section", "panel", el("div", "section-head", el("h2", "", "Пункты плана"),
        el("span", "muted", `Выбрано ${plan.selected_count} · `, sized(plan.selected_size))), feedback,
        rows.length ? el("div", "table-tools", filter) : null,
        rows.length ? el("div", "table-scroll", el("table", "",
          el("thead", "", el("tr", "", ...["Выбор", "Путь и причина", sizeSort, "Оценка", "Действие", "Статус"]
            .map(label => el("th", "", label)))), body,
        )) : message("Добавьте пункты из «Обзора», «Мусора» или «Анализаторов».")),
    );
  }
  show(await api(endpoint, {}, { signal }));
}
