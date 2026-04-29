# TODO

## 目标

在当前 `FastAPI` 服务端基础项目上，集成一套基于 `PydanticAI` 的通用 AI 基建设施，覆盖以下核心能力：

- `Agent` 运行时与注册中心
- `FunctionTools`/`Toolsets` 工具注册与治理
- `MCP` 服务接入与生命周期管理
- `Skills` 技能发现、装载、渐进式披露
- `Chat/Run API`、流式事件、会话历史、审计与测试

目标不是一次性做成“全功能 AI 平台”，而是先搭出一套可演进、可测试、可治理的骨架，后续按阶段逐步实现业务 Agent。

---

## 当前进度

已完成：

- 已完成 `app/ai/` 基础边界、Agent 注册中心、Runner、Chat API、流式 SSE、会话历史、工具审计和 Skills 基线。
- 已完成 AI 配置控制面基础表、repository、schemas、管理 API，支持模型供应商、模型、Agent 配置、MCP server、Agent-MCP binding 管理。
- 已完成 `AICapabilityResolver`，Runner 主链路已接入 `ResolvedRunConfig`，`chat / stream / resume` 都走统一配置解析。
- 已支持 DB provider 构造运行时模型对象，`openai` / `openai_compatible` 会转换为 `OpenAIChatModel + OpenAIProvider`。
- 已支持 DB MCP 配置构造运行时 MCP server/toolset，并完成 headers/env 解密、配置指纹缓存和绑定范围内的自动路由。
- 已支持 DB Agent 配置复用默认 runtime builder，允许类似 `yyx-agent` 这样的业务 Agent 配置在没有同名静态 builder 时运行。
- 已在响应 `meta` 返回 `config_source`、`model_key`、`provider_key`、`config_version`，便于调试和治理。
- 已完成 DB 控制面异常 4xx/502 映射，模型/MCP/Skill 配置类错误不再统一返回 500。
- 当前自动化测试基线：`46 passed`。

当前待补：

- 配置管理接口已有基础能力，但还需要补鉴权策略、审计日志持久化和更完整的 secret/KMS 方案。
- Approval Store 仍是下一阶段重点，需要替换当前客户端回传完整 `message_history_json` 的无状态 resume 协议。

---

## 当前项目适配结论

当前项目具备以下优点，适合承载 AI 基建：

- `app/main.py` 与 `app/core/lifespan.py` 已集中管理应用启动/关闭逻辑，适合挂载 AI 运行时资源
- `app/core/config.py` 已统一环境配置，适合扩展模型、MCP、Skills 相关配置
- `app/db/session.py`、`app/db/redis_client.py` 已具备 DB/Redis 基础设施，可作为 Agent deps 的一部分
- 当前 `api` 层较薄，适合新增独立的 `ai` 服务层，避免把 Agent 逻辑散落在 endpoint 中

当前仍需补齐的能力：

- Streaming SSE 路径的错误事件协议还需要进一步标准化
- 配置管理接口还缺少正式鉴权、审计日志持久化和操作人维度
- Approval 仍是无状态 `message_history_json` 回传协议，后续应改为服务端 Approval Store
- History 还缺少 tenant/user/agent 维度隔离、裁剪和摘要能力
- Observability 还需要 run/tool/model 级事件持久化、usage/cost 聚合和脱敏策略

因此，当前重点已从“搭骨架”转向“治理、安全、可观测和协议稳定化”。

---

## 设计原则

### 1. Agent 定义与 Web 层解耦

- `FastAPI endpoint` 只负责鉴权、参数校验、调用 `agent service`
- `PydanticAI Agent` 的定义、运行和上下文装配统一放在 `app/ai/` 内
- endpoint 不直接拼接 prompt，不直接注册 tool

### 2. 运行时依赖显式注入

- 使用 `PydanticAI deps_type + RunContext`
- 所有工具、动态 instructions、hooks 都通过统一的 `AgentDeps` 访问外部资源
- 不让工具函数直接隐式读取全局状态

### 3. Tool 要可组合、可替换、可治理

- 简单本地函数工具可先用 `@agent.tool` / `@agent.tool_plain`
- 业务场景统一沉淀为 `toolsets`
- 对工具做前缀、过滤、审批、审计、超时、元数据标注，避免后期失控

### 4. MCP 与 Skills 不直接绑死在单个 Agent 上

