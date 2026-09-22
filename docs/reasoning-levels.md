# 思考强度（Reasoning Level）架构

> 关联模块：`app/reasoning.py` · `app/model_registry.py` · `tools/api_client.py` · `app/usage_tracker.py`
> 前端：`app/web/thinking-slider.js` · `app/web/app.js` · `app/web/model-settings.js`

## 1. 设计目标

思考强度是**跨层配置**：用户在 UI 选档位 → 注册表持久化 → 运行时注入 LLM 请求体 → Agent 各框架共用 → 统计与可观测性闭环。

```text
UI (ThinkingSlider)
  → PUT /api/models/reasoning  （全局持久化）
  → POST /api/chat/stream { reasoning_level }  （请求级覆盖，finally 恢复）
ModelRegistryState.active_reasoning_level
  → apply_registry_to_config()
  → cfg.llm.reasoning_level + cfg.llm.registry
LLMClient._payload() / 可选框架 create_args
  → merge_into_payload(vendor, model_map)
厂商 API（thinking / reasoning_effort / …）
  → usage / reasoning_content
  → SSE thought + status + UsageTracker
  → GET /api/stats · /api/health.usage
```

## 2. 档位与厂商能力矩阵

全局档位（`LEVEL_ORDER`）：`none → low → medium → high → xhigh → max`

| 厂商 | UI 可选档位 | API 区分上限 | 说明 |
| --- | --- | --- | --- |
| deepseek | 全部 6 档 | max | `thinking.budget_tokens` 2K–64K |
| qwen | 全部 6 档 | max | `thinking_budget` 2K–64K |
| openai | none–high | high | `reasoning_effort` 仅 low/medium/high |
| glm | none–high | high | `thinking.type=enabled`，低档回退 |
| custom | 依模型配置 | max | 靠 `reasoning_param_map` |

**有效档位** = `模型.reasoning_levels ∩ 厂商.native_levels`（`effective_reasoning_levels()`）。

OpenAI 不在 UI 暴露 xhigh/max，避免「档位变了、API 不变」的虚假梯度。

## 3. 载荷合并规则

1. `level == "none"` → 不注入任何字段
2. 厂商默认 `VENDOR_DEFAULT_MAPS[vendor][level]`
3. 模型级 `reasoning_param_map[level]` 覆盖/合并
4. 厂商未定义低档时，向更低档位回退（如 GLM `low` → `medium` 映射）

注入点：

- **主路径**：`LLMClient._payload()` → `merge_into_payload()`
- **LlamaIndex**：`OpenAILike(additional_kwargs=...)`
- **AutoGen**：`OpenAIChatCompletionClient(create_args=...)`
- **CrewAI**：`LLM(extra_body=...)`

## 4. 可观测性

| 事件 | 字段 | 含义 |
| --- | --- | --- |
| `thought` | `content`, `source=model` | 模型内部推理文本 |
| `status` | `usage`, `latency_ms`, `reasoning_level` | 单次 LLM 调用摘要 |
| `done` | `usage`, `latency_ms` | 流式收尾（若使用 stream_chat） |

`app/usage_tracker.py` 在 `routes_chat` 的 `finally` 中按轮次聚合，**不含消息正文**。

`GET /api/stats` 返回累计指标、近 7 天趋势、按思考强度分布。

## 5. 与 Agent 框架的关系

| 框架 | 思考强度生效 | thought 来源 |
| --- | --- | --- |
| native_react | `llm.chat` 载荷 | 模型推理 + 文本动作兜底 |
| state_loop | 每次 `call_model` | 模型推理 + 状态机描述 |
| autogen / llamaindex / crewai | 框架 LLM 构造参数 | 框架自身输出 |

思考强度**不改变**控制流（ReAct 轮次 / state_loop 转移表），只影响模型侧推理深度与 token 预算。

## 6. 配置字段对照

| 位置 | 字段 | 作用 |
| --- | --- | --- |
| `ModelEntry` | `reasoning_levels` | 模型支持上限（设置中心滑动条） |
| `ModelEntry` | `default_reasoning_level` | 激活模型时的默认档位 |
| `ModelEntry` | `reasoning_param_map` | 按档位覆盖 API 字段 |
| `ModelRegistryState` | `active_reasoning_level` | 当前全局 Agent 档位 |
| `cfg.llm.registry` | `reasoning_level` / `reasoning_levels` | 运行时有效值 |

## 7. 探针与验证

- 模型测试：`POST /api/models/providers/{id}/models/{id}/test` 会带思考强度载荷
- 本地验证：`python scripts/verify_reasoning_chain.py`
- 单元测试：`tests/test_reasoning.py` · `tests/test_usage_tracker.py` · `tests/test_reasoning_integration.py`
