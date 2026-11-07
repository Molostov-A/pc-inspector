const quantity = new Intl.NumberFormat("ru-RU");
const shortNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const clock = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" });

export function el(tag, className = "", ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter(child => child !== null && child !== undefined));
  return node;
}

export const number = value => value == null ? "—" : quantity.format(value);
export const date = value => value == null ? "—" : clock.format(new Date(value * 1000));

export function size(bytes) {
  if (bytes == null) return "—";
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ"];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit++;
  }
  return `${shortNumber.format(bytes)} ${units[unit]}`;
}

export function sized(bytes) {
  const span = el("span", "amount", size(bytes));
  if (bytes != null) span.title = `${number(bytes)} байт`;
  return span;
}

export function href(page, params = {}) {
  const query = new URLSearchParams(params).toString();
  return `#${page}${query ? "?" + query : ""}`;
}

export function link(label, page, params, className = "") {
  const node = el("a", className, label);
  node.href = href(page, params);
  return node;
}

export function heading(label, title, description, action = null) {
  return el("header", "page-head",
    el("div", "", el("p", "eyebrow", label), el("h1", "", title), el("p", "muted", description)), action,
  );
}

export function button(label, className, callback) {
  const node = el("button", className, label);
  node.type = "button";
  node.addEventListener("click", callback);
  return node;
}

export function message(text, className = "empty") {
  return el("p", className, text);
}
