const menuBtn = document.getElementById("menu-btn");
const aside = document.querySelector("aside");
if (menuBtn && aside) {
  menuBtn.addEventListener("click", () => {
    const open = aside.classList.toggle("open");
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    menuBtn.textContent = open ? "Close" : "Menu";
  });
  aside.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      if (window.matchMedia("(max-width: 800px)").matches) {
        aside.classList.remove("open");
        menuBtn.setAttribute("aria-expanded", "false");
        menuBtn.textContent = "Menu";
      }
    });
  });
}

document.querySelectorAll("nav details").forEach((item) => {
  item.addEventListener("toggle", () => {
    if (!item.open) return;
    document.querySelectorAll("nav details").forEach((other) => {
      if (other !== item) other.open = false;
    });
  });
});

document.querySelectorAll("[data-phone]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.querySelector("input[name=phone]");
    if (input) input.value = button.dataset.phone;
  });
});

const form = document.getElementById("fuel-form");
const note = document.getElementById("offline-note");
const KEY = "tms-fuel-queue";

function pending() {
  return JSON.parse(localStorage.getItem(KEY) || "[]");
}

function renderQueue() {
  if (!note) return;
  const items = pending();
  note.textContent = items.length
    ? `${items.length} fuel entry waiting to sync. It will send when the connection returns.`
    : "If the network drops, this form keeps the entry on the phone and syncs later.";
}

async function flush() {
  const items = pending();
  if (!items.length || !navigator.onLine) return;
  const left = [];
  for (const item of items) {
    try {
      const body = new URLSearchParams(item);
      const res = await fetch("/fuel", { method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" } });
      if (!res.ok && res.status !== 302) left.push(item);
    } catch (_err) {
      left.push(item);
    }
  }
  localStorage.setItem(KEY, JSON.stringify(left));
  renderQueue();
}

if (form) {
  form.addEventListener("submit", (event) => {
    if (navigator.onLine) return;
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    const items = pending();
    items.push(data);
    localStorage.setItem(KEY, JSON.stringify(items));
    form.reset();
    renderQueue();
  });
  renderQueue();
  window.addEventListener("online", flush);
  flush();
}
