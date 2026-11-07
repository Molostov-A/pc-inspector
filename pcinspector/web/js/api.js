const token = document.querySelector('meta[name="pci-token"]').content;

export async function api(path, params = {}, options = {}) {
  const url = new URL(`/api/${path}`, location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== undefined) url.searchParams.set(key, value);
  }
  const headers = new Headers();
  const method = options.method ?? "GET";
  if (method !== "GET") headers.set("X-PCI-Token", token);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(url, {
    method, headers, cache: "no-store", signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? `Ошибка HTTP ${response.status}`);
  return data;
}
