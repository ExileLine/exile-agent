# TODO2

## 目标

在当前已经具备 `Agent / Toolsets / MCP / Skills / Streaming / Approval` 最小闭环的基础上，进入第二阶段：建设可运营、可灰度、可审计的 AI 配置控制面。

核心方向是把“重配置、可调节、非唯一”的能力从 `.env` 中抽离出来，进入数据库管理，例如：

- 模型 provider 与模型列表
- MCP server 配置与启用策略
- Agent 默认模型、允许模型、默认 MCP、默认 skills
- 工具审批策略、风险等级、租户/角色权限
- 预算、限流、灰度、回滚与审计

`.env` 仍保留启动级配置，数据库承载运行级配置。

---

## 当前结论

当前工程的 AI runtime 骨架已经成立：

- `app/ai/runtime` 负责 Agent 运行、history、stream、resume
- `app/ai/agents` 负责 Agent 定义
- `app/ai/toolsets` 负责 builtin toolsets、metadata、approval、audit wrapper
- `app/ai/mcp` 负责 MCP 配置解析与 toolset 装配
- `app/ai/skills` 负责 filesystem skills 的发现与解析
- `app/ai/services` 负责 API 到 runtime 的稳定调用入口

下一阶段不应该继续扩大 `.env` 配置，而应该增加“控制面配置层”：

- `.env` 只保证服务能启动
- MySQL 保存可运营配置
- Redis 或内存缓存保存热路径配置快照
- Runner 只消费解析后的 `ResolvedRunConfig`

---

## 设计原则

### 1. Bootstrap Config 与 Runtime Config 分离

启动级配置仍由 `AISettings` 管理：

- `AI_ENABLED`
- `AI_DEFAULT_AGENT`
- `AI_DEFAULT_MODEL`
- `AI_HTTP_TIMEOUT_SECONDS`
- `AI_SKILLS_DIR`
- 数据库、Redis、基础密钥、加密密钥

运行级配置进入数据库：

- 可用模型与 provider
- Agent 默认模型与模型 allowlist
- MCP server 配置与 allowlist
- Skills 启用策略
- 审批策略与工具风险策略
- 预算、限流、灰度、审计

### 2. 请求只表达意图，最终能力由策略解析

请求里的 `model`、`mcp_servers`、`skill_ids` 不能直接等价于最终启用能力。

运行前必须经过统一解析：

```text
request payload
  -> RequestContext(user / tenant / role / session)
  -> AIConfigRepository
  -> AICapabilityResolver
  -> ResolvedRunConfig
  -> AgentRunner
```

示例约束：

- 用户请求 `model=xxx`，只有在该 agent、tenant、user role 允许时才可使用
- 用户请求 `mcp_servers=["filesystem"]`，必须检查该 MCP 是否启用、是否允许该 agent 使用、是否需要审批
- Skill 声明依赖 MCP，也必须经过同一套 MCP allowlist 与策略校验

### 3. Runner 降低职责，Resolver 承担配置决策

当前 `AgentRunner` 同时处理 agent、model、skill、MCP、history、approval、stream、audit。

第二阶段应逐步拆分：

- `AIConfigRepository`：读写数据库配置
- `AIConfigCache`：缓存配置快照
- `AICapabilityResolver`：解析本轮 run 的最终能力
- `ResolvedRunConfig`：Runner 消费的稳定配置对象
- `ApprovalStore`：服务端持久化待审批 run
- `HistoryManager`：会话历史隔离、裁剪、摘要
- `ObservabilityService`：审计、trace、usage、cost

### 4. 安全默认收紧

模型、MCP、工具、skills 都应默认不可用，必须显式启用。

建议规则：

- 未启用的模型不可被请求覆盖
- 未绑定到 agent 的 MCP 不可被请求挂载
- 高风险工具默认进入 approval
- production 环境禁止从请求任意指定模型或 MCP
- secret 不明文返回给管理接口
- 所有配置变更必须记录审计

---

## 推荐数据模型

首期可以先用 SQLAlchemy model 落库，后续再补 Alembic migration。

### ai_model_provider

保存模型供应商配置。

字段建议：

- `id`
- `provider_key`：稳定标识，例如 `openai`、`deepseek`、`anthropic`
- `name`
- `provider_type`：`openai_compatible`、`anthropic`、`local` 等
- `base_url`
- `api_key_encrypted`
- `enabled`
- `timeout_seconds`
- `max_retries`
- `metadata_json`
- `created_at`
- `updated_at`

