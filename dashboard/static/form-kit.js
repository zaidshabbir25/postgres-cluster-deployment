/* Controls shared by the deploy and add-node forms.
 *
 * Plain functions over existing markup — no framework, no build step, and no
 * state of their own: each takes an element and a callback, and the page
 * decides what a choice means. Loaded before the page's own script.
 */

/** Segmented control: a group of buttons where exactly one is chosen. */
function wireSegmented(group, onPick) {
  if (!group) return;

  const mark = (chosen) => {
    group.querySelectorAll("button").forEach((button) => {
      const on = button === chosen;
      button.classList.toggle("on", on);
      button.setAttribute("aria-checked", on ? "true" : "false");
    });
  };

  group.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-value]");
    if (!button) return;
    mark(button);
    onPick(button.dataset.value);
  });

  // Arrow keys move between options, as a radio group should.
  group.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    const buttons = [...group.querySelectorAll("button")];
    const current = buttons.findIndex((b) => b.classList.contains("on"));
    const next = event.key === "ArrowLeft" || event.key === "ArrowUp"
      ? (current - 1 + buttons.length) % buttons.length
      : (current + 1) % buttons.length;
    buttons[next].click();
    buttons[next].focus();
    event.preventDefault();
  });

  group.select = (value) => {
    const button = group.querySelector(`button[data-value="${value}"]`);
    if (button) mark(button);
  };
  return group;
}

/** Filtering combobox over a list the page supplies, typed or picked. */
function wireCombo(input, itemsFor, onChange) {
  if (!input) return;
  const list = document.getElementById(`${input.id}-list`);
  const toggle = input.parentElement.querySelector(".combo-toggle");
  let active = -1;

  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  };

  const open = () => {
    const items = itemsFor(input.value.trim()) || [];
    list.innerHTML = "";
    if (!items.length) { close(); return; }
    items.forEach((item) => {
      const option = document.createElement("li");
      if (item.group) {
        option.className = "group";
        option.textContent = item.group;
      } else {
        option.setAttribute("role", "option");
        option.dataset.value = item.value;
        option.innerHTML = `<span>${item.value}</span>${
          item.note ? `<small>${item.note}</small>` : ""}`;
        option.addEventListener("mousedown", (event) => {
          event.preventDefault();
          input.value = item.value;
          input.dataset.touched = "1";
          close();
          onChange();
        });
      }
      list.appendChild(option);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };

  input.addEventListener("focus", open);
  input.addEventListener("input", () => { input.dataset.touched = "1"; open(); onChange(); });
  input.addEventListener("blur", () => setTimeout(close, 120));
  if (toggle) {
    toggle.addEventListener("click", () => {
      if (list.hidden) { input.focus(); open(); } else close();
    });
  }
  input.addEventListener("keydown", (event) => {
    const options = [...list.querySelectorAll("li[role='option']")];
    if (event.key === "Escape") { close(); return; }
    if (!options.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      active = event.key === "ArrowDown"
        ? Math.min(active + 1, options.length - 1)
        : Math.max(active - 1, 0);
      options.forEach((option, index) => option.classList.toggle("active", index === active));
      options[active].scrollIntoView({ block: "nearest" });
      event.preventDefault();
    } else if (event.key === "Enter" && active >= 0) {
      input.value = options[active].dataset.value;
      input.dataset.touched = "1";
      close();
      onChange();
      event.preventDefault();
    }
  });
}

/** Number steppers: the − and + buttons around a number input. */
function wireSteppers(root, onChange) {
  (root || document).querySelectorAll(".stepper button").forEach((button) => {
    button.addEventListener("click", () => {
      const input = button.parentElement.querySelector("input");
      const next = Number(input.value || 1) + Number(button.dataset.step);
      input.value = Math.min(Number(input.max || 99),
                             Math.max(Number(input.min || 0), next));
      onChange();
    });
  });
}

/** Debounce, so a preview follows typing without chasing every keystroke. */
function debounce(fn, delay = 180) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
