import { el, message } from "./ui.js";
import { beginActivity } from "./activity.js";
import { render as home } from "./pages/home.js";
import { render as explorer } from "./pages/explorer.js";
import { render as junk } from "./pages/junk.js";
import { render as analyzers } from "./pages/analyzers.js";

const routes = { "/": home, "/explorer": explorer, "/junk": junk, "/analyzers": analyzers };
const activities = {
  "/": "Читаем индекс и сводку по дискам…",
  "/explorer": "Читаем содержимое папки и строим карту…",
  "/junk": "Собираем группы мусора из индекса…",
  "/analyzers": "Загружаем список анализаторов и параметры…",
};
const page = document.querySelector("#page");
const theme = document.querySelector("#theme");
let controller;
let dispose;

const applyTheme = () => {
  document.documentElement.dataset.theme = theme.value;
  localStorage.setItem("pci-theme", theme.value);
};
theme.value = localStorage.getItem("pci-theme") || "auto";
if (!theme.value) theme.value = "auto";
applyTheme();
theme.addEventListener("change", applyTheme);

async function navigate() {
  controller?.abort();
  dispose?.();
  dispose = null;
  controller = new AbortController();
  const { signal } = controller;
  const [path, query] = location.hash.slice(1).split("?", 2);
  const route = Object.hasOwn(routes, path) ? path : "/";
  for (const item of document.querySelectorAll("[data-page]")) {
    if (item.dataset.page === route) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const container = el("div", "page");
  page.replaceChildren(container);
  const finish = beginActivity(activities[route]);
  try {
    const cleanup = await routes[route](container, new URLSearchParams(query), signal);
    if (signal.aborted) cleanup?.();
    else dispose = cleanup;
  } catch (error) {
    if (!signal.aborted) container.replaceChildren(message(error.message, "error panel"));
  } finally {
    finish();
  }
}

window.addEventListener("hashchange", navigate);
window.addEventListener("pci-scan-finished", navigate);
navigate();