- MCP server 和 skill 都属于“可插拔能力”
- 运行时按配置、租户、用户、agent 类型动态组合
- 避免将特定 skill / MCP server 写死在 endpoint 中

### 5. 首期优先最小可运行闭环

- 先完成单 Agent、单模型、少量本地工具、可选 MCP、文件系统 skills
- 再补流式输出、审批、会话历史压缩、多 Agent 编排

### 6. 观测、测试、安全前置

- 从第一阶段开始就保留 hooks、usage、request_id、tool audit 的扩展位
- 使用 `TestModel` / `FunctionModel` 建立测试基线
- 为高风险工具预留 approval/deny 机制

---

## 推荐目录规划

建议新增：

```text
app/
  ai/
    __init__.py
    config.py                # AI 相关配置读取与校验
    constants.py             # 固定枚举、默认值
    deps.py                  # AgentDeps / RequestContext / UserContext
    exceptions.py            # AI 运行时异常
    schemas/
      __init__.py
      agent.py               # Agent manifest / metadata
      chat.py                # chat request/response
      events.py              # stream events / tool approval payload
      skill.py               # skill manifest
      session.py             # session / message history schema
    runtime/
      __init__.py
      registry.py            # agent registry / builder registry
      manager.py             # AgentManager，统一获取 agent
      runner.py              # run / run_stream / resume / approval flow
      history.py             # message history store / processors
      approvals.py           # deferred tool approval handling
      observability.py       # hooks / trace / usage / audit
    agents/
      __init__.py
      base.py                # BaseAgentSpec / builder contract
      manifests.py           # agent metadata registry
      chat_agent.py          # MVP 示例 agent
    toolsets/
      __init__.py
      builtin.py             # 基础工具集，如 time/health/config
      business.py            # 后续业务工具集
      wrappers.py            # prefix/filter/approval/audit wrappers
    mcp/
      __init__.py
      config.py              # MCP server config parsing
      manager.py             # MCP client lifecycle
      loader.py              # dynamic toolset assembly
    skills/
      __init__.py
      models.py              # SkillManifest
      loader.py              # filesystem/programmatic loader
      registry.py            # skill registry
      resolver.py            # 根据 query / agent / tags 选择 skill
      renderer.py            # 渐进式披露，先摘要后加载正文
      catalog/
        example_skill/
          skill.yaml
          SKILL.md
    services/
      __init__.py
      chat_service.py        # API -> runtime 的服务入口
app/
  api/
    v1/
      endpoints/
        agent.py             # chat/run/stream/session endpoints
```

说明：

- `app/ai/runtime` 负责“运行”
- `app/ai/agents` 负责“定义”
- `app/ai/toolsets` 负责“能力提供”
- `app/ai/mcp` 负责“外部工具协议接入”
- `app/ai/skills` 负责“知识/流程技能管理”
- `app/ai/services` 负责给 `FastAPI endpoint` 提供稳定调用面

---

## 关键抽象设计

### A. AgentDeps

建议定义统一依赖容器，例如：

```python
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


@dataclass
class RequestContext:
    request_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    session_id: str | None = None


@dataclass
class AgentDeps:
    request: RequestContext
    settings: "AISettings"
    db_session_factory: async_sessionmaker[AsyncSession] | None
    redis: Redis | None
    http_client: httpx.AsyncClient
    mcp_manager: "MCPManager"
    skill_registry: "SkillRegistry"
    tool_audit: "ToolAuditService"
```

约束：

- 工具、动态 instructions、history processors、hooks 一律通过 `ctx.deps` 访问资源
- 不直接把 `FastAPI Request` 塞进工具层
- 如果后续需要用户权限、租户、灰度标记，可只扩展 `RequestContext`

### B. Agent Registry

需要一个统一注册中心，而不是在 endpoint 内散落实例：

- `AgentManifest`: `agent_id`、名称、说明、默认模型、默认 skills、默认 MCP、是否支持流式、是否支持审批
- `AgentBuilder`: 根据 `AgentDeps` 和运行参数返回 `PydanticAI Agent`
- `AgentManager`: 对外统一 `get_agent(agent_id)` / `list_agents()`

建议：

- `Agent` 对象按“定义”复用
- 与请求强相关的 `toolsets / mcp / skills / history / metadata` 在运行时追加
- 避免把用户态数据直接固化在全局 agent 单例中

### C. Toolsets 优先于零散工具

