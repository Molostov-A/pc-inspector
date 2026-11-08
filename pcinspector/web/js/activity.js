const line = document.querySelector("#activity");
const tasks = new Map();
let scan = "";
let hash = "";

function update() {
  const text = [...tasks.values()].at(-1) || hash || scan || "Готово";
  if (line.textContent !== text) line.textContent = text;
  line.title = text;
}

export function beginActivity(text) {
  const task = Symbol();
  tasks.set(task, text);
  update();
  return () => {
    tasks.delete(task);
    update();
  };
}

export function scanActivity(text) {
  scan = text;
  update();
}

export function hashActivity(text) {
  hash = text;
  update();
}