说明：

- `api_key` 不应明文保存
- 管理接口只返回 `has_api_key`
- 加密密钥放 `.env` 或后续接 KMS/Vault

### ai_model

保存具体模型。

字段建议：

- `id`
- `model_key`：内部稳定标识
- `provider_key`
- `model_name`：供应商侧真实模型名
- `display_name`
- `enabled`
- `context_window`
- `max_output_tokens`
- `supports_stream`
- `supports_tools`
- `supports_json_output`
- `input_price_per_1k`
- `output_price_per_1k`
- `risk_level`
- `metadata_json`
- `created_at`
- `updated_at`

### ai_agent_config

保存 Agent 的运行配置。

字段建议：

- `id`
- `agent_id`
- `enabled`
- `default_model_key`
- `allowed_model_keys_json`
- `default_skill_ids_json`
- `default_mcp_server_ids_json`
- `allow_request_model_override`
- `allow_request_mcp_override`
- `supports_stream`
- `approval_policy_key`
- `metadata_json`
- `created_at`
- `updated_at`

### ai_mcp_server

保存 MCP server 配置。

字段建议：

- `id`
- `server_key`
- `name`
- `transport`：`stdio`、`sse`、`streamable-http`
- `command`
- `args_json`
- `url`
- `headers_encrypted_json`
- `env_encrypted_json`
- `cwd`
- `tool_prefix`
- `enabled`
- `auto_route_enabled`
- `route_keywords_json`
- `timeout_seconds`
- `read_timeout_seconds`
- `max_retries`
- `include_instructions`
- `risk_level`
- `metadata_json`
- `created_at`
- `updated_at`

说明：

- `stdio` 类型要增加命令白名单
- `url` 类型要增加 egress allowlist
- `headers/env` 需要加密存储

### ai_agent_mcp_binding

保存 Agent 与 MCP 的绑定关系。

字段建议：

- `id`
- `agent_id`
- `server_key`
- `enabled`
- `required_approval`
- `allow_auto_route`
- `created_at`
- `updated_at`

### ai_policy

保存审批与权限策略。

字段建议：

- `id`
- `policy_key`
- `name`
- `policy_type`：`approval`、`model_access`、`mcp_access`、`tool_access`
- `enabled`
- `rules_json`
- `created_at`
- `updated_at`

### ai_approval_request

服务端持久化待审批 run，替代让客户端回传完整 `message_history_json`。

字段建议：

- `id`
- `approval_id`
- `run_id`
- `request_id`
- `agent_id`
- `session_id`
- `user_id`
- `tenant_id`
- `status`：`pending`、`approved`、`denied`、`expired`、`completed`
- `tool_calls_json`
- `message_history_json`
- `expires_at`
- `created_at`
- `updated_at`

### ai_audit_log

保存配置变更与运行审计。

字段建议：

- `id`
- `event_type`
- `request_id`
- `run_id`
- `agent_id`
- `user_id`
- `tenant_id`
- `resource_type`
- `resource_key`
- `action`
- `before_json`
- `after_json`
- `metadata_json`
- `created_at`

---

## 推荐目录补充

```text
app/
  ai/
    config_store/
      __init__.py
      models.py              # DB ORM models
      schemas.py             # 管理接口入参与出参
      repository.py          # AIConfigRepository
      cache.py               # AIConfigCache
      resolver.py            # AICapabilityResolver
      encryption.py          # secret 加解密
    runtime/
      approvals.py           # ApprovalStore / approval lifecycle
      resolved_config.py     # ResolvedRunConfig
      history_manager.py     # history isolation / trim / summary
      observability.py       # usage / trace / audit
    services/
      config_admin_service.py
    schemas/
      config_admin.py
app/
  api/
    v1/
      endpoints/
        ai_config.py          # 配置管理接口
```

---

## 分阶段开发规划

### Phase 1：配置控制面数据结构（已完成）

目标：先定义数据库模型和 repository，不改变现有 chat 行为。

任务：

- 新增 `app/ai/config_store/models.py`
- 新增 `AIModelProvider`、`AIModel`、`AIAgentConfig`、`AIMCPServer`、`AIAgentMCPBinding`
- 新增 `AIConfigRepository`
- 新增基础查询方法：
  - `get_enabled_model(model_key)`
  - `list_enabled_models()`
  - `get_agent_config(agent_id)`
  - `list_agent_mcp_servers(agent_id)`
  - `get_mcp_server(server_key)`