虽然 `PydanticAI` 支持直接 `@agent.tool`，但项目内建议以 `toolsets` 为主体：

- 基础公共工具：`system`, `time`, `health`, `config_readonly`
- 业务工具：按业务域拆分，如 `customer_toolset`, `order_toolset`
- 外部工具：`MCP toolset`, `External toolset`
- 包装工具：`Prefixed`, `Filtered`, `ApprovalRequired`, `Wrapper/Audit`

建议分层：

- `tool_plain`: 纯函数、无外部上下文
- `tool`: 需要 `RunContext[AgentDeps]`
- `FunctionToolset`: 用于可复用工具包
- `dynamic toolset`: 用于按请求动态拼装 skills / MCP

### D. MCP Manager

MCP 不应直接散落在 agent 定义中，建议做统一装配层：

- 解析配置中的 `stdio/http/sse` MCP server 定义
- 应用启动时校验配置，不一定全部预连接
- agent run 前按 `agent_id + tenant + request flags` 动态选取 server
- 将 server 转换为 `toolsets`
- 生命周期交给 `lifespan` 或 `MCPManager`

建议支持两类来源：

- 静态配置型：环境变量或配置文件定义
- 动态启用型：请求参数声明临时启用哪些 MCP server

首期只做：

- 读取配置
- 装配 toolset
- 基础错误处理和日志

后续再补：

- 连接池 / 复用
- 权限分级
- 健康检查
- 超时与熔断

### E. Skills 作为“轻量能力包”

这里不建议把 skill 理解成“另一个 Agent”，而应先定义为：

- 一组可发现的能力说明
- 一段或多段可注入的 instructions
- 可选依赖的 toolsets / MCP servers / docs files
- 支持渐进式披露，减少 token 消耗

建议 skill 结构：

- `skill.yaml`
  - `name`
  - `title`
  - `description`
  - `tags`
  - `enabled`
  - `priority`
  - `load_strategy` (`summary_only`, `full_on_match`)
  - `allowed_agents`
  - `required_toolsets`
  - `required_mcp_servers`
  - `instruction_files`
- `SKILL.md`
  - 面向模型的任务说明、执行规范、边界、注意事项

Skill Resolver 建议职责：

- 根据 `agent_id`、用户输入、tag、策略，选出候选 skills
- 首轮仅注入 skill 摘要
- 命中后再加载正文
- 将 skill 依赖的 toolset / MCP 一并挂到本次运行

这样做的好处：

- 后续可兼容第三方 `pydantic-ai-skills`
- 当前先做本地文件系统 skills，不被第三方实现细节绑定

### F. Runtime Runner

需要一个统一 Runner，而不是 endpoint 直接 `agent.run(...)`：

- `run()`: 普通同步式/一次性响应
- `run_stream()`: SSE 或 chunk stream
- `resume()`: 处理 deferred approvals 或外部工具结果
- `run_with_history()`: 带会话历史

Runner 负责：

- 构建 `AgentDeps`
- 加载 agent
- 组合 toolsets / MCP / skills
- 注入 metadata/request_id
- 处理 `message_history`
- 记录 usage / tool calls / errors

### G. History / Session

项目已具备 `Redis`，首选用 Redis 存历史与运行中间态：

- `session:{session_id}:messages`
- `session:{session_id}:summary`
- `run:{run_id}:approval_state`

首期建议：

- 先存完整 message history
- 支持按 `session_id` 连续对话
- 先不做复杂摘要压缩

第二阶段再做：

- `history_processors`
- 滑动窗口
- 摘要压缩
- 大工具输出裁剪

### H. Observability / Audit

需要从第一期就预留：

- `request_id`、`agent_id`、`session_id`、`user_id`
- 模型名、token usage、耗时
- tool 调用记录、参数摘要、返回摘要、错误
- MCP server 调用统计

实现建议：

- 用 `Hooks` / wrapper toolset 统一审计
- 日志先写本地 logger
- 后续可接 `Pydantic Logfire` 或 OpenTelemetry

### I. Approval / Safety

对高风险工具必须预留审批能力：

- 删除、写入、外部调用、执行代码、敏感查询类工具默认走审批
- 使用 `requires_approval` 或 `ApprovalRequiredToolset`
- 如果本次 run 输出是审批请求，则返回给前端 `DeferredToolRequests`
- 前端确认后，调用 `resume` 接口继续执行

