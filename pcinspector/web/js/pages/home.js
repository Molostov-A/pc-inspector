import { api } from "../api.js";
import { scanDialog } from "../scan.js";
import { button, date, el, heading, link, message, number, sized } from "../ui.js";
import { sizeBar } from "../components/sizeBar.js";

function diskCard(drive) {
  const known = drive.total != null && drive.free != null;
  const title = el("strong", "path", drive.path);
  const action = drive.scanned
    ? link("Открыть обзор →", "/explorer", { path: drive.path }, "text-link")
    : button("Сканировать", "secondary small", () => scanDialog([drive.path]));
  return el("article", "panel disk-card",
    el("div", "section-head", el("span", "disk-icon", "▣"), el("span", "eyebrow", drive.filesystem)),
    el("div", "disk-name", title, el("span", "muted", drive.label || "Локальный том")),
    known ? sizeBar(drive.total - drive.free, drive.total, "Занято на томе") : null,
    known ? el("p", "muted", "Свободно ", sized(drive.free), " из ", sized(drive.total)) : message("Ёмкость недоступна", "muted"),
    action,
  );
}

function ranking(title, action, items, itemLink, empty) {
  const panel = el("section", "panel",
    el("div", "section-head", el("h2", "", title), action));
  if (!items.length) panel.append(message(empty));
  else panel.append(el("ol", "ranking", ...items.map(item =>
    el("li", "", itemLink(item), sized(item.size)),
  )));
  return panel;
}

function folderLink(item) {
  const entry = link(item.title, "/explorer", { path: item.path }, "text-link path");
  entry.title = item.path;
  return entry;
}

export async function render(container, _params, signal) {
  const options = { signal };
  const [roots, drives, tree, junk, preferences] = await Promise.all([
    api("roots", {}, options),
    api("drives", {}, options),
    api("folders/top", {}, options),
    api("analyzers/junk", { limit: 5 }, options),
    api("settings", {}, options),
  ]);
  const indexed = roots.findings.filter(item => item.kind === "stat");
  const scanRoots = preferences.scan_roots.length ? preferences.scan_roots : indexed.map(item => item.path);
  const latest = Math.max(0, ...indexed.map(item => item.meta.scanned_at ?? 0));
  const cards = el("section", "block", el("h2", "", "Локальные диски"));
  cards.append(drives.length ? el("div", "disks", ...drives.map(diskCard)) : message("Доступные локальные тома не найдены."));
  const recent = el("section", "panel",
    el("div", "section-head", el("h2", "", "Просканированные корни"),
      el("span", "muted", latest ? `Последний скан · ${date(latest)}` : "Первый скан ещё впереди")),
  );
  if (indexed.length) {
    recent.append(el("ul", "ranking", ...indexed.map(item => el("li", "",
      link(item.path, "/explorer", { path: item.path }, "text-link path"),
      el("span", "muted", `${number(item.meta.file_count)} файлов · ${date(item.meta.scanned_at)}`), sized(item.size),
    ))));
  } else {
    recent.append(message("Запустите сканирование папки или диска, чтобы увидеть распределение места."));
  }
  const system = roots.findings.filter(item => item.kind === "group");
  if (system.length) recent.append(el("p", "note",
    "Системные файлы на просканированных томах · ", sized(system.reduce((sum, item) => sum + item.size, 0)),
  ));
  container.replaceChildren(
    heading("Индекс дисков", "Дисковое пространство", "Что занимает место и какие данные стоит проверить.",
      el("div", "row-actions",
        link("Найти дубликаты", "/duplicates", {}, "text-link"),
        button(indexed.length ? "Пересканировать" : "Сканировать", "primary", () => scanDialog(scanRoots))),
    ),
    cards,
    el("div", "metrics",
      el("section", "panel metric", el("p", "eyebrow", "Можно освободить"), sized(junk.summary.safe_size), el("p", "muted", "Кэши и временные данные")),
      el("section", "panel metric review", el("p", "eyebrow", "Нужна проверка"), sized(junk.summary.review_size), el("p", "muted", "Файлы и папки для ручной оценки")),
      el("section", "panel metric neutral", el("p", "eyebrow", "Рост с прошлого скана"), el("strong", "", "—"),
        link("Открыть историю роста", "/organize", { tab: "growth" }, "text-link")),
    ),
    el("div", "split",
      ranking("Крупные папки", link("В обзор →", "/explorer", {}, "text-link"), tree.findings,
        folderLink, "Данные появятся после сканирования."),
      ranking("Группы мусора", link("Все группы →", "/junk", {}, "text-link"), junk.findings,
        item => el("span", "", item.title), "Подходящие группы пока не найдены."),
    ),
    recent,
  );
}