- 新增 repository 单元测试

验收：

- 不影响现有 `uv run pytest`
- repository 可在无真实 DB 的测试中通过 fake/session 或 sqlite 方式验证
- 数据模型不泄漏 secret 明文
- 当前状态：
  - 已落地 config_store ORM / repository / schemas / service / API 基线
  - 已支持分页、模糊搜索、中文 summary、schema description
  - 已通过 `uv run pytest`

### Phase 2：运行时配置解析器（已完成）

目标：新增 `AICapabilityResolver`，把模型、MCP、skill 的最终选择逻辑从 Runner 中抽出来。

任务：

- 定义 `ResolvedRunConfig`
- Resolver 输入：
  - `RequestContext`
  - `AgentChatRequest`
  - `AgentManifest`
  - DB 配置快照
- Resolver 输出：
  - `agent_id`
  - `model_key`
  - `model_name`
  - `provider_config`
  - `skill_ids`
  - `mcp_server_keys`
  - `approval_policy`
  - `runtime_flags`
- 增加 allowlist 校验：
  - 请求模型必须属于 agent allowed models
  - 请求 MCP 必须属于 agent 绑定 MCP
  - disabled 配置不可被解析出来
- 保留 fallback：数据库无配置时沿用当前 `AISettings`

验收：

- 现有 chat 流程行为保持不变
- 新增 resolver 测试覆盖：
  - 默认模型
  - 请求覆盖模型
  - 禁用模型拒绝
  - MCP allowlist
  - 数据库缺省 fallback
- 当前状态：
  - 已新增 `ResolvedRunConfig`
  - 已新增 `AICapabilityResolver`
  - 已实现数据库配置命中与 `settings_fallback` 双路径
  - 已覆盖模型 allowlist、MCP binding、disabled 配置校验

### Phase 3：接入 Runner（已完成）

目标：Runner 消费 `ResolvedRunConfig`，降低 Runner 自己的配置决策职责。

任务：

- [x] 在 `AgentRunner.run_chat`、`run_chat_stream`、`resume_chat` 前置调用 resolver
- [x] 替换当前 `_resolve_agent`、`_resolve_request_toolsets` 中的部分逻辑
- [x] MCPManager 支持从 DB config 构造 server
- [x] AgentManager 支持 model provider config 构造模型
- [x] 响应 `meta` 增加 `model_key`、`provider_key`、`config_version`

验收：

- [x] 现有测试继续通过，当前为 `46 passed`
- [x] DB provider 可构造 `OpenAIChatModel + OpenAIProvider`
- [x] DB MCP 配置可构造运行时 MCP server/toolset，并支持 headers/env 解密
- [x] DB Agent 缺少同名静态注册时，可复用默认 `chat-agent` builder，并保留业务 `agent_id`
- [x] DB 控制面启用时关闭旧 MCP 自动路由，避免绕过 binding 校验
- [x] DB 控制面支持在 Agent 已绑定 MCP 范围内按关键词安全自动路由
- [x] 新增测试证明 DB MCP 配置可以直接影响 MCP 装配
- [x] 请求非法模型/MCP 时返回更明确的 4xx/502 错误，而不是统一 500

### Phase 4：配置管理 API（基础能力已完成，治理待补）

目标：提供最小可用的配置管理接口。

任务：

- [x] 新增 `/api/v1/ai-config/model-providers`
- [x] 新增 `/api/v1/ai-config/models`
- [x] 新增 `/api/v1/ai-config/agents/{agent_id}/config`
- [x] 新增 `/api/v1/ai-config/mcp-servers`
- [x] 新增 `/api/v1/ai-config/agents/{agent_id}/mcp-bindings`
- [ ] 增加基础鉴权占位，避免公开配置管理接口
- [ ] 所有写操作记录 `ai_audit_log`

验收：

- [x] OpenAPI 可见配置管理接口
- [x] secret 字段写入时加密，读取时不返回明文
- [ ] 配置变更有审计记录

当前状态：

- 已支持分页和关键词模糊搜索
- 已支持中文 summary 和 schema description
- 写接口已采用 `PUT` 更新并使用数据库主键 ID
- 后续重点是鉴权、审计、配置变更事件和更完整的 secret/KMS 方案