首期即便前端审批 UI 还没做，也要把后端协议设计出来。

---

## API 设计建议

建议新增如下接口：

### 1. `POST /api/v1/agents/chat`

用途：

- 单轮或多轮聊天
- 支持指定 `agent_id`
- 可选 `session_id`
- 可选启用 skills / MCP servers

请求示意：

```json
{
  "agent_id": "chat-agent",
  "message": "帮我检查服务健康状态",
  "session_id": "sess_xxx",
  "stream": false,
  "skill_tags": ["ops"],
  "mcp_servers": ["local-filesystem"]
}
```

响应建议：

- `status`
- `message`
- `agent_id`
- `session_id`
- `run_id`
- `usage`
- `tool_calls`
- `deferred_tool_requests`

### 2. `POST /api/v1/agents/chat/stream`

用途：

- 流式输出
- 后续对接前端 SSE / WebSocket

首期建议用 `SSE`

### 3. `POST /api/v1/agents/chat/resume`

用途：

- 继续处理工具审批结果
- 或处理外部 deferred tool result

当前实现说明：

- 当前已落地无状态 `resume` 协议
- 前端需要回传 `message_history_json + approvals`
- 后续如要升级为审批中心，再考虑引入 `run_id` 级状态持久化

### 4. `GET /api/v1/agents`

用途：

- 查询当前可用 agent 清单

### 5. `GET /api/v1/skills`

用途：

- 查询当前 skills 注册情况，便于调试

---

## 配置设计建议

建议在 `app/core/config.py` 中补充 AI 配置，或拆出 `app/ai/config.py` 再由主配置统一装配。

建议增加环境变量：

```env
AI_ENABLED=true
AI_DEFAULT_AGENT=chat-agent
AI_DEFAULT_MODEL=openai:gpt-5.2
AI_MAX_RETRIES=2
AI_MAX_TOOL_CALLS=16
AI_HISTORY_BACKEND=redis
AI_HISTORY_TTL=86400
AI_SKILLS_DIR=app/ai/skills/catalog
AI_ENABLE_STREAMING=true
AI_ENABLE_MCP=true
AI_ENABLE_APPROVALS=true
AI_TOOL_TIMEOUT_SECONDS=30

OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=

AI_MCP_SERVERS_JSON={}
```

说明：

- 模型厂商密钥按实际 provider 增减
- `AI_MCP_SERVERS_JSON` 首期先用 JSON 配置，当前已经支持 `{ "mcpServers": { ... } }`、数组、以及 `{id: config}` 三种输入形态
- skills 目录建议支持相对路径

---

## 分阶段实施清单

## 当前进度概览

截至目前，项目已经完成的核心基建有：

- `Phase 0` 设计落盘
- `Phase 1` AI 最小运行骨架
- `Phase 2` Toolsets 与工具治理基础能力
- `Phase 3` 会话历史与运行时管理
- `Phase 4` MCP 基础接入
- `Phase 5` Skills 基础设施
- `Phase 6` Streaming / Approval / External Tools 的最小闭环
- DB 配置控制面主链路：模型、Agent、MCP、resolver、Runner 接入

当前已经具备：

- `chat-agent` 最小可运行链路
- `AgentRegistry` / `AgentManager` / `AgentRunner`
- `POST /api/v1/agents/chat`
- `POST /api/v1/agents/chat/stream`
- `POST /api/v1/agents/chat/resume`
- `GET /api/v1/agents`
- 基于 `session_id` 的基础 history load/save
- `/chat` / `/chat/resume` / `/chat/stream` 统一 run metadata
- 分域 builtin toolsets
- tool metadata / conventions / audit wrapper
- metadata 驱动的 approval policy
- `DeferredToolRequests` / `DeferredToolResults` 审批续跑协议
- approval 端到端测试
- 多轮对话基础测试
- 流式文本输出与 approval fallback 流式测试
- README 调用链与 approval 闭环文档
- 数据库模型供应商、模型、Agent、MCP server、Agent-MCP binding 管理接口
- `AICapabilityResolver` 与 `ResolvedRunConfig`
- DB provider 到运行时模型对象的构造
- DB MCP 到运行时 MCP toolset 的构造和自动路由

当前最自然的下一阶段是：

- 服务端 Approval Store
- History Manager 与 Observability / Guardrails

## Phase 0 - 设计落盘

