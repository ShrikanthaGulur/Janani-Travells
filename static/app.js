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

const backBtn = document.getElementById("back-btn");
if (backBtn) {
  backBtn.addEventListener("click", () => {
    window.location.href = backBtn.dataset.home;
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

document.querySelectorAll(".pick-date").forEach((input) => {
  const wrap = document.createElement("span");
  wrap.className = "date-field";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  const panel = document.createElement("div");
  panel.className = "date-panel";
  panel.hidden = true;
  wrap.appendChild(panel);
  let view = input.value ? new Date(`${input.value}T00:00:00`) : new Date();

  function render() {
    const year = view.getFullYear();
    const month = view.getMonth();
    const first = new Date(year, month, 1).getDay();
    const count = new Date(year, month + 1, 0).getDate();
    const label = view.toLocaleString("en", { month: "short", year: "numeric" });
    let html = `<div class="date-nav"><button type="button" data-move="-1">‹</button><span>${label}</span><button type="button" data-move="1">›</button></div><div class="date-grid">`;
    ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].forEach((name) => {
      html += `<span class="date-dow">${name}</span>`;
    });
    for (let blank = 0; blank < first; blank += 1) html += "<span></span>";
    for (let day = 1; day <= count; day += 1) {
      const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      html += `<button type="button" data-day="${iso}" class="${input.value === iso ? "on" : ""}">${day}</button>`;
    }
    panel.innerHTML = `${html}</div>`;
  }

  function openPanel() {
    render();
    panel.hidden = false;
    const roomBelow = window.innerHeight - input.getBoundingClientRect().bottom;
    panel.classList.toggle("above", roomBelow < 280);
  }

  input.addEventListener("focus", openPanel);
  input.addEventListener("click", openPanel);
  panel.addEventListener("click", (event) => {
    const move = event.target.dataset.move;
    if (move) {
      view.setMonth(view.getMonth() + Number(move));
      render();
      return;
    }
    if (event.target.dataset.day) {
      input.value = event.target.dataset.day;
      panel.hidden = true;
    }
  });
  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target)) panel.hidden = true;
  });
});

function showSalaryPopup(message) {
  document.querySelectorAll(".toast-backdrop").forEach((node) => node.remove());
  const backdrop = document.createElement("div");
  backdrop.className = "toast-backdrop";
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.textContent = "Close";
  close.addEventListener("click", () => backdrop.remove());
  toast.appendChild(close);
  backdrop.appendChild(toast);
  document.body.appendChild(backdrop);
}

document.querySelectorAll(".toast-backdrop").forEach((backdrop) => {
  const toast = backdrop.querySelector(".toast");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.textContent = "Close";
  close.addEventListener("click", () => backdrop.remove());
  toast.appendChild(close);
});

document.querySelectorAll("form[data-salary]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const data = new FormData(form);
    const amount = Number(data.get("amount"));
    const linkedNote = form.id ? document.querySelector(`[name="note"][form="${form.id}"]`) : null;
    const note = ((data.get("note") || (linkedNote && linkedNote.value) || "") + "").trim();
    const salary = Number(form.dataset.salary);
    const taken = Number(form.dataset.taken || 0);
    const oldAmount = Number(form.dataset.old || 0);
    const total = taken - oldAmount + amount;
    if (amount > salary || total > salary) {
      showSalaryPopup("This advance is more than the salary. For an emergency, write it in the note, then save the advance.");
      if (!note) event.preventDefault();
    }
  });
});

document.querySelectorAll("input[name=phone][maxlength='10']").forEach((input) => {
  input.addEventListener("input", () => {
    const digits = input.value.replace(/\D/g, "").slice(0, 10);
    if (digits !== input.value) input.value = digits;
  });
});

document.querySelectorAll("[data-phone]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.querySelector("input[name=phone]");
    if (input) input.value = button.dataset.phone;
  });
});

const tripKind = document.getElementById("trip-kind");
const companyPick = document.getElementById("company-pick");
if (tripKind && companyPick) {
  const companySelect = companyPick.querySelector("select");
  const syncTrip = () => {
    const company = tripKind.value === "company";
    companyPick.hidden = !company;
    companySelect.required = company;
    if (!company) companySelect.value = "";
  };
  tripKind.addEventListener("change", syncTrip);
  syncTrip();
}

const oncallForm = document.getElementById("oncall-form");
if (oncallForm) {
  const openKm = oncallForm.querySelector("[name=open_km]");
  const closeKm = oncallForm.querySelector("[name=close_km]");
  const totalKm = oncallForm.querySelector("[name=total_km]");
  const totalAmount = oncallForm.querySelector("[name=total_amount]");
  const chargeNames = ["extra_hour", "extra_km", "check_post", "toll_fees", "parking", "waiting", "bata"];
  const refreshOncall = () => {
    const start = Number(openKm.value);
    const end = Number(closeKm.value);
    totalKm.value = openKm.value !== "" && closeKm.value !== "" ? Math.max(0, end - start).toFixed(1) : "";
    const total = chargeNames.reduce((sum, name) => sum + (Number(oncallForm.querySelector(`[name=${name}]`).value) || 0), 0);
    totalAmount.value = total.toFixed(2);
  };
  oncallForm.querySelectorAll("input").forEach((input) => input.addEventListener("input", refreshOncall));
  refreshOncall();
}

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