### Phase 5：服务端 Approval Store

目标：替换当前客户端回传完整 `message_history_json` 的无状态 resume 协议。

任务：

- [x] 新增 `ApprovalStore`
- [x] approval_required 时服务端保存：
  - message history
  - tool calls
  - user/tenant/session/request/run
  - 过期时间
- [x] 响应返回 `approval_id`、待审批工具摘要、过期时间和状态
- [x] `/chat/resume` 支持通过 `approval_id` 续跑，并保留旧版 `message_history_json` 兼容
- [x] 审批时校验：
  - 审批状态
  - 过期时间
  - 审批人权限
  - tool_call_id 是否匹配
- [ ] 增加 args hash 校验
- [ ] 增加审批单查询/撤销接口
- [ ] 将审批生命周期事件持久化到审计日志

验收：

- [x] 旧协议可临时保留兼容，但新协议优先
- [x] 客户端无法通过新协议篡改 message history 影响 resume
- [x] approval 可过期，完成后不可重复续跑
- [ ] approval 状态可查询、可撤销、可审计

当前状态：

- 已完成 Redis 优先、内存 fallback 的 `ApprovalStore`
- `/chat` 和 `/chat/stream` 进入 `approval_required` 时会创建服务端审批单
- `/chat/resume` 可只传 `approval_id + approvals` 续跑，不再要求客户端回传完整 `message_history_json`
- 已补充服务端审批单续跑、重复续跑拒绝、未知 `tool_call_id` 拒绝和流式审批事件测试

### Phase 6：History Manager

目标：让会话历史具备隔离、裁剪和摘要能力。

任务：

- session key 加入 tenant/user/agent 维度
- 保存 message metadata：
  - model
  - skills
  - MCP
  - token usage
  - created_at
- 增加 token/message 数量上限
- 增加历史裁剪策略
- 预留摘要压缩接口

验收：

- 不同 user/tenant 的同名 session 不串线
- 长会话不会无限增长
- 历史裁剪有测试覆盖

### Phase 7：观测与成本

目标：把内存审计升级为生产可用审计与 metrics。

任务：

- 将 tool exposure / execution 写入审计表或结构化日志
- 记录 run latency、model latency、tool latency
- 记录 token usage 与估算 cost
- 增加错误分类：
  - model_error
  - tool_error
  - mcp_error
  - policy_denied
  - approval_timeout
- 增加 request_id / run_id / session_id 贯穿日志

验收：

- 每次 run 可通过 run_id 查到关键事件
- tool args 和 result 做脱敏或摘要
- usage/cost 可聚合统计

---

## 首批开发顺序

当前已完成：

1. `config_store` 数据模型与 repository
2. `ResolvedRunConfig` 与 `AICapabilityResolver`
3. Runner 接入 resolver，并保持 settings fallback
4. MCP 从 DB config 构造和安全自动路由
5. 配置管理 API 基础能力
6. DB 控制面异常 4xx/502 化

建议继续按这个顺序推进：

1. 服务端 approval store
2. history manager
3. observability / guardrails
4. 业务 Agent 落地

原因：

- 模型和 MCP 配置数据库化已完成，可以支撑后续管理后台和灰度策略
- resolver 已成为权限、灰度、预算、审批的统一入口
- Runner 已完成减负，后续应避免继续把策略分支堆回 Runner
- approval store 和 history manager 涉及协议变化，应在业务 Agent 大规模接入前推进

---

## 开发约束

- 每个阶段都必须保持现有测试通过
- 新能力先有测试，再接入主链路
- 不在 Runner 中继续堆配置分支
- 不让请求参数绕过 DB 策略直接生效
- 不明文返回 secret
- 不把 `.env` 扩展成巨型 AI 配置中心
- 数据库配置缺省时，必须有兼容当前 `.env` 的 fallback

---

## 待确认问题

- 当前是否需要多租户。如果暂时没有，也建议数据结构预留 `tenant_id`
- 配置管理接口的鉴权来源是什么，是已有用户体系、API key，还是先做内部 header 占位
- secret 加密首期使用 Fernet/AES-GCM 本地密钥，还是直接规划 KMS/Vault
- MCP stdio 命令白名单首期允许哪些命令
- 模型价格是否需要首期用于成本统计，还是先只记录 usage