- [x] 评估当前 FastAPI 项目结构
- [x] 明确 `PydanticAI` 集成分层
- [x] 设计 `Agent + Toolsets + MCP + Skills + Runtime + API`
- [x] 将实施方案记录在 `TODO.md`

验收标准：

- 本文件成为后续实现基线

## Phase 1 - AI 最小运行骨架

- [x] 在 `pyproject.toml` 中增加 `pydantic-ai` 相关依赖
- [x] 新建 `app/ai/` 基础目录
- [x] 定义 `AISettings`
- [x] 定义 `RequestContext`、`AgentDeps`
- [x] 实现 `AgentManifest`、`AgentRegistry`、`AgentManager`
- [x] 实现一个最小 `chat-agent`
- [x] 在 `lifespan` 中初始化 AI 运行时所需资源
- [x] 新增 `POST /api/v1/agents/chat`
- [x] 新增 `GET /api/v1/agents`
- [x] 增加 OpenAI 兼容 provider 配置支持（`OPENAI_API_KEY` / `OPENAI_BASE_URL`）
- [x] 完成 README 调用链文档
- [x] 为最小 agent 增加基础测试

验收标准：

- 可以通过 API 调起一个最小 agent
- 可以通过 API 查看已注册 agent
- 可以从 `deps` 中拿到 http client / redis / db factory
- 项目结构中出现明确 `app/ai/` 边界
- 真实模型与 `TestModel` 均可跑通最小链路

## Phase 2 - Toolsets 与工具治理

- [x] 实现基础 `FunctionToolset`
- [x] 增加首批系统类只读工具（`get_current_utc_time`、`get_request_context`、`get_runtime_config_summary`、`check_runtime_resources`）
- [x] 按能力域拆分 builtin toolsets（`time` / `request` / `runtime`）
- [x] 统一工具命名、描述、参数 schema 规范
- [x] 实现 tool metadata 标注
- [x] 收敛 tool metadata 约定（`toolset.id/kind/owner`、`category`、`readonly`、`risk`、`approval_required`、`tags`）
- [x] 建立工具注册约定文档（README）
- [x] 实现 wrapper/audit toolset
- [x] 实现最小 tool execution audit（success/error）
- [x] 对高风险工具预留 approval 配置位（metadata policy wrapper）

当前阶段说明：

- 已具备分域 builtin toolsets 组合挂载能力
- 已具备最小 tool exposure audit
- 已具备最小 tool execution audit
- 已具备 metadata 驱动的 approval policy wrapper
- Phase 2 最小目标已完成，后续增强项主要集中在更细粒度的 audit 扩展

验收标准：

- agent 能按 toolset 装配工具
- tool 调用日志可追踪
- 工具不直接依赖 web 层对象

## Phase 3 - 会话历史与运行时管理

- [x] 实现基础 history store（优先 Redis，无 Redis 时内存兜底）
- [x] 支持 `session_id` 多轮对话
- [x] 抽象 `run` / `resume`
- [x] 抽象最小 `run_stream`
- [x] 统一基础 run metadata、usage、error handling
- [x] 支持历史加载与保存
- [x] 为历史存储增加测试

当前阶段说明：

- `Runner` 已成为普通运行与审批续跑的统一入口
- 基础 `session_id` 历史读写已经接通
- 已具备基础 `SSE run_stream`
- 已支持 `tool_call` / `tool_result` / `approval_pending` 等关键流式事件
- 已具备统一基础 run metadata / error handling
- 当前这轮最小运行时增强已经完成
- 后续增强点主要集中在历史裁剪/摘要与更完整的平台级流式协议

验收标准：

- 同一个 `session_id` 可以连续对话
- 异常与 usage 信息可回传
- Runner 成为唯一运行入口

## Phase 4 - MCP 基础接入

- [x] 定义 `MCPServerConfig`
- [x] 实现 `MCPManager`
- [x] 支持从配置加载 MCP server
- [x] 支持从数据库 MCP 配置动态构造 MCP server
- [x] 支持为指定 agent/run 动态装配 MCP toolset
- [x] 支持未显式传入 `mcp_servers` 时按消息关键词自动路由 MCP
- [x] 支持 DB 控制面下只在 Agent 已绑定 MCP 范围内自动路由
- [x] 增加 MCP 错误处理、超时和日志
- [x] 新增一个示例 MCP server 配置与联调说明

验收标准：

