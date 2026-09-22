/* ============================================================
   模型设置模块 — 供应商 CRUD / Agent 激活 / RAG 嵌入衔接
   ============================================================ */
"use strict";

const ModelSettings = (() => {
  const catalog = {
    providers: [],
    active: { provider_id: null, model_id: null },
    rag: { use_active_provider: true, provider_id: null, model_id: null },
    integration: null,
    presets: [],
    api_formats: [],
  };

  let selectedProviderId = null;
  let deps = { api: null, toast: null, escapeHtml: null, onActiveChange: null };

  function init(options) {
    deps = { ...deps, ...options };
  }

  async function loadCatalog() {
    const data = await deps.api("/api/models");
    catalog.providers = data.providers || [];
    catalog.active = data.active || catalog.active;
    catalog.rag = data.rag || catalog.rag;
    catalog.integration = data.integration || null;
    catalog.presets = data.presets || [];
    catalog.api_formats = data.api_formats || [];
    if (!selectedProviderId) {
      selectedProviderId = catalog.active.provider_id
        || (catalog.providers[0] && catalog.providers[0].id);
    }
    return data;
  }

  function providerById(id) {
    return catalog.providers.find((p) => p.id === id);
  }

  function modelById(provider, modelId) {
    return (provider?.models || []).find((m) => m.id === modelId);
  }

  function vendorBadge(vendor) {
    const map = {
      openai: "AI", deepseek: "DS", qwen: "千", glm: "智",
      gemini: "G", anthropic: "C", moonshot: "K", custom: "∗",
    };
    return map[vendor] || vendor.slice(0, 2).toUpperCase();
  }

  function renderPage() {
    const integ = catalog.integration || {};
    const agent = integ.agent || {};
    const rag = integ.rag || {};

    return `
    <div class="ms-page">
      <div class="set-page-head">
        <div>
          <h1 class="set-page-title">模型配置</h1>
          <p class="set-page-desc">管理 OpenAI 兼容供应商与模型参数，激活后立即接入 Agent 循环与 RAG 检索。</p>
        </div>
        <div class="set-head-actions">
          <button class="ghost-square" id="ms-btn-refresh" title="刷新">
            <svg class="ic"><use href="#i-refresh"/></svg>
          </button>
          <button class="dark-btn" id="ms-btn-add-provider">
            <svg class="ic sm"><use href="#i-plus"/></svg>添加供应商
          </button>
        </div>
      </div>

      <div class="ms-integration">
        <div class="ms-int-card">
          <h3>Agent 循环</h3>
          <div class="ms-int-value">
            <span class="ms-status-dot ${agent.ready ? "ok" : "warn"}"></span>
            ${agent.ready ? `${deps.escapeHtml(agent.provider)} / ${deps.escapeHtml(agent.model)}` : "未配置可用模型"}
          </div>
          <div class="ms-int-meta">
            ${agent.ready
              ? `上下文 ${formatCtx(agent.context_window)} · 思考 ${agent.reasoning_label || "中"}`
              : "请选择已启用且 API 格式为 Chat Completions 的对话模型并激活"}
          </div>
          ${agent.ready ? `<div class="ms-int-thinking" id="ms-global-thinking"></div>` : ""}
        </div>
        <div class="ms-int-card">
          <h3>RAG 嵌入</h3>
          <div class="ms-int-value">
            <span class="ms-status-dot ${rag.ready ? "ok" : "off"}"></span>
            ${rag.ready
              ? `${deps.escapeHtml(rag.provider)} / ${deps.escapeHtml(rag.model)}`
              : "跟随 Agent 或单独配置嵌入模型"}
          </div>
          <div class="ms-int-meta">
            ${rag.use_active_provider ? "当前跟随 Agent 主模型端点" : "独立嵌入端点"}
            ${rag.endpoint ? `<br>${deps.escapeHtml(rag.endpoint)}` : ""}
          </div>
        </div>
      </div>

      <div class="ms-layout">
        <aside class="ms-sidebar">
          <div class="ms-sidebar-head">供应商</div>
          <div class="ms-provider-list" id="ms-provider-list">
            ${catalog.providers.map((p) => provItemHtml(p)).join("")}
          </div>
        </aside>
        <div class="ms-main" id="ms-main-panel">
          ${renderProviderPanel()}
        </div>
      </div>
    </div>`;
  }

  function provItemHtml(p) {
    const active = catalog.active.provider_id === p.id;
    return `
      <button class="ms-prov-item ${p.id === selectedProviderId ? "active" : ""}" data-provider="${p.id}">
        <span class="ms-prov-badge">${vendorBadge(p.vendor)}</span>
        <span class="ms-prov-meta">
          <div class="ms-prov-name">${deps.escapeHtml(p.name)}</div>
          <div class="ms-prov-sub">${p.models.length} 个模型${active ? " · 当前 Agent" : ""}</div>
        </span>
        <span class="ms-prov-dot ${p.enabled ? "on" : "off"}"></span>
      </button>`;
  }

  function renderProviderPanel() {
    const p = providerById(selectedProviderId);
    if (!p) {
      return `<p class="set-page-desc">暂无供应商，点击右上角添加。</p>`;
    }
    const isActive = catalog.active.provider_id === p.id;
    const activeModel = modelById(p, catalog.active.model_id);

    return `
      <div class="ms-main-head">
        <div>
          <h2 class="ms-main-title">${deps.escapeHtml(p.name)}</h2>
          <div class="ms-main-sub">${deps.escapeHtml(p.api_format_label || p.api_format)}</div>
          <div class="ms-chip-row">
            ${p.agent_compatible
              ? '<span class="ms-chip accent">Agent 兼容</span>'
              : '<span class="ms-chip">需 OpenAI 兼容端点</span>'}
            ${p.api_key_set ? '<span class="ms-chip">Key 已配置</span>' : '<span class="ms-chip">未配置 Key</span>'}
            ${isActive && activeModel ? `<span class="ms-chip accent">运行中: ${deps.escapeHtml(activeModel.name)}</span>` : ""}
          </div>
        </div>
        <div class="ms-inline">
          <label class="toggle" title="启用供应商">
            <input type="checkbox" id="ms-prov-enabled" ${p.enabled ? "checked" : ""}>
            <span class="track"></span>
          </label>
          <button class="gray-btn" id="ms-btn-test-prov">测试连接</button>
          <button class="gray-btn danger-text" id="ms-btn-del-prov">删除</button>
        </div>
      </div>

      <div class="ms-form-grid">
        <div class="ms-field">
          <label>Base URL</label>
          <input id="ms-base-url" value="${deps.escapeHtml(p.base_url)}" placeholder="https://api.example.com/v1">
        </div>
        <div class="ms-field">
          <label>API 格式</label>
          <select id="ms-api-format">
            ${catalog.api_formats.map((f) => `
              <option value="${f.id}" ${p.api_format === f.id ? "selected" : ""}>
                ${deps.escapeHtml(f.label)}${f.agent_compatible ? "" : " (Agent 暂不支持)"}
              </option>`).join("")}
          </select>
          <div class="ms-field-hint">Agent 循环与工具调用需要 OpenAI Chat Completions 兼容接口</div>
        </div>
        <div class="ms-field">
          <label>API Key</label>
          <div class="input-with-icon">
            <input class="text-input" id="ms-api-key" type="password"
              value="" placeholder="${p.api_key_set ? `已保存 ${p.api_key_preview}` : "输入 API Key"}">
            <button class="icon-btn-abs" id="ms-toggle-key" type="button" title="显示">
              <svg class="ic"><use href="#i-eye"/></svg>
            </button>
          </div>
        </div>
      </div>

      <div class="ms-models-head">
        <h4>模型列表</h4>
        <button class="gray-btn" id="ms-btn-add-model"><svg class="ic sm"><use href="#i-plus"/></svg>添加模型</button>
      </div>
      <table class="ms-table">
        <thead>
          <tr>
            <th>模型</th>
            <th>角色</th>
            <th>上下文</th>
            <th>标签</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${(p.models || []).map((m) => modelRowHtml(p, m)).join("")}
        </tbody>
      </table>

      <div class="ms-rag-panel">
        <h4>RAG 嵌入衔接</h4>
        <div class="ms-inline" style="margin-bottom:10px">
          <label class="ms-check-pill ${catalog.rag.use_active_provider ? "checked" : ""}" id="ms-rag-follow-wrap">
            <input type="checkbox" id="ms-rag-follow" ${catalog.rag.use_active_provider ? "checked" : ""}>
            跟随 Agent 主模型端点
          </label>
        </div>
        <div id="ms-rag-pick" ${catalog.rag.use_active_provider ? "hidden" : ""}>
          <div class="ms-field">
            <label>嵌入模型（本供应商）</label>
            <select id="ms-rag-model">
              ${(p.models || []).filter((m) => m.role === "embedding" || m.name.includes("embed"))
                .map((m) => `<option value="${m.id}" ${catalog.rag.model_id === m.id ? "selected" : ""}>${deps.escapeHtml(m.name)}</option>`)
                .join("") || '<option value="">请先添加 embedding 角色模型</option>'}
            </select>
          </div>
          <button class="gray-btn" id="ms-btn-save-rag">保存 RAG 绑定</button>
        </div>
      </div>`;
  }

  function modelRowHtml(p, m) {
    const isAgent = catalog.active.provider_id === p.id && catalog.active.model_id === m.id;
    return `
      <tr class="${isAgent ? "active-row" : ""}">
        <td><strong>${deps.escapeHtml(m.name)}</strong></td>
        <td>${m.role === "embedding" ? "嵌入" : "对话"}</td>
        <td>${formatCtx(m.context_window)}</td>
        <td>${(m.tags || []).map((t) => `<span class="tag">${t}</span>`).join(" ")}</td>
        <td class="ms-actions">
          ${m.role === "chat" && m.agent_compatible
            ? `<button title="设为 Agent 主模型" data-act="activate" data-mid="${m.id}">▶</button>` : ""}
          <button title="配置" data-act="edit" data-mid="${m.id}"><svg class="ic" style="width:14px;height:14px"><use href="#i-sliders"/></svg></button>
          <button title="探针" data-act="test" data-mid="${m.id}"><svg class="ic" style="width:14px;height:14px"><use href="#i-zap"/></svg></button>
          <button class="danger" title="删除" data-act="del" data-mid="${m.id}"><svg class="ic" style="width:14px;height:14px"><use href="#i-trash"/></svg></button>
          <label class="toggle" title="启用">
            <input type="checkbox" data-act="toggle" data-mid="${m.id}" ${m.enabled ? "checked" : ""}>
            <span class="track"></span>
          </label>
        </td>
      </tr>`;
  }

  function formatCtx(n) {
    if (!n) return "—";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1) + "M";
    if (n >= 1000) return Math.round(n / 1000) + "K";
    return String(n);
  }

  async function saveProviderFields() {
    const p = providerById(selectedProviderId);
    if (!p) return;
    const payload = {
      enabled: $("#ms-prov-enabled")?.checked,
      base_url: $("#ms-base-url")?.value?.trim(),
      api_format: $("#ms-api-format")?.value,
    };
    const key = $("#ms-api-key")?.value;
    if (key) payload.api_key = key;
    await deps.api(`/api/models/providers/${encodeURIComponent(p.id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    await refresh();
    deps.toast("供应商配置已保存", "ok");
  }

  async function refresh() {
    await loadCatalog();
    if (deps.onActiveChange) deps.onActiveChange(catalog);
  }

  function bindPage(root) {
    const $ = (sel) => root.querySelector(sel);
    const $$ = (sel) => Array.from(root.querySelectorAll(sel));

    $("#ms-btn-refresh")?.addEventListener("click", async () => {
      await refresh();
      rerender(root);
      deps.toast("已刷新", "ok");
    });

    $("#ms-btn-add-provider")?.addEventListener("click", () => showAddProviderModal(root));

    $$(".ms-prov-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedProviderId = btn.dataset.provider;
        rerender(root);
      });
    });

    $("#ms-prov-enabled")?.addEventListener("change", () => saveProviderFields().catch(errToast));
    $("#ms-base-url")?.addEventListener("change", () => saveProviderFields().catch(errToast));
    $("#ms-api-format")?.addEventListener("change", () => saveProviderFields().catch(errToast));
    $("#ms-api-key")?.addEventListener("change", () => saveProviderFields().catch(errToast));

    $("#ms-toggle-key")?.addEventListener("click", () => {
      const input = $("#ms-api-key");
      if (!input) return;
      input.type = input.type === "password" ? "text" : "password";
    });

    $("#ms-btn-test-prov")?.addEventListener("click", async () => {
      const p = providerById(selectedProviderId);
      if (!p) return;
      try {
        const r = await deps.api(`/api/models/providers/${encodeURIComponent(p.id)}/test`, { method: "POST" });
        deps.toast(r.ok ? "连接成功" : r.detail, r.ok ? "ok" : "error");
      } catch (e) { errToast(e); }
    });

    $("#ms-btn-del-prov")?.addEventListener("click", async () => {
      const p = providerById(selectedProviderId);
      if (!p || !confirm(`确定删除供应商「${p.name}」？`)) return;
      try {
        await deps.api(`/api/models/providers/${encodeURIComponent(p.id)}`, { method: "DELETE" });
        selectedProviderId = catalog.providers[0]?.id || null;
        await refresh();
        rerender(root);
        deps.toast("已删除", "ok");
      } catch (e) { errToast(e); }
    });

    $("#ms-btn-add-model")?.addEventListener("click", () => {
      const name = prompt("模型 ID（与 API 请求中的 model 字段一致）");
      if (!name?.trim()) return;
      const role = confirm("是否为嵌入模型（用于 RAG）？\n确定 = 嵌入，取消 = 对话") ? "embedding" : "chat";
      addModel(name.trim(), role).then(() => rerender(root)).catch(errToast);
    });

    $$("[data-act]").forEach((el) => {
      el.addEventListener("click", async (e) => {
        if (el.tagName === "LABEL") return;
        const act = el.dataset.act;
        const mid = el.dataset.mid;
        const p = providerById(selectedProviderId);
        if (!p) return;
        if (act === "activate") {
          try {
            await deps.api("/api/models/activate", {
              method: "POST",
              body: JSON.stringify({ provider_id: p.id, model_id: mid }),
            });
            await refresh();
            rerender(root);
            deps.toast("已设为 Agent 主模型", "ok");
          } catch (e) { errToast(e); }
        } else if (act === "edit") {
          const m = modelById(p, mid);
          if (m) showModelModal(root, p, m);
        } else if (act === "test") {
          try {
            const r = await deps.api(
              `/api/models/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}/test`,
              { method: "POST" });
            deps.toast(r.ok ? `探针成功: ${r.sample || ""}` : r.detail, r.ok ? "ok" : "error");
          } catch (e) { errToast(e); }
        } else if (act === "del") {
          const m = modelById(p, mid);
          if (!m || !confirm(`删除模型「${m.name}」？`)) return;
          try {
            await deps.api(
              `/api/models/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}`,
              { method: "DELETE" });
            await refresh();
            rerender(root);
            deps.toast("已删除", "ok");
          } catch (e) { errToast(e); }
        }
      });
      if (el.dataset.act === "toggle") {
        el.addEventListener("change", async () => {
          const p = providerById(selectedProviderId);
          try {
            await deps.api(
              `/api/models/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(el.dataset.mid)}`,
              { method: "PUT", body: JSON.stringify({ enabled: el.checked }) });
            await refresh();
          } catch (e) { errToast(e); }
        });
      }
    });

    $("#ms-rag-follow")?.addEventListener("change", async (e) => {
      const wrap = $("#ms-rag-follow-wrap");
      const pick = $("#ms-rag-pick");
      if (wrap) wrap.classList.toggle("checked", e.target.checked);
      if (pick) pick.hidden = e.target.checked;
      if (e.target.checked) {
        try {
          await deps.api("/api/models/rag", {
            method: "PUT",
            body: JSON.stringify({ use_active_provider: true }),
          });
          await refresh();
          deps.toast("RAG 已跟随 Agent 端点", "ok");
        } catch (err) { errToast(err); }
      }
    });

    $("#ms-btn-save-rag")?.addEventListener("click", async () => {
      const p = providerById(selectedProviderId);
      const mid = $("#ms-rag-model")?.value;
      if (!p || !mid) { deps.toast("请选择嵌入模型", "error"); return; }
      try {
        await deps.api("/api/models/rag", {
          method: "PUT",
          body: JSON.stringify({
            use_active_provider: false,
            provider_id: p.id,
            model_id: mid,
          }),
        });
        await refresh();
        deps.toast("RAG 嵌入配置已保存，建议执行增量索引", "ok");
      } catch (e) { errToast(e); }
    });
  }

  async function addModel(name, role) {
    const p = providerById(selectedProviderId);
    if (!p) return;
    await deps.api(`/api/models/providers/${encodeURIComponent(p.id)}/models`, {
      method: "POST",
      body: JSON.stringify({ name, role }),
    });
    await refresh();
    deps.toast("模型已添加", "ok");
  }

  function rerender(root) {
    const body = root.closest("#settings-body") || root;
    body.innerHTML = renderPage();
    bindPage(body);
  }

  function showAddProviderModal(root) {
    const backdrop = document.createElement("div");
    backdrop.className = "ms-modal-backdrop";
    backdrop.innerHTML = `
      <div class="ms-modal" role="dialog">
        <div class="ms-modal-head">
          <h3>添加供应商</h3>
          <button class="ic-btn sm" id="ms-modal-close" type="button">✕</button>
        </div>
        <div class="ms-modal-body">
          <p class="set-page-desc" style="margin-top:0">从预设快速创建，或添加空白自定义网关。</p>
          <div class="ms-preset-grid" style="margin-top:12px">
            ${catalog.presets.map((pr) => `
              <button class="ms-preset-btn" data-vendor="${pr.vendor}">
                <strong>${deps.escapeHtml(pr.name)}</strong>
                <span>${deps.escapeHtml(pr.base_url)}</span>
              </button>`).join("")}
            <button class="ms-preset-btn" data-vendor="custom">
              <strong>自定义</strong>
              <span>空白 OpenAI 兼容网关</span>
            </button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    const close = () => backdrop.remove();
    backdrop.querySelector("#ms-modal-close").addEventListener("click", close);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
    backdrop.querySelectorAll("[data-vendor]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await deps.api("/api/models/providers", {
            method: "POST",
            body: JSON.stringify({ vendor: btn.dataset.vendor }),
          });
          await refresh();
          selectedProviderId = catalog.providers[catalog.providers.length - 1]?.id;
          close();
          rerender(root.closest("#settings-body") || root);
          deps.toast("供应商已添加", "ok");
        } catch (e) { errToast(e); }
      });
    });
  }

  function showModelModal(root, provider, model) {
    const caps = model.capabilities || {};
    const inputs = model.input_types || ["text"];
    const levels = model.reasoning_levels || ["none", "low", "medium", "high"];
    const maxLevel = levels[levels.length - 1] || "medium";
    const defaultLevel = model.default_reasoning_level || "medium";
    const backdrop = document.createElement("div");
    backdrop.className = "ms-modal-backdrop";
    backdrop.innerHTML = `
      <div class="ms-modal" role="dialog">
        <div class="ms-modal-head">
          <h3>模型参数 · ${deps.escapeHtml(model.name)}</h3>
          <button class="ic-btn sm" id="ms-modal-close" type="button">✕</button>
        </div>
        <div class="ms-modal-body">
          <div class="ms-form-grid">
            <div class="ms-field">
              <label>模型 ID</label>
              <input id="mm-name" value="${deps.escapeHtml(model.name)}">
            </div>
            <div class="ms-field">
              <label>上下文窗口 (tokens)</label>
              <input id="mm-ctx" type="number" value="${model.context_window || 128000}">
            </div>
            <div class="ms-field">
              <label>最大输出 Token</label>
              <input id="mm-max" type="number" value="${model.max_output_tokens || 4096}">
            </div>
            <div class="ms-field">
              <label>Temperature</label>
              <input id="mm-temp" type="number" step="0.1" min="0" max="2" value="${model.temperature ?? 0.2}">
            </div>
            <div class="ms-field">
              <label>输入类型</label>
              <div class="ms-check-group" id="mm-inputs">
                ${["text", "image", "video", "pdf"].map((t) => `
                  <label class="ms-check-pill ${inputs.includes(t) ? "checked" : ""}">
                    <input type="checkbox" value="${t}" ${inputs.includes(t) ? "checked" : ""} ${t === "text" ? "disabled" : ""}> ${t}
                  </label>`).join("")}
              </div>
            </div>
            <div class="ms-field">
              <label>模型能力</label>
              <div class="ms-check-group" id="mm-caps">
                ${[
                  ["tools", "工具调用"], ["vision", "视觉"], ["structured_output", "结构化输出"],
                  ["web_search", "联网搜索"], ["system_message", "系统消息"],
                ].map(([k, label]) => `
                  <label class="ms-check-pill ${caps[k] ? "checked" : ""}">
                    <input type="checkbox" data-cap="${k}" ${caps[k] ? "checked" : ""}> ${label}
                  </label>`).join("")}
              </div>
            </div>
            <div class="ms-field">
              <label>思考强度上限</label>
              <div id="mm-reason-max"></div>
              <div class="ms-field-hint">滑动选择该模型支持的最高思考档位（由低到高连续启用）</div>
            </div>
            <div class="ms-field">
              <label>默认思考强度</label>
              <div id="mm-reason-default"></div>
            </div>
            <div class="ms-field">
              <label>推理参数映射 (JSON)</label>
              <textarea id="mm-reason-map">${deps.escapeHtml(JSON.stringify(model.reasoning_param_map || {}, null, 2))}</textarea>
              <div class="ms-field-hint">Agent 循环可将推理档位映射为厂商专有请求字段</div>
            </div>
          </div>
        </div>
        <div class="ms-modal-foot">
          <button class="gray-btn" id="ms-modal-cancel">取消</button>
          <button class="dark-btn" id="ms-modal-save">保存</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    const close = () => backdrop.remove();
    backdrop.querySelector("#ms-modal-close").addEventListener("click", close);
    backdrop.querySelector("#ms-modal-cancel").addEventListener("click", close);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });

    backdrop.querySelectorAll(".ms-check-pill input").forEach((inp) => {
      inp.addEventListener("change", () => {
        inp.closest(".ms-check-pill").classList.toggle("checked", inp.checked);
      });
    });

    let reasonMax = maxLevel;
    let reasonDefault = defaultLevel;
    ThinkingSlider.mount(backdrop.querySelector("#mm-reason-max"), {
      value: maxLevel,
      title: "强度上限",
      onChange: (id) => {
        reasonMax = id;
        const allowed = ThinkingSlider.levelsUpTo(id);
        if (!allowed.includes(reasonDefault)) reasonDefault = allowed[allowed.length - 1];
        defSlider.setValue(reasonDefault);
      },
    });
    const defSlider = ThinkingSlider.mount(backdrop.querySelector("#mm-reason-default"), {
      value: defaultLevel,
      title: "默认强度",
      allowed: ThinkingSlider.levelsUpTo(maxLevel),
      onChange: (id) => { reasonDefault = id; },
    });

    backdrop.querySelector("#ms-modal-save").addEventListener("click", async () => {
      let reasonMap = {};
      try {
        reasonMap = JSON.parse(backdrop.querySelector("#mm-reason-map").value || "{}");
      } catch (_) {
        deps.toast("推理参数映射 JSON 格式错误", "error");
        return;
      }
      const payload = {
        name: backdrop.querySelector("#mm-name").value.trim(),
        context_window: parseInt(backdrop.querySelector("#mm-ctx").value, 10),
        max_output_tokens: parseInt(backdrop.querySelector("#mm-max").value, 10),
        temperature: parseFloat(backdrop.querySelector("#mm-temp").value),
        input_types: ["text", ...Array.from(backdrop.querySelectorAll("#mm-inputs input:checked"))
          .map((i) => i.value).filter((v) => v !== "text")],
        capabilities: {},
        reasoning_levels: ThinkingSlider.levelsUpTo(reasonMax),
        default_reasoning_level: reasonDefault,
        reasoning_param_map: reasonMap,
      };
      backdrop.querySelectorAll("#mm-caps input[data-cap]").forEach((inp) => {
        payload.capabilities[inp.dataset.cap] = inp.checked;
      });
      try {
        await deps.api(
          `/api/models/providers/${encodeURIComponent(provider.id)}/models/${encodeURIComponent(model.id)}`,
          { method: "PUT", body: JSON.stringify(payload) });
        close();
        await refresh();
        rerender(root.closest("#settings-body") || root);
        deps.toast("模型配置已保存", "ok");
      } catch (e) { errToast(e); }
    });
  }

  function errToast(err) {
    deps.toast(err?.message || String(err), "error");
  }

  function activeLabel() {
    const p = providerById(catalog.active.provider_id);
    const m = p && modelById(p, catalog.active.model_id);
    if (!p || !m) return "未选择模型";
    return `${p.name}/${m.name}`;
  }

  function getCatalog() { return catalog; }

  function bindGlobalThinking(root) {
    const slot = root.querySelector("#ms-global-thinking");
    if (!slot || typeof ThinkingSlider === "undefined") return;
    const integ = catalog.integration?.agent || {};
    const allowed = integ.reasoning_levels || ThinkingSlider.LEVELS.map((l) => l.id);
    const level = catalog.active?.reasoning_level || "medium";
    ThinkingSlider.mount(slot, {
      value: level,
      title: "全局思考强度（Agent 循环）",
      allowed,
      onChange: async (id) => {
        try {
          await deps.api("/api/models/reasoning", {
            method: "PUT",
            body: JSON.stringify({ level: id }),
          });
          await refresh();
          deps.toast("思考强度已更新", "ok");
        } catch (e) { errToast(e); }
      },
    });
    const note = integ.reasoning_vendor?.note;
    if (note) {
      const hint = document.createElement("div");
      hint.className = "ms-field-hint";
      hint.textContent = note;
      slot.appendChild(hint);
    }
  }

  async function renderInto(body) {
    try {
      await loadCatalog();
      body.innerHTML = renderPage();
      bindPage(body);
      bindGlobalThinking(body);
    } catch (e) {
      body.innerHTML = `<p class="set-page-desc">模型配置加载失败：${deps.escapeHtml(e.message)}</p>`;
    }
  }

  return {
    init,
    loadCatalog,
    renderInto,
    getCatalog,
    activeLabel,
    refresh,
  };
})();
