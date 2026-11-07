import { api } from "../api.js";
import { reportTable } from "../components/reportTable.js";
import { safetyBadge, safetyLabels } from "../components/safetyBadge.js";
import { el, heading, message, number, sized } from "../ui.js";

export async function render(container, _params, signal) {
  const report = await api("analyzers/junk", { limit: 100 }, { signal });
  const filter = el("input", "filter");
  filter.type = "search";
  filter.placeholder = "Найти группу или путь";
  filter.setAttribute("aria-label", filter.placeholder);
  const groups = el("div", "junk-groups");
  const items = report.findings.map(item => {
    const findings = item.meta.top ?? [];
    const detail = el("details", "junk-group",
      el("summary", "",
        el("span", "group-name", item.title),
        el("span", "muted", `${number(item.meta.count)} шт.`), safetyBadge(item.safety), sized(item.size),
      ),
      el("div", "group-body",
        el("p", "muted", item.reason),
        item.meta.count > findings.length ? message("Показаны 10 самых крупных путей.", "note") : null,
        reportTable({ findings: findings.map(entry => ({
          ...entry, title: entry.path, safety: item.safety, reason: item.reason,
        })) }),
      ),
    );
    const search = [item.title, item.reason, safetyLabels[item.safety], ...findings.map(entry => entry.path)]
      .join(" ").toLocaleLowerCase("ru");
    return { detail, search };
  });
  const empty = message("Подходящие группы не найдены.");
  function update() {
    const value = filter.value.trim().toLocaleLowerCase("ru");
    items.forEach(({ detail, search }) => { detail.hidden = !search.includes(value); });
    empty.hidden = items.some(({ detail }) => !detail.hidden);
  }
  filter.addEventListener("input", update);
  groups.append(...items.map(({ detail }) => detail), empty);
  update();
  container.replaceChildren(
    heading("Оценка места", "Мусор", "Кэши, временные данные и другие находки, которые стоит проверить."),
    el("div", "metrics",
      el("section", "panel metric", el("p", "eyebrow", "Можно освободить"), sized(report.summary.safe_size)),
      el("section", "panel metric review", el("p", "eyebrow", "Нужна проверка"), sized(report.summary.review_size)),
      el("section", "panel metric neutral", el("p", "eyebrow", "Найдено путей"), el("strong", "", number(report.summary.items))),
    ),
    el("section", "panel", el("div", "section-head", el("h2", "", "Группы находок"), filter), groups),
  );
}