- 指定请求可以显式或自动启用 MCP 工具
- MCP 生命周期不散落在 endpoint 中
- MCP 失败不会拖垮整个服务进程

当前阶段说明：

- 已新增 `app/ai/mcp/config.py` 与 `app/ai/mcp/manager.py`
- `init_ai_runtime(...)` 会统一初始化并挂载 `ai_mcp_manager`
- `/chat`、`/chat/stream`、`/chat/resume` 已支持通过 `mcp_servers` 做请求级动态装配
- settings fallback 路径下，`MCPManager` 会基于 `route_keywords` 做自动 MCP 路由
- DB 控制面路径下，`AICapabilityResolver` 会基于 Agent 绑定和 `route_keywords_json/server_key/tool_prefix` 做安全自动路由
- MCP toolset 会统一经过现有 approval / audit wrapper
- 当前环境如果未安装 `mcp` SDK，服务仍可正常启动；只有真正构建 MCP server 时才会返回明确错误
- 已提供本地 mock MCP server 与联调测试路径
- 本阶段目标已经完成，下一阶段进入 `Phase 5 - Skills`

## Phase 5 - Skills 基础设施

- [x] 定义 `SkillManifest`
- [x] 实现 filesystem `SkillLoader`
- [x] 实现 `SkillRegistry`
- [x] 实现 `SkillResolver`
- [x] 支持 skill 摘要注入
- [x] 支持按命中加载 `SKILL.md`
- [x] 支持 skill 依赖的 toolsets / MCP 自动挂载
- [x] 增加示例 skill

当前阶段说明：

- 已新增 `app/ai/skills/models.py`、`loader.py`、`registry.py`、`resolver.py`
- `init_ai_runtime(...)` 会统一初始化并挂载 `ai_skill_loader`、`ai_skill_registry`、`ai_skill_resolver`
- `/chat`、`/chat/stream`、`/chat/resume` 已支持通过 `skill_ids` / `skill_tags` 做请求级 skill 装配
- 未显式传入 skill 时，`SkillResolver` 也可基于 `route_keywords` 做消息命中
- 当前已支持“summary 优先，full_on_match 时加载 `SKILL.md` 正文”的渐进式披露策略
- skill 依赖的 toolsets / MCP servers 会并入本轮 run 的动态能力装配
- 已提供示例 skill 与基础自动化测试
- 本阶段目标已经完成，下一阶段进入 `Phase 7 - Observability / Guardrails / Tests`

验收标准：

- agent run 可动态带入 skills
- skills 不需要写死在 agent 代码中
- skills 具备渐进式披露能力

## Phase 6 - Streaming / Approval / External Tools

- [x] 增加 `chat/stream` SSE 接口
- [x] 引入 `DeferredToolRequests` / `DeferredToolResults`
- [x] 实现无状态 `chat/resume` 协议
- [x] 对敏感工具启用审批流程
- [ ] 如有前端工具调用需求，评估 `ExternalToolset`
- [x] 增加 approval 端到端测试
- [x] 增加 stream 相关测试

当前阶段说明：

- approval 最小闭环已完成
- SSE stream 接口已完成，并已补齐工具执行与审批前置信号
- 当前仍是无状态 `resume`，由前端回传 `message_history_json`
- 本阶段目标已经完成
- 后续如果要升级为平台级审批能力，还需要审批单持久化、状态管理和更完整的平台级事件协议

验收标准：

- 高风险工具可中断等待审批
- 通过 `resume` 接口可继续执行
- 可以输出流式事件给前端

## Phase 7 - Observability / Guardrails / Tests

- [ ] 基于 hooks 增加请求、模型、工具调用链日志
- [ ] 记录 token、耗时、tool 调用统计
- [ ] 增加输入输出保护与敏感信息脱敏
- [ ] 使用 `TestModel` 建立大部分单元测试
- [ ] 使用 `FunctionModel` 为复杂工具路径建测试
- [ ] 全局禁止测试时真实模型请求
- [ ] 增加 skills / MCP / approvals 相关集成测试

验收标准：

- AI 栈具备基础可观测性
- 测试环境不依赖真实 LLM
- 关键基建路径有自动化测试覆盖

## Phase 8 - 业务 Agent 落地

- [ ] 按业务域新增实际 agent
- [ ] 将现有服务能力逐步封装为业务 toolsets
- [ ] 建立 agent/version/skill 变更流程
- [ ] 评估是否引入多 Agent 协作或 graph 工作流

