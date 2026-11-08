import { api } from "../api.js";
import { modal } from "../components/planDialog.js";
import { actions, states } from "./plans.js";
import { button, date, el, heading, message, sized } from "../ui.js";

export async function render(container, _params, signal) {
  let feedback = "";
  async function refresh() {
    const [history, quarantine] = await Promise.all([api("history", {}, { signal }), api("quarantine", {}, { signal })]);
    const filter = el("input", "filter");
    filter.type = "search";
    filter.placeholder = "Фильтр по пути или действию";
    filter.setAttribute("aria-label", filter.placeholder);
    const tbody = el("tbody");
    const sorted = [...history];
    function update() {
      const query = filter.value.trim().toLocaleLowerCase("ru");
      const visible = sorted.filter(item => `${item.src} ${item.dst ?? ""} ${actions[item.action]}`
        .toLocaleLowerCase("ru").includes(query));
      tbody.replaceChildren(...visible.map(item => {
        const canRestore = item.restorable && !item.restored_at && !item.purged_at;
        const recycle = ["recycle", "delete_empty_dir"].includes(item.action) && item.status === "done";
        const restore = canRestore || recycle ? button(recycle ? "Как восстановить" : "Откатить", "secondary small", () => {
          if (recycle) {
            feedback = "Откройте Корзину Windows и восстановите элемент вручную.";
            refresh().catch(error => { notice.textContent = error.message; });
            return;
          }
          modal("Восстановить объект", [el("p", "path", item.src)], "Восстановить", async () => {
            const result = await api(`history/${item.id}/restore`, {}, {
              method: "POST", activity: "Проверяем и возвращаем объект…",
            });
            feedback = result.restored ? `Восстановлено · ${result.path}` : result.message;
            await refresh();
          });
        }) : null;
        const state = item.restored_at ? "Восстановлено" : item.purged_at ? "Из карантина в Корзину" : states[item.status];
        return el("tr", "", el("td", "", date(item.at)), el("td", "", actions[item.action]),
          el("td", "path", item.src, item.dst ? el("p", "muted", item.dst) : null),
          el("td", "", sized(item.size)), el("td", "", state, item.error ? el("p", "error", item.error) : null),
          el("td", "", restore));
      }));
    }
    filter.addEventListener("input", update);
    update();
    let newestFirst = true;
    const order = button("Время ↓", "sort", () => {
      newestFirst = !newestFirst;
      sorted.reverse();
      order.textContent = newestFirst ? "Время ↓" : "Время ↑";
      update();
    });
    const purge = button("Очистить старше 30 дней…", "secondary", () => {
      modal("Очистить карантин", [
        el("p", "", "Элементы старше 30 дней будут перенесены в Корзину Windows."),
      ], "Переместить в Корзину", async () => {
        const result = await api("quarantine/purge", {}, {
          method: "POST", body: { older_than_days: 30, confirmed: true }, activity: "Проверяем старые элементы карантина…",
        });
        feedback = [`В Корзину перенесено ${result.count} элементов.`, ...result.errors.map(item => item.reason)].join(" ");
        await refresh();
      });
    });
    purge.disabled = !quarantine.items.length;
    const notice = el("p", "note", feedback);
    container.replaceChildren(
      heading("Уборка", "История", "Выполненные действия, откат и карантин."),
      el("section", "panel", el("div", "section-head", el("h2", "", "Карантин"), purge),
        el("p", "muted", "Текущий объём · ", sized(quarantine.size)), notice),
      el("section", "panel", el("div", "section-head", el("h2", "", "Журнал действий"), filter),
        history.length ? el("div", "table-scroll", el("table", "", el("thead", "",
          el("tr", "", el("th", "", order), ...["Действие", "Путь", "Размер", "Статус", "Откат"]
            .map(label => el("th", "", label)))), tbody)) : message("Действий пока нет.")),
    );
  }
  await refresh();
}
