/* ============================================================
   3D 思考强度滑动条（ChatGPT 式拖拽，分档变色）
   ============================================================ */
"use strict";

const ThinkingSlider = (() => {
  const LEVELS = [
    { id: "none", label: "关闭", color: "#94a3b8", glow: "rgba(148,163,184,.45)" },
    { id: "low", label: "低", color: "#60a5fa", glow: "rgba(96,165,250,.5)" },
    { id: "medium", label: "中", color: "#34d399", glow: "rgba(52,211,153,.5)" },
    { id: "high", label: "高", color: "#fbbf24", glow: "rgba(251,191,36,.55)" },
    { id: "xhigh", label: "极高", color: "#f97316", glow: "rgba(249,115,22,.55)" },
    { id: "max", label: "最大", color: "#ef4444", glow: "rgba(239,68,68,.55)" },
  ];

  const byId = (id) => LEVELS.find((l) => l.id === id) || LEVELS[2];

  function allowedLevels(maxId, catalog) {
    const order = (catalog || LEVELS).map((l) => l.id || l);
    const maxIdx = order.indexOf(maxId);
    const cap = maxIdx >= 0 ? maxIdx : order.length - 1;
    return LEVELS.filter((l) => order.indexOf(l.id) <= cap && order.includes(l.id));
  }

  function mount(container, options = {}) {
    const {
      value = "medium",
      title = "思考强度",
      compact = false,
      allowed = null,
      onChange = null,
    } = options;

    const pool = allowed && allowed.length
      ? LEVELS.filter((l) => allowed.includes(l.id))
      : LEVELS;

    if (!pool.length) pool.push(LEVELS[0]);

    let current = pool.find((l) => l.id === value)?.id || pool[pool.length - 1].id;

    const root = document.createElement("div");
    root.className = "think3d" + (compact ? " compact" : "");

    function render() {
      const meta = byId(current);
      const idx = pool.findIndex((l) => l.id === current);
      const pct = pool.length <= 1 ? 0 : (idx / (pool.length - 1)) * 100;

      root.style.setProperty("--think-color", meta.color);
      root.style.setProperty("--think-glow", meta.glow);
      root.innerHTML = `
        <div class="think3d-head">
          <svg class="ic"><use href="#i-brain"/></svg>
          <span class="think3d-title">${title}</span>
          <span class="think3d-value">${meta.label}</span>
        </div>
        <div class="think3d-rail">
          <div class="think3d-track"></div>
          <div class="think3d-fill" style="width:calc(${pct}% * (100% - 16px) / 100% + 8px)"></div>
          <div class="think3d-ticks">
            ${pool.map((l, i) => `<span class="think3d-tick ${i <= idx ? "active" : ""}"></span>`).join("")}
          </div>
          <input type="range" class="think3d-input" min="0" max="${pool.length - 1}" step="1" value="${idx}">
          <div class="think3d-thumb" style="left:calc(8px + (100% - 16px) * ${pct} / 100)"></div>
        </div>
        <div class="think3d-legend">
          ${pool.map((l) => `<span class="${l.id === current ? "active" : ""}">${l.label}</span>`).join("")}
        </div>`;

      const input = root.querySelector(".think3d-input");
      const thumb = root.querySelector(".think3d-thumb");
      const fill = root.querySelector(".think3d-fill");
      const valueEl = root.querySelector(".think3d-value");

      function syncVisual(i) {
        const m = pool[i];
        const p = pool.length <= 1 ? 0 : (i / (pool.length - 1)) * 100;
        root.style.setProperty("--think-color", m.color);
        root.style.setProperty("--think-glow", m.glow);
        if (thumb) thumb.style.left = `calc(8px + (100% - 16px) * ${p} / 100)`;
        if (fill) fill.style.width = `calc(${p}% * (100% - 16px) / 100% + 8px)`;
        if (valueEl) valueEl.textContent = m.label;
        root.querySelectorAll(".think3d-tick").forEach((t, ti) => t.classList.toggle("active", ti <= i));
        root.querySelectorAll(".think3d-legend span").forEach((s, si) => s.classList.toggle("active", si === i));
      }

      input.addEventListener("input", () => {
        const i = parseInt(input.value, 10);
        current = pool[i].id;
        syncVisual(i);
        if (onChange) onChange(current, pool[i]);
      });
    }

    render();
    container.appendChild(root);

    return {
      getValue: () => current,
      setValue: (id) => {
        if (pool.find((l) => l.id === id)) {
          current = id;
          render();
        }
      },
      destroy: () => root.remove(),
      element: root,
    };
  }

  function meta(id) { return byId(id); }

  function levelsUpTo(maxId) {
    const idx = LEVELS.findIndex((l) => l.id === maxId);
    return LEVELS.slice(0, idx >= 0 ? idx + 1 : LEVELS.length).map((l) => l.id);
  }

  return { mount, meta, LEVELS, levelsUpTo, allowedLevels };
})();