验收标准：

- 至少一个真实业务 Agent 落地
- 技术骨架可支撑业务扩展而不返工

---

## 首期实现顺序建议

当前已经完成：

- `Phase 1`
- `Phase 2`
- `Phase 3`
- `Phase 4`
- `Phase 5`
- `Phase 6` 的最小 approval / streaming 闭环
- DB 配置控制面主链路

后续建议按这个顺序继续推进：

1. 服务端 Approval Store
2. History Manager
3. `Phase 7` Observability / Guardrails / Tests
4. `Phase 8` 业务 Agent 落地

原因：

- 模型、Agent、MCP 已经进入数据库控制面，下一步应先把错误语义和审批协议稳定下来
- Approval Store 和 History Manager 会影响 API 协议，应在业务 Agent 大规模接入前完成
- 可观测性和 guardrails 是上线治理的前置条件

---

## 首批建议实现的 MVP 内容

当前已完成：

- 一个 `chat-agent`
- 一个 `AgentRegistry`
- 一个 `AgentManager`
- 一个 `Runner`
- `POST /api/v1/agents/chat`
- `POST /api/v1/agents/chat/resume`
- `GET /api/v1/agents`
- 一组按能力域拆分的 builtin `FunctionToolset`
- 四个 builtin 只读工具
- builtin toolsets 组合挂载
- builtin audit wrapper toolset
- metadata approval wrapper toolset
- toolset conventions / metadata helper
- builtin tool metadata 标注
- 最小 tool audit 记录
- 最小 tool execution audit
- metadata 驱动的 approval policy 入口
- `DeferredToolRequests` / `DeferredToolResults` 审批续跑协议
- approval 端到端测试
- OpenAI 兼容 provider 配置接入
- 基础测试
- README 调用链文档
- README approval 闭环说明文档
- MCP 配置解析与请求级动态装配
- MCP manager 生命周期管理
- MCP 相关测试

下一步建议补齐：

- 更细粒度的工具执行审计（耗时 / 入参脱敏 / 异常分类）
- 更完整的 streaming 事件（thinking / request boundary / progress）
- 一个示例 skill

不要首轮就做：

- 多 Agent 编排
- 自动摘要压缩
- 复杂审批 UI
- 大规模业务工具集
- 过度抽象的 DSL

---

## 风险与控制点

### 风险 1：把 Agent 逻辑写进 endpoint

控制：

- endpoint 只调 `chat_service -> runner`

### 风险 2：工具函数直接依赖全局状态

控制：

- 统一走 `AgentDeps`

### 风险 3：MCP server 接入后资源泄漏

控制：

- 集中在 `MCPManager` 管理生命周期

### 风险 4：skills 越做越像硬编码 prompt 拼接

控制：

- 使用 manifest + registry + resolver，而不是直接字符串拼接

### 风险 5：测试依赖真实大模型

控制：

- 默认 `TestModel`
- 复杂路径用 `FunctionModel`

### 风险 6：高风险工具缺少审批与审计

控制：

- 从第二阶段起就预留 `approval` 和 `audit`

---

## 与 PydanticAI 对齐的实现要点

后续实现时，优先围绕这些能力落地：

- `deps_type + RunContext` 作为统一依赖注入方式
- `tools` 与 `toolsets` 作为工具能力组织方式
- `capabilities` 作为 Skills / 审计 / guardrails / hooks 的推荐承载方式
- `Hooks` 用于日志、观测、审计
- `history_processors` 用于后续上下文裁剪与摘要
- `ApprovalRequiredToolset` / `requires_approval` 用于高风险工具
- `Agent.override`、`TestModel`、`FunctionModel` 用于测试
- `DeferredToolRequests` / `DeferredToolResults` 用于审批续跑或外部工具回填

说明：

- 以上为当前官方文档能力方向
- `Skills` 在 `PydanticAI` 生态里更适合作为 capability / 外挂能力处理
- 本项目首期建议先定义自有 `SkillManifest + SkillRegistry`，后续再决定是否适配第三方 `pydantic-ai-skills`

---

## 下一步

下一步建议优先进入 `Phase 5` 与 `Phase 7`：

- 开始定义 `SkillManifest / SkillLoader / SkillRegistry`
- 在 Skills 进入主链路后，再继续细化观测、历史策略和更完整的 streaming 协议
