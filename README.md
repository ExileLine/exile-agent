# exile-agent

`exile-agent` 是一个基于 `FastAPI` 的服务端项目，当前已完成一套基于 `PydanticAI` 的最小可运行 AI 基建设施。现阶段重点覆盖运行时装配、Agent 注册与执行、工具治理、审批闭环、基础历史会话与流式响应能力。

- `Phase 1` AI 最小运行骨架
- `Phase 2` Toolsets 与工具治理基础能力
- `Phase 3` 运行时增强（history / resume / response metadata）
- `Phase 4` MCP 基础接入
- `Phase 5` Skills 基础设施
- `Phase 6` 中 approval 相关的最小闭环

本文档面向开发人员，重点说明当前项目的：

- 架构边界
- 运行时初始化方式
- `/api/v1/agents/chat`、`/chat/stream`、`/chat/resume` 的调用链
- Agent、Toolset、Approval、History 的当前实现方式
- 后续演进方向

## 当前能力

当前版本已经具备以下 AI 能力：

- 应用启动时初始化 AI runtime
- 注册最小 `chat-agent`
- 支持通过 `/api/v1/agents/chat` 触发一次 Agent 调用
- 支持通过 `/api/v1/agents/chat/stream` 进行 SSE 流式输出
- 支持通过 `/api/v1/agents/chat/resume` 继续审批中断的 run
- 支持基于 `session_id` 的基础会话历史恢复
- `/chat` / `/chat/resume` / `/chat/stream` 已具备统一基础 run metadata
- 支持 `deps_type + RunContext`
- 支持通过 `FunctionToolset` 装配基础工具
- 已提供 4 个 builtin 只读工具
- 已为 builtin tools 增加稳定 metadata
- 已接入最小 tool audit 记录
- 已接入 wrapper/audit toolset 与最小 tool execution audit
- 已接入 metadata 驱动的 approval policy wrapper
- 已接入 MCP 配置解析与 `MCPManager`
- 支持在 `/chat`、`/chat/stream`、`/chat/resume` 请求中通过 `mcp_servers` 动态挂载 MCP toolsets
- 支持基于 `route_keywords` 的自动 MCP 路由，未显式传 `mcp_servers` 时可按消息内容自动装配
- MCP toolsets 已接入现有 approval / audit 治理包装
- 已接入 filesystem `SkillLoader`、`SkillRegistry`、`SkillResolver`
- 支持通过 `skill_ids` / `skill_tags` 做请求级 skill 选择
- 支持基于 skill `route_keywords` 的自动命中与 `SKILL.md` 渐进式加载
- 支持 skill 依赖的 toolsets / MCP 自动挂载
- 支持通过 `/api/v1/agents/skills` 查看当前注册的 skills
- `/chat` / `/chat/resume` / `/chat/stream` 响应会在 `meta.skills` 回显本轮解析出的 skills
- `/chat/stream` 已支持 `start` / `delta` / `tool_call` / `tool_result` / `approval_pending` / `approval_required` / `done` / `error`
- 支持真实模型和 `TestModel`

当前尚未覆盖或尚未完整落地的能力包括：

- 真实第三方 MCP 的生产级认证、限流、稳定性治理与业务级工具落地
- 更完整的平台级流式协议（如 thinking / request boundary / richer progress events）
- 历史摘要压缩 / 裁剪
- 更完整的 `toolsets` 包装、审批治理与基于 hooks 的细粒度审计

---

## 架构概览

当前 `/api/v1/agents/chat` 的主调用链如下：

```text
FastAPI app
  -> lifespan.startup_event()
  -> init_ai_runtime()
  -> app.state 挂载 ai_runner / ai_agent_manager / ai_mcp_manager

POST /api/v1/agents/chat
  -> agent endpoint
  -> _build_chat_service(request)
  -> ChatService.chat(...)
  -> AgentRunner.run_chat(...)
  -> AgentManager.get_agent(...)
  -> SkillResolver.resolve(...)        # 可选，请求显式传 skill_ids/skill_tags 或命中 skill 路由时触发
  -> MCPManager.build_toolsets(...)  # 可选，请求显式传 mcp_servers 或命中自动路由时触发
  -> build_chat_agent(...)
  -> agent.run(message, deps=deps, instructions=..., toolsets=...)
  -> 返回 AgentChatResponse
  -> api_response(...)
```

按职责可概括为：

1. 应用启动时，`FastAPI` 会先把 AI 运行时对象准备好。
2. 这些对象会被挂到 `app.state` 上。
3. 请求来了以后，endpoint 不自己创建 Agent，也不自己直接调模型。
4. endpoint 只是从 `app.state` 里取出已经准备好的 `ai_runner` 和 `ai_agent_manager`，组装成 `ChatService`。
5. `ChatService.chat()` 再继续调用 `AgentRunner.run_chat()`。
6. `AgentRunner` 是“真正调大模型的运行入口”，负责：
   - 确定要用哪个 Agent
   - 确定要用哪个模型
   - 构造 `AgentDeps`
   - 解析本轮 skills，并构造运行时 instructions
   - 按请求解析额外的 MCP toolsets
   - 在执行前记录当前 run 可见的工具集合
   - 调用 `agent.run(...)`
   - 把结果整理成统一响应结构
7. `AgentManager` 负责“拿 Agent”，如果缓存里没有，就调用 `build_chat_agent()` 构建。
8. `build_chat_agent()` 负责定义这个 Agent 的模型、输出类型、提示词和 toolsets。
9. 最后 `pydantic_ai.Agent.run(...)` 才真正触发模型调用或测试模型执行。

---

## 关键目录

```text
app/
  main.py                       # FastAPI 应用创建
  core/
    lifespan.py                 # 应用启动/关闭，挂载 AI runtime
    middleware.py               # 注入 request_id
  api/
    router.py                   # /api
    v1/router.py                # /api/v1
    v1/endpoints/agent.py       # /api/v1/agents 与 /api/v1/agents/chat
  ai/
    config.py                   # AISettings
    deps.py                     # RequestContext / AgentDeps
    skills/
      loader.py                 # filesystem skill loader
      models.py                 # SkillManifest
      registry.py               # SkillRegistry
      resolver.py               # SkillResolver
      catalog/                  # 项目内置 skill catalog
    mcp/
      config.py                 # MCP 配置解析
      manager.py                # MCP server 生命周期与请求级装配
    toolsets/
      audit.py                  # wrapper/audit toolset
      builtin.py                # builtin toolsets（time / request / runtime）
      conventions.py            # toolset 本地注册规范与校验
      metadata.py               # tool / toolset metadata helper
    runtime/__init__.py         # init_ai_runtime / shutdown_ai_runtime
    runtime/registry.py         # AgentRegistry
    runtime/manager.py          # AgentManager
    runtime/runner.py           # AgentRunner
    agents/__init__.py          # 默认 agent 注册入口
    agents/chat_agent.py        # build_chat_agent
    services/chat_service.py    # ChatService
```

建议按以下顺序阅读源码：

1. [app/main.py](./app/main.py)
2. [app/core/lifespan.py](./app/core/lifespan.py)
3. [app/ai/runtime/__init__.py](./app/ai/runtime/__init__.py)
4. [app/api/v1/endpoints/agent.py](./app/api/v1/endpoints/agent.py)
5. [app/ai/services/chat_service.py](./app/ai/services/chat_service.py)
6. [app/ai/runtime/runner.py](./app/ai/runtime/runner.py)
7. [app/ai/toolsets/builtin.py](./app/ai/toolsets/builtin.py)
8. [app/ai/runtime/manager.py](./app/ai/runtime/manager.py)
9. [app/ai/agents/chat_agent.py](./app/ai/agents/chat_agent.py)

---

## 一、应用启动阶段：`init_ai_runtime()` 的调用入口

这一段不是处理请求，而是在应用启动时把后续请求需要的 AI 对象全部准备好。

### Step 1. FastAPI 创建应用

入口在 [app/main.py](./app/main.py)。

`create_app()` 里最关键的代码是：

```python
app = FastAPI(
    ...,
    lifespan=lifespan,
)
```

其作用为：

- 这个项目不是靠 `@app.on_event("startup")` 做初始化
- 而是把启动和关闭逻辑统一交给 `lifespan`

所以后续 AI runtime 的初始化，不会发生在某个 endpoint 内部，也不会发生在第一次调用 `/api/v1/agents/chat` 时，而是在应用一启动时就执行。

### Step 2. FastAPI 进入 `lifespan`

入口在 [app/core/lifespan.py](./app/core/lifespan.py)。

当前 `lifespan(app)` 是这样写的：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_event(app)
    try:
        yield
    finally:
        await shutdown_event(app)
```

其执行语义为：

- `yield` 之前：应用启动阶段
- `yield` 之后：应用关闭阶段

因此：

- `startup_event(app)` 负责初始化资源
- `shutdown_event(app)` 负责回收资源

### Step 3. `startup_event(app)` 初始化基础资源和 AI runtime

当前顺序是：

```python
await _init_db()
await _init_redis()
await init_ai_runtime(app, project_config)
```

该初始化顺序体现出两个设计点：

#### 含义 1：AI runtime 依赖于更底层的基础设施

这表明当前 AI 层默认允许后续 Agent 访问以下底层资源：

- 数据库 session factory
- Redis 连接池
- HTTP 客户端

虽然 `Phase 1` 还没真正大量使用 DB/Redis，但运行时依赖注入结构已经留好了位置。

#### 含义 2：AI runtime 是“单次应用启动初始化”，不是“每个请求重复创建”

这是当前架构很关键的一点。

如果每次请求都新建：

- registry
- manager
- runner
- http_client

那请求层会很重，而且生命周期不好管。

现在的做法是：

- 启动时创建一次
- 请求时复用

---

## 二、`init_ai_runtime(app, project_config)` 具体做了什么

入口在 [app/ai/runtime/__init__.py](./app/ai/runtime/__init__.py)。

这是当前 AI 初始化链路的真正核心。

它可以视为：

- “把 AI 运行所需的对象装配出来”
- “再统一挂到 `app.state` 上”

### Step 1. 先把主配置转成 `AISettings`

代码：

```python
settings = AISettings.from_config(project_config)
```

作用：

- `project_config` 是全项目总配置
- `AISettings` 是 AI 子系统自己的配置对象

这样拆开的好处是：

- AI 层只关心自己需要的配置
- 不需要每一层都依赖整个 `BaseConfig`

当前 `AISettings` 里主要有：

- `enabled`
- `default_agent`
- `default_model`
- `max_retries`
- `http_timeout_seconds`
- `enable_mcp`
- `mcp_servers_json`
- `openai_api_key`
- `openai_base_url`

### Step 2. 创建 `AgentRegistry`

代码：

```python
registry = AgentRegistry()
```

`AgentRegistry` 的职责可以概括为：

- “Agent 注册表”
- “系统中有哪些 Agent，先在这里登记”

它内部存的是：

- `agent_id`
- `manifest`
- `builder`

需要特别注意的是：

#### `AgentRegistry` 里不是直接存一个已经跑起来的 Agent 实例

它存的是：

- 一份描述信息 `manifest`
- 一个构造函数 `builder`

这样设计的原因是：

因为后面真正用 Agent 时，还要结合：

- 当前模型名
- 当前运行配置
- 当前 provider 配置

所以更合理的方式是：

- 先注册“如何构造”
- 真正需要时再构造

### Step 3. 注册默认 Agent

代码：

```python
register_default_agents(registry, settings)
```

入口在 [app/ai/agents/__init__.py](./app/ai/agents/__init__.py)。

当前做的事情很简单：

- 注册一个 `chat-agent`
- 它的构造函数是 `build_chat_agent`

可将其视为：

- `registry` 现在知道系统里有一个叫 `chat-agent` 的 Agent
- 但这时候还没有真正开始调用模型

### Step 4. 创建 `AgentManager`

代码：

```python
manager = AgentManager(registry=registry, settings=settings)
```

`AgentManager` 的角色不是执行模型，而是“管理 Agent 的获取”。

它当前负责三件事：

1. 列出系统里的 Agent
2. 根据 `agent_id` 解析真正要用的模型
3. 根据 `(agent_id, model_name)` 缓存 Agent 实例

可以将两者关系概括为：

- `AgentRegistry` 是“登记簿”
- `AgentManager` 是“管理员”

后面真正请求来了，不是 endpoint 自己去 registry 里翻，而是统一交给 `AgentManager` 去拿。

### Step 5. 创建共享 `http_client`

代码：

```python
http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
```

当前 `Phase 1` 里，这个对象还没有大量派上用场，但它已经是 `AgentDeps` 的一部分了。

这意味着后续工具层若需要调用：

- 内部 HTTP 服务
- 第三方 API
- MCP 相关 HTTP 资源

都可以走这个共享 client。

### Step 6. 创建 `SkillLoader` / `SkillRegistry` / `SkillResolver`

代码：

```python
skill_loader = SkillLoader(skills_dir=settings.skills_dir)
skill_registry = SkillRegistry(skill_loader.load_manifests())
skill_resolver = SkillResolver(registry=skill_registry, loader=skill_loader)
```

这一层对应 `Phase 5 - Skills`，职责分为三段：

1. `SkillLoader`
   负责从文件系统扫描 `skill.yaml` 与 `SKILL.md`
2. `SkillRegistry`
   负责保存当前系统已注册的 skills，并提供按名称查询、按 agent 过滤能力
3. `SkillResolver`
   负责在每次 run 前，根据 `agent_id`、用户消息、`skill_ids`、`skill_tags` 解析出本轮应该注入的 skills

当前 skills 设计不是“再造一个 Agent”，而是把 skill 视为一组运行时能力包：

- skill 摘要
- 可选的完整说明正文
- 可选依赖的 toolsets
- 可选依赖的 MCP servers

### Step 7. 创建 `MCPManager`

代码：

```python
mcp_manager = MCPManager(
    enabled=settings.enable_mcp,
    server_configs=load_mcp_server_configs(settings),
    http_client=http_client,
)
```

`MCPManager` 是 `Phase 4` 新增的统一装配层，职责有三类：

1. 解析并持有 MCP server 配置
2. 按请求显式参数或消息自动路由，把 MCP server 解析为当前 run 的附加 toolsets
3. 在应用关闭时统一释放已创建的 MCP server 连接

当前实现里有几个关键约束：

- MCP server 不直接写死在 `chat-agent` 定义中
- `/chat`、`/chat/stream`、`/chat/resume` 都是按请求决定是否挂载 MCP
- MCP toolset 在进入 run 前，会继续复用现有 approval / audit wrapper
- 如果当前环境没有安装 `mcp` SDK，服务仍可启动；只有真正构建 MCP server 时才返回明确错误

### Step 8. 创建 `AgentRunner`

代码：

```python
runner = AgentRunner(
    settings=settings,
    agent_manager=manager,
    http_client=http_client,
    mcp_manager=mcp_manager,
)
```

`AgentRunner` 是当前运行链路中最核心的执行入口之一。

在当前架构里，它是：

- 真正负责“执行一次 Agent 调用”的核心入口

它与 `build_chat_agent()` 的分工如下：

- `build_chat_agent()`：负责定义 Agent 长什么样，默认挂哪些 toolsets
- `AgentRunner.run_chat(...)`：负责执行这次请求，并把 request context / deps / tool exposure 接上

这意味着：

- `AgentManager` 管理“拿哪个 Agent”
- `AgentRunner` 管理“怎么跑这次请求”

当前已经实现了：

- `run_chat(...)`
- `resume(...)`
- `run_chat_stream(...)`

后面还会继续扩展成：

- `run_with_history(...)`
- 更细粒度的 stream event pipeline

### Step 9. 把这些对象挂到 `app.state`

代码：

```python
app.state.ai_settings = settings
app.state.ai_agent_registry = registry
app.state.ai_agent_manager = manager
app.state.ai_http_client = http_client
app.state.ai_mcp_manager = mcp_manager
app.state.ai_runner = runner
```

这一步决定了后续 endpoint 获取 AI 运行时对象的方式。

`app.state` 可以视为：

- FastAPI 应用级别的共享对象容器

放置在 `app.state` 的原因是：

因为这些对象的生命周期和应用一致，而不是和单个请求一致。

所以后面在 `POST /api/v1/agents/chat` 时：

- `_build_chat_service(request)` 就是通过 `request.app.state` 把 `ai_runner` 和 `ai_agent_manager` 取出来

这也是当前代码中 endpoint 获取 `ai_runner` 与 `ai_agent_manager` 的实际装配方式。

---

## 三、MCP 基础接入的当前设计

当前 MCP 接入采用“配置驱动 + 请求级动态装配”的方式，而不是把外部 MCP 能力写死在某个 Agent 定义里。

在当前实现里，可以把 MCP 的职责边界理解为：

- `AI_MCP_SERVERS_JSON`：定义系统级“可用 MCP 能力池”
- `mcp_servers` 或自动路由：决定当前这轮 run “向模型开放哪些 MCP”
- 模型 tool call：在本轮已开放的工具范围内，自主决定是否调用

这意味着，仅配置 MCP server 还不等于模型一定能调用；只有当该 server 被当前 run 装配进 toolsets 后，模型才看得到它。

### 3.1 配置入口

当前新增了两个配置字段：

- `AI_ENABLE_MCP`
- `AI_MCP_SERVERS_JSON`

其中 `AI_MCP_SERVERS_JSON` 当前支持三种输入形态：

1. `{"mcpServers": {"filesystem": {...}}}`
2. `{"filesystem": {...}}`
3. `[{"id": "filesystem", ...}]`

项目内部会先把这些输入统一规整成自己的 `ManagedMCPServerConfig`，再由 `MCPManager` 懒加载成实际 MCP server 对象。

当前实现把 `mcp` SDK 视为可选依赖：

- 未安装 `mcp` 时，项目仍可正常启动
- 只有在真正构建 MCP server 对象时，才会返回明确的配置错误

当前每个 MCP server 还支持补充自动路由字段：

- `auto_route_enabled`
- `route_keywords`

其中：

- `auto_route_enabled=true` 表示该 server 允许被自动路由选中
- `route_keywords` 用于定义命中这类问题时的关键词，例如 `["地图", "路线", "导航"]`

### 3.2 请求级启用方式

`/api/v1/agents/chat`、`/chat/stream`、`/chat/resume` 现在都支持在请求体中传：

```json
{
  "message": "请调用 MCP 工具",
  "mcp_servers": ["filesystem"]
}
```

当请求带了 `mcp_servers` 时，调用链会多出一段：

1. `ChatService` 把 `mcp_servers` 继续传给 `AgentRunner`
2. `AgentRunner._resolve_request_toolsets(...)` 调用 `MCPManager.build_toolsets(...)`
3. `MCPManager` 把这些 server 转成当前 run 的附加 toolsets
4. `AgentRunner` 在 `agent.run(..., toolsets=...)` 或 `agent.run_stream_events(..., toolsets=...)` 时一起挂进去

这样做的结果是：

- `chat-agent` 的静态定义仍然保持精简
- MCP 能力可以按请求启用，不污染所有 run
- 同一套 approval / audit wrapper 可以继续复用到 MCP toolsets

### 3.3 自动 MCP 路由

当前已经支持“显式参数优先，未显式传参时自动路由”的模式：

1. 请求显式传了 `mcp_servers`
   当前 run 直接使用显式值，不做自动推断
2. 请求未显式传 `mcp_servers`
   `MCPManager` 会遍历已注册 server 的 `route_keywords`，根据用户消息做关键词匹配

例如：

```env
AI_MCP_SERVERS_JSON={"mcpServers":{"maps":{"transport":"streamable-http","url":"https://example.com/maps/mcp","tool_prefix":"maps","route_keywords":["地图","路线","导航"]}}}
```

当请求为：

```json
{
  "message": "请帮我查上海到杭州的路线"
}
```

且本轮没有显式传 `mcp_servers` 时，运行时会自动命中 `maps`，并把它挂到当前 run。

### 3.4 返回结果中的体现

当前运行结束后，响应里的 `meta.mcp_servers` 会回显本轮实际装配的 MCP server ID 列表。

这样做的目的有两个：

- 方便前端或调用方理解这次 run 带了哪些外部能力
- 在 `/chat/resume` 需要续跑时，调用方可以明确回传同一组 `mcp_servers`

当本轮是通过自动路由命中 MCP 时，这里回显的仍然是最终实际装配的 server ID，因此前端可以据此判断当前回答是否使用了自动装配的外部能力。

### 3.5 本地 mock MCP server 联调

项目内提供了一个最小可运行的本地 demo MCP server：

- [scripts/mock_mcp_server.py](./scripts/mock_mcp_server.py)

它通过 `stdio` 暴露了 3 个工具：

- `ping`
- `echo_text`
- `get_runtime_snapshot`

推荐的本地配置示例：

```env
AI_ENABLE_MCP=true
AI_MCP_SERVERS_JSON={"mcpServers":{"demo":{"transport":"stdio","command":"/Users/yangyuexiong/Desktop/exile-agent/.venv/bin/python","args":["/Users/yangyuexiong/Desktop/exile-agent/scripts/mock_mcp_server.py"],"tool_prefix":"demo"}}}
```

完成配置并重启服务后，可以直接测试：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/agents/chat' \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: tester' \
  -d '{
    "message": "请不要猜测，必须调用 demo MCP 工具先执行 ping，再简要回答当前 MCP 是否可用。",
    "mcp_servers": ["demo"]
  }'
```

如果链路正常，响应里应满足：

- `data.meta.mcp_servers == ["demo"]`
- 模型返回内容明确引用了 MCP 工具结果

如果要进一步确认模型是否真的执行了工具，而不是只返回最终文本，建议改用 `/api/v1/agents/chat/stream` 观察 `tool_call` / `tool_result` 事件。

### 3.6 Skills 基础设施的当前设计

当前 skills 采用“本地目录 + manifest + 渐进式注入”的方式。

#### 目录结构

每个 skill 目录至少包含：

- `skill.yaml`
- `SKILL.md`

其中：

- `skill.yaml` 描述 skill 的元数据、依赖与路由策略
- `SKILL.md` 描述面向模型的完整执行说明

当前 `skill.yaml` 主要支持这些字段：

- `name`
- `title`
- `description`
- `tags`
- `enabled`
- `priority`
- `load_strategy`
- `allowed_agents`
- `required_toolsets`
- `required_mcp_servers`
- `instruction_files`
- `route_keywords`
- `summary`

#### 运行时职责拆分

skills 主链路由三层完成：

1. `SkillLoader`
   扫描 `AI_SKILLS_DIR`，读取 `skill.yaml` 与 `SKILL.md`
2. `SkillRegistry`
   保存当前已注册 skills，提供查询与 agent 过滤
3. `SkillResolver`
   在每次 run 前解析：
   - 哪些 skills 被显式请求
   - 哪些 skills 被 `skill_tags` 命中
   - 哪些 skills 被消息内容中的 `route_keywords` 命中

#### 注入策略

当前实现遵循“摘要优先、正文按需展开”的原则：

- 先注入 skill summary
- 当 `load_strategy=full_on_match` 且当前请求真正命中 skill 时，再继续加载 `SKILL.md`
- skills 依赖的 `required_toolsets` / `required_mcp_servers` 会一起挂到当前 run

因此，skills 的职责边界可以概括为：

- `AI_SKILLS_DIR`：定义系统可发现的 skill 能力池
- `skill_ids` / `skill_tags` / 消息命中：决定本轮 run 采用哪些 skills
- `meta.skills`：回显本轮最终解析并注入的 skills

#### 当前 API 入口

当前有两类 skill 相关入口：

1. `GET /api/v1/agents/skills`
   查看当前注册的 skills
2. `POST /api/v1/agents/chat`
   可通过请求体传入：

```json
{
  "message": "请帮我检查当前运行时健康状态",
  "skill_tags": ["ops"]
}
```

当命中 `ops-observer` 这类 skill 时：

- `AgentRunner` 会把 skill 摘要或正文作为运行时 instructions 注入
- 同时把 skill 依赖的 toolsets / MCP 一并挂到当前 run
- 最终响应中的 `meta.skills` 会回显本轮命中的 skill 名称

---

## 三、请求阶段：`POST /api/v1/agents/chat` 的调用链

以下说明一次实际请求从进入系统到返回响应的调用路径。

---

## 1. 请求先经过中间件

入口在 [app/core/middleware.py](./app/core/middleware.py)。

当前最关键的一步是注入 `request_id`：

```python
request_id = headers.get("x-request-id") or headers.get("x-log-uuid") or shortuuid.uuid()
scope.setdefault("state", {})["request_id"] = request_id
```

其作用为：

- 如果上游没有传 `x-request-id`
- 系统就自己生成一个 `request_id`
- 然后放到 `request.state` 对应的底层 `scope["state"]` 里

后面 endpoint 可以这样取：

```python
getattr(request.state, "request_id", None)
```

这样设计的直接收益是：

- 一次请求从进入到返回，都能带着同一个 `request_id`
- 后面日志、Agent 返回值、错误处理都能串起来

---

## 2. 路由命中 `/api/v1/agents/chat`

路由链路如下：

1. [app/api/router.py](./app/api/router.py)  
   定义 `/api`
2. [app/api/v1/router.py](./app/api/v1/router.py)  
   定义 `/v1`
3. [app/api/v1/endpoints/agent.py](./app/api/v1/endpoints/agent.py)  
   定义 `/agents/chat`

合在一起就是：

- `POST /api/v1/agents/chat`

---

## 3. 进入 endpoint：`chat_with_agent(...)`

入口在 [app/api/v1/endpoints/agent.py](./app/api/v1/endpoints/agent.py)。

当前 endpoint 做的事情，可以概括成：

1. 从 `app.state` 拿运行时对象
2. 组装请求级上下文
3. 调用 service 层
4. 把异常转成统一 HTTP 响应

它**不做**这些事：

- 不自己构造 Agent
- 不自己决定模型 provider
- 不自己调用 `agent.run(...)`
- 不自己拼 prompt

这就是当前分层的价值。

### 3.1 `_build_chat_service(request)` 的职责

代码：

```python
service = _build_chat_service(request)
```

`_build_chat_service(request)` 的内部逻辑是：

```python
runner = getattr(request.app.state, "ai_runner", None)
agent_manager = getattr(request.app.state, "ai_agent_manager", None)
return ChatService(runner=runner, agent_manager=agent_manager)
```

其职责如下：

- `POST /api/v1/agents/chat` 被调用时
- endpoint 先通过 `request.app.state` 取出应用启动阶段放进去的 `ai_runner` 和 `ai_agent_manager`
- 然后用这两个对象实例化 `ChatService`

这两个对象分别是什么：

- `ai_runner` 是 `AgentRunner`
  - 它是当前“执行一次大模型调用”的核心运行入口
- `ai_agent_manager` 是 `AgentManager`
  - 它负责根据 `agent_id` 和 `model_name` 拿到正确的 Agent 实例

因此，`_build_chat_service(...)` 的意义并不是“简单实例化一个 service”，而是：

- 把应用级 AI runtime 对象，桥接给当前这次请求使用

如果这里拿不到 `ai_runner` 或 `ai_agent_manager`，通常表示：

- 应用启动时 `init_ai_runtime()` 没有正确执行
- 或者 app.state 没准备好

此时会返回：

- `AI runtime 未初始化`

### 3.2 构建 `RequestContext`

代码：

```python
request_context = RequestContext(
    request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
    user_id=request.headers.get("x-user-id"),
    session_id=payload.session_id,
)
```

这一层的目标，是把“请求级信息”从 `FastAPI Request` 中抽离出来，形成 AI 层可稳定依赖的结构。

为什么不把 `FastAPI Request` 直接传给 Agent 层？

因为那样会导致：

- Agent 层和 Web 框架强绑定
- 工具函数测试困难
- 后面迁移到别的入口时很麻烦

当前实现为：

- Web 层只提取必要字段
- 再放进 `RequestContext`

当前提取的字段有：

- `request_id`
- `user_id`
- `session_id`

该步骤本质上是在构造后续 `AgentDeps.request` 的值。

---

## 4. 进入 `ChatService.chat(...)`

入口在 [app/ai/services/chat_service.py](./app/ai/services/chat_service.py)。

当前它的实现很薄：

```python
return await self.runner.run_chat(
    request_context=request_context,
    message=payload.message,
    agent_id=payload.agent_id,
    session_id=payload.session_id,
    model_name=payload.model,
)
```

这意味着：

- `ChatService.chat()` 本质上只是把 endpoint 的参数继续往下转给 `AgentRunner.run_chat()`

保留这一层的原因是：

因为它是一个稳定的服务边界。

后续如果需要补充：

- 权限判断
- 业务限流
- 审计
- 参数兜底逻辑

这些逻辑比起放在 endpoint，更适合放在 `ChatService`。

因此，尽管当前 `ChatService` 实现较薄，这一层仍然是有意保留的稳定扩展位。

---

## 5. 进入 `AgentRunner.run_chat(...)`

入口在 [app/ai/runtime/runner.py](./app/ai/runtime/runner.py)。

这是当前 AI 链路中最核心的运行入口之一。

可将其理解为：

- “把一次 Web 请求，转成一次 Agent 运行”

它当前做了以下几件核心事情。

### 5.1 检查 AI 是否启用

```python
if not self.settings.enabled:
    raise AIDisabledError("AI 能力已关闭")
```

这是运行前的最外层开关保护。

### 5.2 确定本次请求使用的 Agent

代码：

```python
resolved_agent_id = agent_id or self.settings.default_agent
```

逻辑是：

- 如果请求体里传了 `agent_id`，优先使用请求里的
- 否则使用系统默认 `default_agent`

当前默认值是：

- `chat-agent`

### 5.3 确定本次请求使用的模型

代码：

```python
resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
```

这一步并不是简单读取 `payload.model`，而是交由 `AgentManager` 统一解析。

逻辑是：

- 如果请求里显式传了 `model`
  - 用请求值
- 否则
  - 用该 Agent manifest 的默认模型
- 如果 manifest 没定义
  - 再回退到系统默认模型

模型解析规则因此集中收敛在 manager 层，而不是分散在 endpoint 中。

### 5.4 通过 `AgentManager` 获取 Agent

代码：

```python
agent = self.agent_manager.get_agent(resolved_agent_id, resolved_model)
```

这一句非常关键，因为它把 Agent 获取与复用逻辑统一交给了 `AgentManager`。

此时 `AgentRunner` 不关心：

- registry 内部怎么查
- Agent 是否已经缓存
- 是否需要重新构建

此时 `AgentRunner` 只关心一件事：

- “给我一个这次应该跑的 Agent”

这正是当前分层设计的价值所在。

### 5.5 组装这次运行的 `AgentDeps`

代码：

```python
deps = AgentDeps(
    request=request_context,
    settings=self.settings,
    db_session_factory=AsyncSessionLocal,
    redis=redis_client.redis_pool,
    http_client=self.http_client,
)
```

这一段是现阶段 AI 架构中的关键依赖注入点。

它表达的是：

- 把“这次运行需要的依赖”打成一个对象
- 然后统一传给 `agent.run(..., deps=deps)`

当前放进去的有：

- `request`
  - 当前请求级上下文
- `settings`
  - AI 配置
- `db_session_factory`
  - 数据库 session factory
- `redis`
  - Redis 连接池
- `http_client`
  - 共享异步 HTTP 客户端

这一步的重要性在于：

因为后面所有带上下文的工具，都会通过：

- `ctx.deps`

访问这些资源。

这也是 `deps_type + RunContext` 在当前项目中的具体落点。

### 5.6 真正调用 Agent

代码：

```python
result = await agent.run(message, deps=deps)
```

这是当前链路中真正触发模型执行的一行。

从执行语义上看：

- 前面所有层都在做准备工作
- 到这一层，用户输入才真正交给 `PydanticAI Agent`

此时 `agent.run(...)` 会使用：

- 当前消息 `message`
- 当前运行依赖 `deps`
- 当前 Agent 定义里的 instructions
- 当前 Agent 定义里的工具
- 当前模型配置

最终返回一个 `result`。

### 5.7 把结果整理成项目自己的返回结构

代码：

```python
return AgentChatResponse(
    run_id=shortuuid.uuid(),
    agent_id=resolved_agent_id,
    model=resolved_model,
    message=result.output,
    request_id=request_context.request_id,
    session_id=session_id,
    usage=self._serialize_usage(result),
)
```

这一步不会直接把 `PydanticAI` 的原始结果对象返回给接口，而是转换成项目自己的 schema：

- `AgentChatResponse`

这样设计的收益包括：

- API 响应结构稳定
- 即使后续底层库升级，接口层仍可保持稳定

---

## 6. `AgentManager.get_agent(...)` 的职责

入口在 [app/ai/runtime/manager.py](./app/ai/runtime/manager.py)。

其职责是：

- 根据 `agent_id`
- 根据 `model_name`
- 返回一个可运行的 Agent

它的关键逻辑是：

```python
cache_key = (agent_id, resolved_model)
if cache_key not in self._cache:
    registered = self.registry.get(agent_id)
    self._cache[cache_key] = registered.builder(self.settings, resolved_model)
return self._cache[cache_key]
```

其行为可以概括为：

1. 先看缓存里有没有这个 `(agent_id, model)` 对应的 Agent
2. 如果有，直接返回
3. 如果没有，就去 registry 里找到对应的 builder
4. 调用 builder 真正构建 Agent
5. 缓存起来，后面复用

`AgentManager` 的价值在于：

- 不让 endpoint 直接 new Agent
- 不让 `AgentRunner` 直接知道每个 Agent 怎么构造
- 把“获取 Agent”的逻辑集中管理

### 6.1 “缓存”在这里的具体含义

这里提到的“如果缓存里没有，就调用 builder 构建”，其中“缓存”并不是指：

- Redis
- 数据库
- `app.state`

这里的缓存，指的是 `AgentManager` 实例内部维护的一个内存字典：

```python
self._cache: dict[tuple[str, str], Agent[AgentDeps, str]] = {}
```

这意味着：

- key 是 `(agent_id, model_name)`
- value 是已经构建好的 `PydanticAI Agent` 实例

可将其理解为：

- 在当前应用进程里
- 相同的 `agent_id + model_name`
- 只构建一次 Agent
- 后续重复请求直接复用

### 6.2 这个缓存什么时候创建

缓存对象不是单独创建的，而是在 `AgentManager` 初始化时创建：

```python
class AgentManager:
    def __init__(...):
        ...
        self._cache = {}
```

而 `AgentManager` 本身又是在 `init_ai_runtime(...)` 里创建的：

```python
manager = AgentManager(registry=registry, settings=settings)
```

所以顺序是：

1. 应用启动
2. `init_ai_runtime(...)`
3. 创建 `AgentManager`
4. `AgentManager` 内部创建空的 `_cache`
5. `AgentManager` 被挂到 `app.state.ai_agent_manager`

这意味着：

- 缓存的生命周期跟当前应用进程一致
- 应用启动后存在
- 应用关闭后消失

### 6.3 这个缓存什么时候写入

写入发生在 `get_agent(...)` 里面：

```python
cache_key = (agent_id, resolved_model)
if cache_key not in self._cache:
    registered = self.registry.get(agent_id)
    self._cache[cache_key] = registered.builder(self.settings, resolved_model)
return self._cache[cache_key]
```

这意味着：

1. 先生成 `cache_key = (agent_id, resolved_model)`
2. 查询 `_cache`
3. 如果没有命中
   - 去 `registry` 找到这个 Agent 的 `builder`
   - 调用 `builder(...)`
   - 把返回的 Agent 放进 `_cache`
4. 如果命中
   - 直接返回缓存里的 Agent

因此，缓存写入并不发生在应用启动阶段，而是在**第一次请求到某个 `agent_id + model_name` 组合时**。

### 6.4 一个实际示例

假设第一次请求：

```json
{
  "agent_id": "chat-agent",
  "model": "openai:deepseek-chat"
}
```

调用链走到：

- `ChatService.chat(...)`
- `AgentRunner.run_chat(...)`
- `AgentManager.get_agent("chat-agent", "openai:deepseek-chat")`

此时如果 `_cache` 还是空的，会发生：

1. 生成 `cache_key = ("chat-agent", "openai:deepseek-chat")`
2. 缓存未命中
3. 调用 `build_chat_agent(settings, "openai:deepseek-chat")`
4. 拿到构建好的 Agent
5. 写入 `_cache[("chat-agent", "openai:deepseek-chat")]`
6. 返回这个 Agent

第二次再来同样的组合时，就不会再执行 `build_chat_agent(...)`，而是直接从 `_cache` 里取。

### 6.5 为什么可以缓存 Agent，而不是每次都重新建

因为当前缓存的是“Agent 定义”，不是“请求数据”。

`build_chat_agent(...)` 构建的是这些相对稳定的内容：

- 模型对象
- `deps_type`
- `output_type`
- instructions
- tools

这些内容对于同一个：

- `agent_id`
- `model_name`

通常可以复用。

而真正和本次请求绑定的数据，并没有存进缓存的 Agent，而是每次请求都由 `AgentRunner.run_chat(...)` 重新构造：

```python
deps = AgentDeps(
    request=request_context,
    settings=self.settings,
    db_session_factory=AsyncSessionLocal,
    redis=redis_client.redis_pool,
    http_client=self.http_client,
)
```

也就是说：

- `Agent` 实例是缓存复用的
- `AgentDeps` 是请求级、每次重新创建的

这是当前缓存策略成立的前提。

### 6.6 这个缓存不是分布式缓存

还要特别注意：

这个 `_cache` 只是当前 Python 进程里的内存缓存，不是跨进程共享的。

因此，如果后续采用多 worker 部署，例如：

- 多个 `uvicorn` worker
- `gunicorn + uvicorn workers`

那么每个 worker 都会有自己独立的一份：

- `AgentManager`
- `_cache`

这是预期行为。

---

## 7. `build_chat_agent(...)` 的职责

入口在 [app/ai/agents/chat_agent.py](./app/ai/agents/chat_agent.py)。

这里定义了当前默认 Agent 的构造方式。

可将其理解为：

- “`chat-agent` 的结构定义集中在此处”

### 7.1 先构造模型对象

代码：

```python
model = _build_model(settings, model_name)
```

这一步主要用于兼容两种场景：

#### 场景 1：只是普通字符串模型

如果模型名不是 `openai:` 开头，就直接返回原字符串。

#### 场景 2：OpenAI 兼容 provider

如果模型名是 `openai:` 开头，并且项目配置里提供了：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

那么就显式构造：

- `OpenAIProvider`
- `OpenAIChatModel`

这样做的原因是：

- 项目自己的配置是通过 `BaseSettings` 读取的
- 不是所有下游 SDK 都会自动从项目配置对象里拿值
- 显式构造 provider 更稳定

### 7.2 构造 `PydanticAI Agent`

代码：

```python
agent: Agent[AgentDeps, str] = Agent[AgentDeps, str](
    model=model,
    deps_type=AgentDeps,
    output_type=str,
    name="chat-agent",
    instructions=...,
    retries=settings.max_retries,
    defer_model_check=True,
)
```

这里几个关键参数可按如下方式理解：

- `model=model`
  - 这次 Agent 真正要用的模型
- `deps_type=AgentDeps`
  - 这个 Agent 运行时依赖的类型
- `output_type=str`
  - 当前输出就是字符串
- `instructions=...`
  - 默认系统级行为约束

这一步本质上是在：

- 定义这个 Agent 的“人格 + 模型 + 依赖类型 + 输出类型”

### 7.3 装配 `builtin_toolset`

当前不是直接在 `chat-agent` 上零散注册工具，而是通过 `toolsets=get_builtin_toolsets()` 装配一组 builtin 基础工具集。

这代表当前项目已经从“Agent 内直接挂两个函数工具”，演进到“通过 `FunctionToolset` 组织基础工具”的阶段。

当前这组 builtin toolsets 一共提供了 4 个只读工具：

- `get_current_utc_time`
- `get_request_context`
- `get_runtime_config_summary`
- `check_runtime_resources`

这些工具仍然会被模型看到并作为普通 function tools 调用，但它们的组织方式已经变成“多个小 toolset 按能力域组合挂载”。
当前还给它们补上了 metadata，用于标记工具类别、只读属性和所属 toolset。
另外，当前项目已经把一部分“工具注册规范”落成了代码级约束，而不只是口头约定。

这样设计的直接收益包括：

- 工具定义不再散落在具体 Agent 文件里
- 后续可以继续拆出更多业务 toolsets
- 为后面的 wrapper / approval / audit / MCP / Skills 动态装配预留结构

#### 为什么 `FunctionToolset` 还会有 `instructions`

`FunctionToolset` 不只是“装工具的容器”，它自己也可以附带一段 instructions。

这段 instructions 不是给 Python 运行时消费的，而是给模型看的，其作用是告诉模型：

- 这组工具是做什么的
- 什么时候应该优先使用它们
- 不要在不需要的时候乱用它们

当前 builtin toolsets 里的 instructions 大意是：

- 当用户询问请求元数据、当前时间、或者 AI runtime 配置摘要时，优先使用 builtin tools

之所以不将这段内容直接写入 `chat-agent` 的 `instructions`，原因在于：

因为两者职责不同：

- `agent.instructions` 负责定义 Agent 的整体行为、回答风格和全局约束
- `toolset.instructions` 负责定义“这一组工具”的使用策略

这样拆分的收益包括：

- 同一个 toolset 将来可以复用到多个 Agent
- Agent 本身不会堆满工具使用细节
- 后续不同 toolset 可以携带各自不同的 instructions

#### 为什么现在不再只有一个 `builtin-toolset`

当前已经把 builtin 基础工具按能力域拆成了多个 toolset，例如：

- `builtin-time-toolset`
- `builtin-request-toolset`
- `builtin-runtime-toolset`

这样拆的原因是：

- 每个 toolset 的职责更单一
- 后续扩展新 builtin 工具时，不需要一直往一个大 toolset 里堆
- 更符合后面 MCP / Skills / business toolsets 的组合方式

从 Agent 角度看，这些 toolset 仍然会在同一轮 run 中一起暴露给模型，所以外部行为没有变化，只是内部组织结构更清晰了。

#### `id="builtin-toolset"` 或类似稳定 id 的作用

这些 `id` 不是给模型看的，而是给系统和运行时看的，可将其理解为：

- 每个 toolset 的稳定身份标识

当前阶段它最直接的价值是：

- 可读性更强，一眼能知道这是哪个 toolset
- 后续日志、调试、排查时更容易标识来源
- 可以把工具所属 toolset 信息稳定写入 metadata
- 为未来多个 toolset 并存时提供稳定命名

更进一步地说，`FunctionToolset.id` 也是在为后面的能力预留结构，例如：

- wrapper / audit / approval 按 toolset 定向处理
- Skills 依赖某个指定 toolset
- MCP 动态装配后区分 builtin toolset 和外部 toolset
- durable execution 或工作流恢复时稳定识别 toolset

因此：

- `instructions` 是告诉模型“这组工具什么时候用、怎么用”
- `id` 是告诉系统“这组工具是谁”
- `metadata` 是给系统保留一组结构化标签，便于过滤、审计和后续治理

#### 当前已经落地的工具注册规范

当前项目里，各个 builtin toolset 不是直接裸用 `FunctionToolset(...)`，而是通过一层本地约定封装创建：

- `create_function_toolset(...)`

这层封装当前默认开启了两项约束：

- `strict=True`
- `require_parameter_descriptions=True`

对应含义可概括为：

- `strict=True`：要求 function tool 的 JSON schema 更严格，尤其是对 OpenAI 兼容模型更友好
- `require_parameter_descriptions=True`：后续只要工具带业务参数，就要求参数必须写清描述，避免 schema 可运行但语义不清

在这个基础上，toolset 构建完成后还会执行一层本地校验：

- `validate_toolset_conventions(toolset)`

当前这层校验至少会检查：

- tool name 必须是小写 snake_case
- tool description 不能为空
- tool description 必须以句号结尾
- tool 必须启用 `strict=True`
- tool 必须启用 `require_parameter_descriptions=True`
- 如果 metadata 标记了 `readonly=True`，工具名必须以只读前缀开头

当前只读前缀约定是：

- `get_`
- `list_`
- `check_`
- `search_`

例如当前 builtin tools：

- `get_current_utc_time`
- `get_request_context`
- `get_runtime_config_summary`
- `check_runtime_resources`

都符合这套命名规则。

这意味着后续如果有人新增工具时写成：

- `GetUserInfo`
- `fetch_status`
- 没有 description
- description 没有写完整句子

那么在 toolset 构建阶段就会尽早失败，而不是等到线上调用时才暴露出“工具描述不稳定”“schema 不统一”“只读工具命名混乱”这类问题。

因此，在当前阶段，这套约束可以概括为：

- metadata 规范：解决“工具是什么”
- naming / description / schema 规范：解决“工具怎么被稳定地定义出来”
- audit 记录：解决“工具在本次运行里是怎么暴露出去的”

#### `FunctionToolset` 中 `metadata` 入参的规范是什么

先说结论：当前 `pydantic_ai` 对 `FunctionToolset(metadata=...)` 没有定义一套强制固定 schema。

它在类型上就是：

```python
metadata: dict[str, Any] | None = None
```

也就是说：

- 可以不传
- 传了就是一个字典
- key 一般是字符串
- value 理论上可以是任意对象

虽然类型写的是 `Any`，但项目内不建议随意填充。更稳妥的做法是：

- 只放结构化、可序列化的数据
- 尽量限制为 `str / int / float / bool / list / dict / None`
- 不要放数据库连接、函数对象、class 实例这类运行时对象

这样后续做：

- tool selector 过滤
- 审计记录
- 持久化
- 配置化治理

都会更稳定。

#### 这份 `metadata` 会发给模型吗

不会。

`pydantic_ai` 对它的定位更接近“系统内部标签”，而不是给大模型看的提示信息。

它主要用于：

- filtering
- tool behavior customization
- audit / observability
- 后续审批与治理扩展

因此可以将其理解为：

- `description` / `instructions` 是给模型看的
- `metadata` 是给框架和我们项目自己看的

#### `FunctionToolset.metadata` 和单个 tool 的 `metadata` 是什么关系

这里有一个很关键的规则：

- `FunctionToolset` 上的 `metadata` 会应用到这个 toolset 内的所有工具
- 如果单个工具自己也声明了 `metadata`，两者会合并
- 合并规则是浅合并，不是深合并

当前源码中的合并逻辑可以概括为：

```python
tool.metadata = toolset.metadata | (tool.metadata or {})
```

这表示：

- toolset 级别 metadata 先铺一层默认值
- 单个 tool 自己的同名字段会覆盖 toolset 中的字段

例如：

```python
toolset_metadata = {
    "toolset": {"id": "builtin-toolset", "kind": "builtin"},
    "readonly": True,
}

tool_metadata = {
    "category": "time",
    "readonly": False,
}
```

最终工具上会得到：

```python
{
    "toolset": {"id": "builtin-toolset", "kind": "builtin"},
    "readonly": False,
    "category": "time",
}
```

这里要特别注意：

- 这是浅合并
- 如果同名 key 对应的是嵌套 dict，后者会整体覆盖前者对应 key
- 它不是递归 merge

#### 这个 `metadata` 实际会被谁使用

当前最直接的用途有两个。

第一个是 `ToolAuditService`。
它会记录本次 run 暴露给模型的工具集合，以及每个工具带了哪些 metadata 标签。

第二个是后续的 tool 过滤与能力治理。
`pydantic_ai` 的 `ToolSelector` 支持直接用字典按 metadata 做匹配，而且是“深包含”语义。

例如可以表达成：

```python
selector = {
    "toolset": {"kind": "builtin"},
    "readonly": True,
}
```

它的意思不是“metadata 必须完全相等”，而是：

- 工具 metadata 至少要包含这些键值
- 可以额外带更多字段

所以 metadata 设计得是否稳定，直接决定了后面：

- wrapper toolset 是否易于实现
- approval / audit 是否易于治理
- MCP / Skills / builtin tools 能不能统一治理

#### 当前项目里建议采用什么规范

虽然框架没有强制 schema，但项目内最好尽早统一约定。

当前阶段建议把 metadata 规范成下面这类结构：

```python
{
    "toolset": {
        "id": "builtin-time-toolset",
        "kind": "builtin",
        "owner": "platform",
    },
    "category": "time",
    "readonly": True,
    "risk": "low",
    "approval_required": False,
    "tags": ["system", "debug"],
}
```

字段建议可以这样理解：

- `toolset.id`：这个工具属于哪个稳定 toolset
- `toolset.kind`：能力来源类型，例如 `builtin / business / mcp / skill`
- `toolset.owner`：这组工具归哪个模块或团队维护
- `category`：工具业务类别，例如 `time / request / config / runtime`
- `readonly`：是否只读
- `risk`：风险等级
- `approval_required`：是否需要审批
- `tags`：补充型标签

#### 当前项目已经在用的 metadata 约定

当前 [`app/ai/toolsets/builtin.py`](./app/ai/toolsets/builtin.py) 里已经落了一个轻量版本：

```python
metadata={
    "toolset": {
        "id": "builtin-time-toolset",
        "kind": "builtin",
        "owner": "platform",
    }
}
```

而每个具体工具再补自己那层标签，例如：

```python
metadata={
    "category": "time",
    "readonly": True,
    "risk": "low",
    "approval_required": False,
}
```

这说明当前项目已经开始把 metadata 作为“治理标签”使用，而不只是附带备注信息。

后续进入：

- MCP toolset
- skill resolved toolset
- approval wrapper
- tool execution audit

这些阶段时，metadata 基本会成为统一治理的核心连接点之一。

#### `ToolAuditService` 的作用和目的是什么

当前的 `ToolAuditService` 不是业务功能，而是一层最小的工具审计与调试基础设施。

其核心作用包括：

- 记录“这次 Agent 运行把哪些工具暴露给了模型”
- 同时记录这些工具带了什么 metadata
- 记录真实发生的 tool execution 事件

也就是说，它当前更准确记录的是：

- tool exposure
- 最小 tool execution audit

但它还不是完整意义上的：

- 完整 execution telemetry

当前一条审计记录里主要包含：

- `agent_id`
- `request_id`
- `tool_names`
- `tool_metadata`

而当前一条执行记录里主要包含：

- `agent_id`
- `request_id`
- `tool_name`
- `tool_call_id`
- `status`
- `tool_args`
- `tool_metadata`
- `result / error`

这意味着它现在回答的问题是：

- 这次 run 属于哪个 Agent
- 对应哪个请求
- 本次运行前模型能看到哪些工具
- 这些工具各自携带了哪些治理标签

它当前还没有做这些事：

- 不记录执行耗时
- 不做入参脱敏
- 不做异常分类
- 不做持久化存储

因此，当前版本可以视为：

- 面向当前阶段的最小 observability 基础
- 已经具备最小 wrapper / audit toolset 能力
- 但仍然只是后续更细粒度审计的前置地基

#### 为什么现在就要加 `ToolAuditService`

因为项目已经进入 `Phase 2`，工具层开始从“能调用”进入“可治理”。

一旦开始做：

- `FunctionToolset`
- tool metadata
- 多 toolset 组合
- 后续 MCP / Skills 动态装配

就一定会遇到这些排查问题：

- 这次 run 为什么能看到某个工具
- 某个工具具体来自哪个 toolset
- 某个请求暴露出来的工具集合是否符合预期
- 动态装配时是不是把不该给模型的工具也暴露了

如果没有一层基础审计，这些问题后面会很难排查。

#### 它和 tool metadata 是什么关系

两者是配套的。

如果没有 metadata，审计里通常只能看到：

- 工具名

这远远不够。

有了 metadata 之后，审计里才能稳定看到类似这些结构化信息：

- 工具属于哪个 toolset
- 工具属于哪个 category
- 工具是不是只读
- 后面还可以继续加 risk level / approval policy / ownership

两者可配套理解为：

- `ToolAuditService` 负责“记录”
- tool metadata 负责“给记录提供结构化标签”

#### 当前调用链里，它是怎么工作的

当前实现里，`AgentRunner.run_chat(...)` 会在真正执行：

- `agent.run(message, deps=deps)`

之前，先读取当前 Agent 经过 toolset 装配后的工具定义，并把这次 run 可暴露给模型的工具集合记录下来。

按当前代码结构，这一步拿到的不是某一个单独的 builtin toolset，而是 Agent 已经聚合完成后的总 toolset。也就是说：

- `chat-agent` 先挂上 `get_builtin_toolsets()`
- `AgentRunner` 再通过 Agent 聚合后的 toolset 读取当前 run 可见工具
- `ToolAuditService` 记录这些工具名和 metadata

#### 从 `agent.run(...)` 到 `WrapperToolset.call_tool(...)` 的调用链是怎样的

如果继续沿着“真实工具执行”这条线向下看，当前可概括为：

```text
build_chat_agent(...)
  -> toolsets = wrap_toolsets_with_audit(get_builtin_toolsets())
  -> agent.run(message, deps=deps)
  -> PydanticAI 聚合所有 toolsets
  -> model 决定发起某个 tool call
  -> ToolManager 调度工具执行
  -> ToolAuditWrapperToolset.call_tool(...)
  -> wrapped builtin toolset.call_tool(...)
  -> 真实工具函数执行
  -> ToolAuditService 记录 execution event
```

这条链里最关键的点有两个：

第一个点是：

- `AgentRunner` 适合记录 tool exposure

因为它在执行前就能稳定拿到：

- 这次 run 用的是哪个 Agent
- 本轮对模型可见的工具集合是什么

所以它适合回答：

- 这次 run 为什么能看到这些工具

第二个点是：

- `ToolAuditWrapperToolset.call_tool(...)` 适合记录 tool execution

因为真实工具调用最终会经过 toolset 的 `call_tool(...)`。
这意味着只要在 wrapper 里拦截这个方法，就能统一获得：

- tool name
- tool args
- tool_call_id
- tool metadata
- success / error
- result / exception

这也是为什么当前项目没有把“执行审计”写进每个具体工具函数里，而是放在 audit wrapper 里统一处理。

这样设计的收益包括：

- 工具函数本身保持干净，只关心业务逻辑
- 审计逻辑不会散落在每个工具实现中
- 后续 business / MCP / skill toolsets 也可以复用同一层 wrapper
- 以后要扩执行耗时、参数脱敏、异常分类时，只需要继续增强 wrapper

也就是说，它记录的不是“模型猜测会用什么”，而是：

- 当前这次运行实际可见的工具集合

这样设计的收益包括：

- 不依赖 `TestModel` 内部状态
- 对真实模型和测试模型都成立
- 记录点稳定，便于后续扩展

#### 当前工具治理链路中的三层职责

当前项目里，和工具治理最容易混淆的有三层职责。它们分别负责不同的问题：

1. 暴露审计：这轮运行把哪些工具暴露给了模型
2. 执行审计：某个工具是否真的被调用，以及调用结果是什么
3. 审批策略：某个工具调用是否需要先进入 approval 流程

对应到当前代码分别是：

- `app/ai/runtime/runner.py`
  - `_record_tool_exposure(...)`
  - 负责记录 tool exposure
- `app/ai/toolsets/audit.py`
  - `ToolAuditWrapperToolset`
  - 负责记录 tool execution
- `app/ai/toolsets/approval.py`
  - `tool_requires_approval(...)`
  - `MetadataApprovalToolset`
  - 负责 approval policy

也就是说：

- `audit.py` 不是“审批审计”
- `approval.py` 也不是“执行审计”
- 两者分别负责执行层治理与审批层治理

如果用一句更短的话概括当前三层关系：

- `runner.py` 负责记录“模型看到了什么工具”
- `audit.py` 负责记录“工具实际执行了什么”
- `approval.py` 负责决定“工具能不能直接执行”

三者串起来之后，当前工具治理链路可以概括为：

```text
AgentRunner._record_tool_exposure(...)
  -> 记录本轮可见工具集合

MetadataApprovalToolset
  -> 判断这次工具调用是否需要 approval

ToolAuditWrapperToolset.call_tool(...)
  -> 在真实调用前后记录执行事件
```

这样拆分的价值是：

- 暴露审计与执行审计不会混在一起
- approval 规则不会污染具体工具函数
- 后续扩展 MCP / Skills / business toolsets 时，这三层都可以复用

#### 为什么还把它放进 `AgentDeps`

当前虽然主要是 `AgentRunner` 在用它，但它已经被放进了 `AgentDeps`。

这意味着如果后续要扩展成更完整的工具审计，可以继续在以下位置使用它：

- `tool` 函数内部
- wrapper toolset
- hooks
- approval 流程
- external tool 执行回调

所以当前把它放进 `AgentDeps`，本质上是在为后续更完整的审计与治理预留依赖注入入口。

#### `tool_plain`、`tool`、`FunctionToolset` 三者是什么关系

这三个概念不是同一层的东西，它们的关系可以这样理解：

- `tool_plain`：定义一个“纯函数工具”
- `tool`：定义一个“带运行上下文的工具”
- `FunctionToolset`：把一组工具组织成一个可复用的工具集

也就是说：

- `tool_plain` 和 `tool` 解决的是“单个工具怎么定义”
- `FunctionToolset` 解决的是“多个工具怎么组织和装配”

#### 什么时候用 `tool_plain`

`tool_plain` 适合纯函数场景，也就是：

- 不依赖当前请求上下文
- 不需要访问 `ctx.deps`
- 不依赖 DB / Redis / HTTP client
- 只根据传入参数计算结果

比如当前的：

- `get_current_utc_time`

它本质上就是一个不需要运行态依赖的只读工具。

可以概括为：

- `tool_plain` 适合“纯逻辑 / 纯计算 / 纯格式化 / 纯时间读取”这类工具

#### 什么时候用 `tool`

`tool` 适合需要运行上下文的场景，也就是：

- 需要读取 `ctx.deps`
- 需要知道这次请求是谁发起的
- 需要读取数据库、Redis、HTTP client 或运行配置

比如当前的：

- `get_request_context`
- `get_runtime_config_summary`
- `check_runtime_resources`

这些工具都依赖当前运行态，因此应该定义为 `tool`，而不是 `tool_plain`。

可将其理解为：

- `tool` 适合“依赖请求态 / 依赖外部资源 / 依赖运行配置”的工具

#### 为什么在 `tool` / `tool_plain` 之外仍需要 `FunctionToolset`

因为 `tool` 和 `tool_plain` 只解决“定义一个工具”的问题，不解决“项目级组织工具”的问题。

如果所有工具都直接写在某个 Agent 里，短期内可以工作，但后续会出现这些问题：

- 工具定义散落在不同 Agent 文件中
- 同一组工具难以复用到多个 Agent
- 不方便统一加 instructions、metadata、approval、audit
- 不方便以后按能力包接 MCP 或 Skills

`FunctionToolset` 的作用，就是把一组相关工具提升成一个可复用的能力包。

所以当前项目里更推荐的理解方式是：

- 用 `tool_plain` / `tool` 定义具体工具
- 用 `FunctionToolset` 组织这批工具
- 再由 Agent 通过 `toolsets=[...]` 挂载它们

#### `toolsets=[...]` 可以挂多个 toolset 吗

可以，而且这正是 `toolsets` 这个参数的重要用途之一。

当前代码里是：

```python
toolsets=get_builtin_toolsets()
```

但它本质上是一个序列，所以完全可以扩展成：

```python
toolsets=[
    *get_builtin_toolsets(),
    get_ops_toolset(),
    get_business_toolset(),
]
```

也可以混合不同来源的 toolset，例如：

```python
toolsets=[
    *get_builtin_toolsets(),
    get_business_toolset(),
    mcp_toolset,
]
```

这意味着，后续项目完全可以按“能力来源”拆分工具，而不是把所有工具都塞进一个大 toolset 里。

#### 多个 toolset 组合时，模型看到的是什么

从模型视角看，最终它看到的是多个 toolset 合并后的可用工具集合。

也就是说：

- `builtin toolsets` 提供一批基础只读工具
- `business_toolset` 提供一批业务工具
- `mcp_toolset` 提供一批外部能力工具

最后模型会把它们当成同一轮 run 中可用的工具池来使用。

#### 为什么后续通常会出现多个 toolset

因为后续这个项目不可能永远只有一组 builtin tools。

按现在的规划，后面很可能逐步出现：

- `builtin-time-toolset`
- `builtin-request-toolset`
- `builtin-runtime-toolset`
- `ops_toolset`
- `business_toolset`
- `mcp_toolset`
- `skill_resolved_toolset`

这时 `toolsets=[...]` 的意义就体现出来了：

- 每一组工具按能力包独立维护
- Agent 只负责组合需要的 toolsets
- 不需要把所有工具重新散落到 Agent 文件中

#### 多个 toolset 组合时要注意什么

最重要的一点是：

- 工具名不能冲突

例如：

- `builtin_toolset` 里定义了 `get_status`
- `ops_toolset` 里也定义了 `get_status`

这种情况后面会带来冲突风险。

因此，项目进入更复杂阶段后，应尽早建立工具命名规范，例如：

- `runtime_get_status`
- `ops_get_status`
- `customer_get_profile`

这样不同 toolset 的工具来源会更清晰。

#### 静态装配和动态装配的区别

当前 `chat-agent` 里写的是：

```python
toolsets=get_builtin_toolsets()
```

这属于静态装配，也就是：

- 这个 Agent 每次运行都会固定带上这组 toolset

后面更成熟的做法通常是：

- Agent 定义阶段挂一部分固定 toolsets
- Runner 执行阶段再根据请求动态补更多 toolsets

例如：

- 所有请求都带 `builtin toolsets`
- 某些请求额外带业务 toolset
- 某些请求额外带 skill toolset
- 某些请求额外带 MCP toolset

后续这条链通常会从：

```python
toolsets=get_builtin_toolsets()
```

演进成：

```python
toolsets=[
    *get_builtin_toolsets(),
    get_business_toolset(),
    *resolved_skill_toolsets,
    *resolved_mcp_toolsets,
]
```

当前可以将这一步理解为：

- 先把一组基础 builtin toolsets 跑通
- 后续再把它扩展成多个 toolset 的组合装配机制

#### 当前项目里的推荐用法

按照现在这套架构，建议这样选：

- 工具是纯函数：优先 `tool_plain`
- 工具依赖 `ctx.deps`：优先 `tool`
- 一组工具要被统一挂载、复用、治理：放进 `FunctionToolset`

所以在当前阶段：

- `get_current_utc_time` 是 `tool_plain`
- `get_request_context` 是 `tool`
- `builtin_time/request/runtime_toolset` 是 `FunctionToolset`

这个分层正是后面继续做：

- wrapper / audit toolset
- MCP toolset
- skill 依赖 toolset
- 动态装配 toolsets

所需要的基础结构。

下面只重点解释两类最有代表性的工具。

#### 工具 1：`get_current_utc_time`

```python
@agent.tool_plain
def get_current_utc_time() -> str:
    ...
```

这是一个纯函数工具：

- 不依赖运行上下文
- 不访问 `deps`

#### 工具 2：`get_request_context`

```python
@agent.tool
def get_request_context(ctx: RunContext[AgentDeps]) -> dict[str, str | None]:
    ...
```

这是一个带上下文的工具：

- 它通过 `ctx.deps.request` 读取当前请求信息

另外两个工具：

- `get_runtime_config_summary`
- `check_runtime_resources`

它们也是只读工具，主要用于让模型读取当前 AI runtime 概况，而不是在需要运行时信息时靠猜测回答。

这一点非常关键，因为它正好说明：

- `RequestContext` 是在 endpoint 构造的
- `AgentDeps` 是在 runner 构造的
- `RunContext[AgentDeps]` 是在工具执行时被注入进来的

也就是说，请求上下文是这样一路传递下来的：

```text
request.state / headers
  -> RequestContext
  -> AgentDeps.request
  -> RunContext[AgentDeps].deps.request
  -> tool 函数
```

---

## 8. 请求结果是怎么回到接口层的

当 `agent.run(...)` 完成之后，链路会反向返回：

```text
agent.run(...)
  -> AgentRunner.run_chat(...)
  -> ChatService.chat(...)
  -> chat_with_agent(...)
  -> api_response(...)
```

最后 endpoint 里执行的是：

```python
return api_response(data=result.model_dump(mode="json"))
```

因此，对外接口返回的不是底层库对象，而是项目自定义的标准响应格式：

```json
{
  "code": 200,
  "message": "操作成功",
  "data": {
    "run_id": "...",
    "agent_id": "chat-agent",
    "model": "...",
    "message": "...",
    "request_id": "...",
    "session_id": "...",
    "usage": {...}
  }
}
```

---

## 四、调用链摘要

以下以摘要形式再次整理这条调用链，便于快速查阅。

### 启动阶段

1. `FastAPI` 启动时，会先进入 `lifespan`。
2. `lifespan.startup_event()` 会调用 `init_ai_runtime(app, project_config)`。
3. `init_ai_runtime()` 会创建：
   - `AISettings`
   - `AgentRegistry`
   - `AgentManager`
   - `http_client`
   - `AgentRunner`
4. 然后把这些对象挂到 `app.state` 上：
   - `app.state.ai_agent_manager`
   - `app.state.ai_runner`
   - 以及其它 AI 运行时对象

### 请求阶段

1. 当 `POST /api/v1/agents/chat` 被调用时，请求先经过 middleware，middleware 会注入 `request_id`。
2. 请求进入 `chat_with_agent(...)` endpoint。
3. endpoint 调用 `_build_chat_service(request)`。
4. `_build_chat_service(request)` 通过 `request.app.state` 取出：
   - `ai_runner`
   - `ai_agent_manager`
5. 然后用这两个对象实例化 `ChatService`。
6. endpoint 再根据当前请求构造 `RequestContext`。
7. 接着调用 `ChatService.chat(...)`。
8. `ChatService.chat(...)` 再调用 `AgentRunner.run_chat(...)`。
9. `AgentRunner` 会先解析这次请求应使用哪个：
   - `agent_id`
   - `model`
10. 然后 `AgentRunner` 调用 `AgentManager.get_agent(...)` 获取 Agent。
11. 如果缓存里没有对应 Agent，`AgentManager` 会调用 `build_chat_agent(...)` 构造。
12. `build_chat_agent(...)` 会定义：
   - 这个 Agent 用什么模型
   - 它的 instructions 是什么
   - 它有哪些工具
   - 它的 `deps_type` 是什么
13. `AgentRunner` 接着构造 `AgentDeps`，把：
   - `RequestContext`
   - `settings`
   - `db_session_factory`
   - `redis`
   - `http_client`
   封装进去。
14. 然后执行：
   - `await agent.run(message, deps=deps)`
15. 大模型返回结果后，`AgentRunner` 把结果转成 `AgentChatResponse`。
16. 最后 endpoint 再通过 `api_response(...)` 包装成统一接口响应返回给前端。

---

## 五、为什么当前要这样分层

如果把这些逻辑全部堆在 endpoint 里，虽然短期可运行，但后续会非常难以扩展。

当前分层的价值在于：

### endpoint 层

负责：

- HTTP 接入
- 参数解析
- 从 `app.state` 取运行时对象
- 构建请求级上下文
- 错误转 HTTP 响应

### service 层

负责：

- 承接业务规则和服务编排

当前虽然很薄，但这是有意保留的扩展位。

### runner 层

负责：

- 真正执行一次 Agent 调用
- 构造 `AgentDeps`
- 调用 `agent.run(...)`
- 统一返回结构

### manager 层

负责：

- 管理和缓存 Agent

### agent 定义层

负责：

- 定义每个 Agent 的模型、提示词、工具和依赖类型

这样的分层，后面做这些能力时会自然很多：

- `toolsets`
- 历史消息
- MCP
- Skills
- streaming
- approvals

## Approval 审批闭环补充说明

这一节专门整理当前项目里与 approval 相关的几个核心问题：

- 什么时候会触发审批
- `ApprovalRequired` 之后是谁接管
- 逻辑闭环在何处完成
- 为什么接口拆成 `/chat` 与 `/chat/resume`
- 前端应该如何驱动这条链

### 1. 什么情况下会触发 approval

当前项目是基于 tool metadata 做最小审批策略判断的。

判断入口在：

- `app/ai/toolsets/approval.py`
- `tool_requires_approval(...)`

当前规则非常明确：

- `metadata["approval_required"] is True`
- 或 `metadata["risk"] == "high"`

命中任意一条规则时，即判定这次工具调用需要进入审批流程。

例如：

```python
metadata={
    "category": "ops",
    "readonly": False,
    "risk": "high",
    "approval_required": True,
}
```

这种工具一旦被模型调用，就不会直接执行业务逻辑，而是先进入 approval 流程。

### 2. `ApprovalRequired` 之后，谁来处理

这一点最容易被误解：

- `ApprovalRequired` 不是让业务代码到处 `try/except` 的
- 它主要由 PydanticAI 运行时内部接管

当前项目中，`chat-agent` 已经把：

- `DeferredToolRequests`

显式加入了 `output_type`。

这意味着：

- 当工具命中 approval 时
- PydanticAI 不会把这次运行直接当成失败
- 而是会把这轮运行转成一份“待审批结果”

也就是：

- `DeferredToolRequests`

然后再由当前项目自己的 `AgentRunner` 把它整理成统一 API 响应。

所以当前正式链路不是：

```python
try:
    await wrapped_toolset.call_tool(...)
except ApprovalRequired:
    ...
```

而是：

```text
工具命中审批
-> PydanticAI 内部转成 DeferredToolRequests
-> AgentRunner 识别 output 类型
-> API 返回 status="approval_required"
```

注意：

- 直接 `except ApprovalRequired` 这种写法
- 更适合测试或底层 wrapper 验证
- 不是正常业务接口的主要写法

### 3. 逻辑闭环在何处完成

approval 的闭环不是在单一一个函数里完成的，而是分成两段。

第一段：首次 `/chat`

- 用户调用 `/api/v1/agents/chat`
- endpoint 构造 `ChatService`
- `ChatService.chat(...)` 调用 `AgentRunner.run_chat(...)`
- `AgentRunner.run_chat(...)` 内部执行 `agent.run(message, deps=deps)`
- 如果模型调用了高风险工具，PydanticAI 会把本轮结果转成 `DeferredToolRequests`
- `AgentRunner._build_chat_response(...)` 识别到这一点后，返回：
  - `status="approval_required"`
  - `deferred_tool_requests.approvals`
  - `deferred_tool_requests.message_history_json`

第二段：后续 `/chat/resume`

- 前端或审批系统拿到第一次 `/chat` 的返回
- 用户做出批准 / 拒绝决定
- 再调用 `/api/v1/agents/chat/resume`
- endpoint 调 `ChatService.resume(...)`
- 再进入 `AgentRunner.resume_chat(...)`
- `resume_chat(...)` 做两件事：
  - 用 `message_history_json` 还原上一轮运行上下文
  - 用前端传回的审批决定构造 `DeferredToolResults`
- 然后再次调用：
  - `agent.run(message_history=..., deferred_tool_results=..., deps=deps)`

这一刻，闭环才真正完成。

可将其理解为：

```text
/chat 负责“停在审批点”
/chat/resume 负责“从审批点继续跑”
```

### 4. `/chat/resume` 是什么时候调用的

它不是后端自动调用的，也不是模型自己回调的。

它的触发时机是：

- 前端先调用 `/chat`
- 如果响应里 `status == "approval_required"`
- 前端就进入审批交互
- 用户批准或拒绝后
- 前端再显式调用 `/chat/resume`

所以当前前端应当按响应 `status` 做分流：

```text
status == "completed"
-> 直接展示 message

status == "approval_required"
-> 展示待审批工具
-> 收集审批结果
-> 调用 /chat/resume
```

也就是说，`/chat/resume` 不是每次都会调，而是：

- 只有首次 `/chat` 停在审批点时才需要调

### 5. 为什么拆成 `/chat` 与 `/chat/resume`

从技术实现上看，也可以统一放进 `/chat`。

但当前拆开是更工程化的设计，因为这两个动作语义不同。

`/chat` 表示：

- 发起一轮新的用户问题

`/chat/resume` 表示：

- 继续一轮已经中断的 Agent 运行

它们的请求体也完全不同：

- `/chat` 的核心字段是 `message`
- `/chat/resume` 的核心字段是 `message_history_json + approvals`

如果全部放在 `/chat`，后端就需要先判断：

- 当前请求是在“发起新问题”
- 还是“继续上一轮 run”

这样会让：

- schema 更复杂
- endpoint 分支更多
- 文档更难讲清楚
- 后续接 streaming / external tools / retry / approval state 时更混乱

所以当前拆成两个接口，本质上是在明确区分：

- 开始一次 run
- 继续一次 run

### 6. 当前前端应如何对接 approval flow

前端现在可以按下面的最小协议来接：

第一步，调用：

- `POST /api/v1/agents/chat`

如果返回：

```json
{
  "status": "completed",
  "message": "最终回答"
}
```

就直接展示结果。

如果返回：

```json
{
  "status": "approval_required",
  "message": null,
  "deferred_tool_requests": {
    "approvals": [
      {
        "tool_call_id": "call_xxx",
        "tool_name": "delete_demo_resource",
        "args": {},
        "metadata": {
          "risk": "high",
          "approval_required": true
        }
      }
    ],
    "calls": [],
    "message_history_json": "..."
  }
}
```

前端就应该：

- 展示审批确认框
- 让用户选择批准或拒绝
- 保留 `message_history_json`
- 保留每个 `tool_call_id`

第二步，调用：

- `POST /api/v1/agents/chat/resume`

例如：

```json
{
  "agent_id": "chat-agent",
  "message_history_json": "...",
  "approvals": [
    {
      "tool_call_id": "call_xxx",
      "approved": true
    }
  ]
}
```

如果批准：

- PydanticAI 会真正执行工具
- 然后继续让模型生成最终回答

如果拒绝：

- PydanticAI 会把拒绝结果回送给模型
- 再由模型决定如何继续回复用户

### 7. 一句话总结当前 approval 闭环

当前项目里的 approval 闭环可以概括成：

```text
模型请求高风险工具
-> approval wrapper 拦截
-> PydanticAI 输出 DeferredToolRequests
-> /chat 返回 approval_required
-> 前端完成审批
-> /chat/resume 回填 DeferredToolResults
-> Agent 从中断点继续执行
-> 返回最终结果
```

所以当前这套实现已经不只是“预留审批入口”，而是已经具备了：

- 审批判定
- 审批中断
- 审批结果回填
- 续跑完成

这就是当前阶段的最小可用 approval 闭环。

### 8. approval 时序图

如果按角色拆分当前闭环，可将其表示为下面这条时序。

```text
User
  -> Frontend: 输入问题

Frontend
  -> POST /api/v1/agents/chat: message="请删除某资源"

FastAPI endpoint
  -> ChatService.chat(...)
  -> AgentRunner.run_chat(...)
  -> agent.run(message, deps=deps)

Agent / PydanticAI runtime
  -> 模型决定调用高风险工具
  -> approval wrapper 判断 metadata:
     - approval_required=True
     - 或 risk=high
  -> 工具调用不直接执行
  -> 内部转成 DeferredToolRequests

AgentRunner
  -> 识别 result.output 是 DeferredToolRequests
  -> 序列化为:
     - status="approval_required"
     - deferred_tool_requests.approvals
     - deferred_tool_requests.message_history_json

FastAPI endpoint
  -> 返回 /chat 响应

Frontend
  -> 判断 status == "approval_required"
  -> 打开审批确认 UI
  -> 用户点击 批准 / 拒绝

Frontend
  -> POST /api/v1/agents/chat/resume:
     - message_history_json
     - approvals[{tool_call_id, approved}]

FastAPI endpoint
  -> ChatService.resume(...)
  -> AgentRunner.resume_chat(...)
  -> 反序列化 message_history_json
  -> 构造 DeferredToolResults
  -> agent.run(message_history=..., deferred_tool_results=..., deps=deps)

PydanticAI runtime
  -> 根据 tool_call_id 找回上一轮待处理工具调用
  -> 如果 approved=True:
     - 执行真实工具
     - 把 ToolReturn 继续喂给模型
  -> 如果 approved=False:
     - 生成 ToolDenied
     - 把拒绝结果继续喂给模型

AgentRunner
  -> 收到最终 result.output
  -> 组装 status="completed"

FastAPI endpoint
  -> 返回 /chat/resume 响应

Frontend
  -> 展示最终 message
```

也可以进一步概括为：

```text
/chat 负责开始并可能停在审批点
/chat/resume 负责把审批结果送回去并继续跑完
```

### 8.1 approval 时序图对应到哪些代码位置

如需按源码逐层追踪，可参考下面这张“步骤 -> 文件位置”对照表。

#### 首次 `/chat` 请求阶段

1. 进入 HTTP 接口

- 文件：`app/api/v1/endpoints/agent.py`
- 方法：`chat_with_agent(...)`
- 作用：
  - 接收 `POST /api/v1/agents/chat`
  - 构造 `RequestContext`
  - 调用 `ChatService.chat(...)`

2. 进入 service 层

- 文件：`app/ai/services/chat_service.py`
- 方法：`chat(...)`
- 作用：
  - 不直接处理 approval 逻辑
  - 只是把请求转换成 `AgentRunner.run_chat(...)`

3. 进入 runner 层

- 文件：`app/ai/runtime/runner.py`
- 方法：`run_chat(...)`
- 作用：
  - 解析当前使用哪个 agent / model
  - 构造 `AgentDeps`
  - 调用 `agent.run(message, deps=deps)`

4. Agent 定义里已经挂好了 approval wrapper

- 文件：`app/ai/agents/chat_agent.py`
- 方法：`build_chat_agent(...)`
- 关键点：
  - `output_type=[str, DeferredToolRequests]`
  - `toolsets=wrap_toolsets_with_audit(wrap_toolsets_with_metadata_approval(...))`

这一步的意义是：

- 如果工具命中了审批逻辑，PydanticAI 才能输出 `DeferredToolRequests`
- 而不是把整个运行当成普通报错

5. approval 判定发生在 toolset wrapper 中

- 文件：`app/ai/toolsets/approval.py`
- 方法：`tool_requires_approval(...)`
- 作用：
  - 根据 tool metadata 判断这次调用是否需要审批

当前规则是：

- `approval_required=True`
- 或 `risk=="high"`

6. toolset wrapper 把工具调用拦进 approval 流程

- 文件：`app/ai/toolsets/approval.py`
- 类型：`MetadataApprovalToolset`
- 继承自：`ApprovalRequiredToolset`
- 作用：
  - 当某个工具满足审批条件时
  - 不直接执行工具函数
  - 而是进入 PydanticAI 的 deferred approval 流程

7. PydanticAI 内部把 approval 转成 deferred output

- 运行库位置：
  - `.venv/lib/python3.13/site-packages/pydantic_ai/result.py`
  - `.venv/lib/python3.13/site-packages/pydantic_ai/_agent_graph.py`
- 作用：
  - 识别这次工具调用属于 `unapproved`
  - 生成 `DeferredToolRequests`

8. 当前项目把 deferred output 包装成 `/chat` 响应

- 文件：`app/ai/runtime/runner.py`
- 方法：`_build_chat_response(...)`
- 关键分支：
  - 如果 `result.output` 是普通 `str`
    - 返回 `status="completed"`
  - 如果 `result.output` 是 `DeferredToolRequests`
    - 返回 `status="approval_required"`

9. 当前项目把待审批数据序列化给前端

- 文件：`app/ai/runtime/runner.py`
- 方法：`_serialize_deferred_tool_requests(...)`
- 输出内容：
  - `approvals`
  - `calls`
  - `message_history_json`

10. 响应模型定义在 schema 中

- 文件：`app/ai/schemas/chat.py`
- 类型：
  - `AgentChatResponse`
  - `AgentDeferredToolRequestsPayload`
  - `AgentApprovalRequest`

这就是前端第一次拿到 `status="approval_required"` 的来源。

#### 后续 `/chat/resume` 续跑阶段

11. 前端在审批后再次进入 HTTP 接口

- 文件：`app/api/v1/endpoints/agent.py`
- 方法：`resume_agent_chat(...)`
- 作用：
  - 接收 `POST /api/v1/agents/chat/resume`
  - 构造新的 `RequestContext`
  - 调用 `ChatService.resume(...)`

12. 进入 service 层的 resume 入口

- 文件：`app/ai/services/chat_service.py`
- 方法：`resume(...)`
- 作用：
  - 把前端提交的审批结果转交给 `AgentRunner.resume_chat(...)`

13. 进入 runner 的续跑逻辑

- 文件：`app/ai/runtime/runner.py`
- 方法：`resume_chat(...)`
- 关键动作：
  - `ModelMessagesTypeAdapter.validate_json(message_history_json)`
  - `_build_deferred_tool_results(approvals)`
  - `agent.run(message_history=..., deferred_tool_results=..., deps=deps)`

这一步是当前项目里“approval 闭环真正接起来”的核心位置。

14. 审批结果被组装成 `DeferredToolResults`

- 文件：`app/ai/runtime/runner.py`
- 方法：`_build_deferred_tool_results(...)`
- 映射规则：
  - `approved=True` -> `ToolApproved(...)` 或 `True`
  - `approved=False` -> `ToolDenied(...)`

15. PydanticAI 根据 `tool_call_id` 恢复上一轮待处理工具调用

- 运行库位置：
  - `.venv/lib/python3.13/site-packages/pydantic_ai/_agent_graph.py`
- 方法：
  - `_handle_deferred_tool_results(...)`
- 作用：
  - 根据 `tool_call_id` 找回之前停住的工具调用
  - 如果批准，就执行真实工具
  - 如果拒绝，就把拒绝消息回送给模型

16. 当前项目再次统一包装最终响应

- 文件：`app/ai/runtime/runner.py`
- 方法：`_build_chat_response(...)`
- 结果：
  - 如果模型已经产出最终文本
  - 就返回 `status="completed"` 和 `message`

#### 工程化概括

如果按项目分层看，approval 闭环大致是：

```text
endpoint 接协议
-> service 转调用
-> runner 发起 / 恢复 run
-> toolset wrapper 决定是否审批
-> PydanticAI runtime 负责 deferred 中断与恢复
-> runner 再包装成统一 API 响应
```

### 9. approval 相关职责分工

为了避免把这条链看成“某一个函数处理了一切”，可以按职责这样理解：

- `tool_requires_approval(...)`
  负责判断“这次工具调用应不应该进入审批”
- `MetadataApprovalToolset`
  负责把需要审批的工具调用拦截成 approval 流程
- `PydanticAI runtime`
  负责把 `ApprovalRequired` 转成 `DeferredToolRequests`
- `AgentRunner.run_chat(...)`
  负责把 deferred 结果包装成 `/chat` 响应
- 前端或审批系统
  负责收集人工审批结果
- `AgentRunner.resume_chat(...)`
  负责把审批结果回填成 `DeferredToolResults`
- `PydanticAI runtime`
  负责根据 `tool_call_id` 恢复并继续执行

按职责拆分后可更清晰地理解为：

- 审批策略在 toolset 层
- 中断与恢复在 runtime 层
- 交互驱动在前端或上层系统
- endpoint 只是协议入口，不负责审批决策本身

### 10. 当前方案的边界

当前 approval 闭环已经可用，但它仍然是“本阶段最小实现”，主要边界有这些：

- 当前是无状态 resume 协议
  - 由前端携带 `message_history_json`
  - 服务端还没有落审批单或 run 状态表
- 当前审批判断仍是最小规则
  - 只看 tool metadata
  - 还没有叠加用户角色、租户、环境、参数内容
- 当前只接通了 approval 路径
  - external tool calls 的完整回填协议还没继续展开
- 当前已经具备最小 streaming 形态
  - 已支持 `/chat/stream` SSE
  - 但还没有更细粒度的完整事件流

因此，这一阶段的定位应为：

- approval 能力已经打通
- 但还没有进入平台级审批中心、持久化审批单、更细粒度流式事件这些更完整阶段

---

## 六、当前这条链已经验证了什么

目前已经验证通过：

- 应用启动时可以初始化 AI runtime
- `/api/v1/agents/chat` 可以真实触发 AgentRunner
- `/api/v1/agents/chat/stream` 可以输出文本、工具执行与审批前置信号的 SSE 事件流
- `/api/v1/agents/chat/resume` 可以基于 `DeferredToolRequests` 继续执行
- 同一个 `session_id` 的后续 `/chat` 可以读取上一轮 message history
- 三条运行链都能返回统一的基础 run metadata
- `AgentManager` 可以正确获取和缓存 Agent
- `build_chat_agent()` 可以正确构造 Agent
- `chat-agent` 可以通过 `FunctionToolset` 装配 builtin tools
- `AgentDeps` 可以被传入 `agent.run(...)`
- `RunContext[AgentDeps]` 工具可以访问请求上下文
- 可以使用 `TestModel`
- 可以使用真实 OpenAI 兼容 provider
- 高风险工具可以先进入 approval，再由前端确认是否继续执行

---

## 七、当前这条链还没覆盖什么

当前它还是最小闭环，只覆盖：

- 单次 chat 调用
- 基础 SSE 流式输出
- 无状态 approval resume
- 基础 `session_id` 多轮恢复
- 单 Agent
- 少量基础工具

还没覆盖：

- thinking / request boundary / progress 这类更完整的 stream event pipeline
- history 摘要压缩 / 裁剪 / processors
- MCP manager
- Skills resolver

因此，当前最准确的定位是：

- “AI 最小运行链路已经打通”
- 但“AI 平台级能力还没铺开”

---

## 八、`/chat/stream` 当前事件模型

当前 `/api/v1/agents/chat/stream` 已经不只是“文本 token 流”，而是一组面向前端可消费的 SSE 事件：

- `start`
  - 表示本次 stream run 已建立，携带 `agent_id`、`model`、`request_id`、`session_id` 和基础 `meta`
- `delta`
  - 文本增量事件，当前只负责输出最终文本内容片段
- `tool_call`
  - 表示某个函数工具开始执行
  - 载荷里会带 `tool_call_id`、`tool_name`、`args`、`args_valid`、`tool_metadata`
- `tool_result`
  - 表示某个函数工具已经返回
  - 载荷里会带 `tool_call_id`、`tool_name`、`status`、`result`、`tool_metadata`
- `approval_pending`
  - 表示本轮 stream 已经判断出需要人工审批，但为了兼容旧前端，还没有替代 `approval_required`
- `approval_required`
  - 兼容现有前端协议的审批中断事件
  - 载荷是完整的 `AgentChatResponse`，前端可以直接取 `deferred_tool_requests` 后再调 `/chat/resume`
- `done`
  - 本轮运行正常完成，载荷是完整的 `AgentChatResponse`
- `error`
  - 流式运行失败，载荷里会带基础错误信息和 `meta`

当前设计目标包括：

- 保留 `/chat/stream` 现有最小协议，避免前端一次性重做
- 增量补齐工具执行态感知，让前端知道“模型不是只在吐字，也可能正在跑工具”
- 为后续审批 UI、工具进度展示、事件审计打基础

native stream 与 fallback stream 当前的差异是：

- native path 会尽量使用 `PydanticAI` 的 `run_stream_events()`，因此能拿到 `tool_call` / `tool_result`
- fallback path 用于模型本身不支持 streamed request 的情况，这时仍然保底返回 `start` + `done`，审批场景会返回 `approval_pending` + `approval_required`

### `runner.py` 这次 stream 改造的关键点

上面说明的是“前端能收到什么事件”，这里补充 `runner.py` 内部如何产出这些事件。

关键文件是：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py)

这次改造的核心不是普通 `/chat` 和 `/chat/resume`，而是：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L100) 中的 `run_chat_stream()`

#### 改造前后的思路差异

改造前更接近“文本流”模式：

- 调 `agent.run_stream(...)`
- 再用 `stream_text()` 读取文本增量
- 最后用 `get_output()` 拿最终输出

这条链对“只看文本”够用，但它不适合稳定感知：

- 工具什么时候开始调用
- 工具什么时候返回结果
- 本轮是不是已经进入 approval / deferred 分支

所以这次改造后，`run_chat_stream()` 改成优先使用：

- `agent.run_stream_events(...)`

它返回的不是单纯文本，而是一条更底层的运行事件流，里面会包含：

- 文本事件：`PartStartEvent`、`PartDeltaEvent`
- 工具调用事件：`FunctionToolCallEvent`
- 工具结果事件：`FunctionToolResultEvent`
- 最终完成事件：`AgentRunResultEvent`

然后由 `AgentRunner` 再把这些 PydanticAI 事件翻译成项目自己的 SSE 事件协议。

#### `run_chat_stream()` 现在的运行顺序

当前 `run_chat_stream()` 的主流程如下：

```text
run_chat_stream()
  -> resolve agent / model
  -> build deps
  -> load history
  -> 记录本轮暴露给模型的工具集合
  -> 尝试启动 run_stream_events()
  -> 成功则走 native stream 事件翻译
  -> 失败则切 fallback
```

更具体一点：

1. 先 `_resolve_agent()`，确定本轮使用哪个 `agent` 和哪个模型。
2. 再 `_build_deps()`，把 `request/settings/db/redis/http_client/tool_audit` 这些运行时依赖塞进 `AgentDeps`。
3. 如果请求带了 `session_id`，先从 `history_store` 里把旧消息历史读出来。
4. 调 `_record_tool_exposure()`，记录这轮 run 暴露给模型的工具集合。
5. 这里除了写 tool audit 记录，还会把每个工具的 `metadata` 返回出来，供后面的 `tool_call` / `tool_result` SSE 事件复用。
6. 开始尝试 `agent.run_stream_events(...)`。
7. 如果 native stream 启动成功，就把底层事件逐个翻译成项目自己的 SSE。
8. 如果 native stream 一启动就失败，就改走 `_run_chat_stream_fallback()`。

#### 为什么要先读取首个事件

`run_chat_stream()` 里不会直接 `async for event in stream`，而是会先做一次：

- `first_event = await anext(stream)`

这样做有两个目的：

1. 尽早判断当前模型是不是真的支持 streamed request。
2. 尽量提早拿到 `run_id`，保证 `start` 事件一发出去就有稳定会话标识。

因为在 PydanticAI 的事件流里：

- 文本事件和工具事件本身不保证自带 `run_id`
- 最稳定能拿到 `run_id` 的通常是最后的 `AgentRunResultEvent`

所以如果首事件提取不到 `run_id`，当前实现就先本地生成一个 `shortuuid`，避免整条 SSE 流没有统一标识。

#### 为什么保留 fallback

fallback 在：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L372)

保留它的原因很现实：

- 有些模型支持普通 `agent.run(...)`
- 但并不支持真正的 streamed request
- 如果没有 fallback，`/chat/stream` 在这些模型上就只能直接失败

当前策略是：

- 能 native stream，就走事件流
- 不能 native stream，就退化成一次普通 `run`
- 然后把结果包装成最小 SSE

fallback 路径当前能保证：

- 至少返回 `start`
- 普通完成时返回 `done`
- 审批时返回 `approval_pending` + `approval_required`
- 出错时返回 `error`

只是它拿不到中间过程，所以不会有：

- `delta`
- `tool_call`
- `tool_result`

#### 底层事件是怎么映射成 SSE 的

当前 `runner.py` 里的映射关系是：

- `PartStartEvent(TextPart)` / `PartDeltaEvent(TextPartDelta)`
  - 映射为 `delta`
- `FunctionToolCallEvent`
  - 映射为 `tool_call`
- `FunctionToolResultEvent`
  - 映射为 `tool_result`
- `AgentRunResultEvent`
  - 映射为最终的 `done`
  - 如果结果是 `DeferredToolRequests`，则发 `approval_pending` + `approval_required`

这一层的意义非常关键：

- 前端不需要理解 PydanticAI 内部事件类型
- 前端只需要消费我们自己定义的 SSE 事件名和 payload
- 后续如果底层框架升级，这一层可以继续充当隔离带

#### 为什么 `tool_call` 和 `tool_result` 仍然需要自行包装 payload

对应方法：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L543)
- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L560)

原因是底层事件对象不适合直接暴露给前端。

当前包装后，`tool_call` 统一带：

- `run_id`
- `tool_call_id`
- `tool_name`
- `args`
- `args_valid`
- `tool_metadata`

`tool_result` 统一带：

- `run_id`
- `tool_call_id`
- `tool_name`
- `status`
- `result`
- `tool_metadata`

这样设计的收益包括：

- 前端协议稳定
- 测试断言稳定
- 工具参数和结果都被统一标准化成可 JSON 化结构

其中有两个辅助方法是专门为这个目标准备的：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L586) `_normalize_tool_args()`
  - 把 `dict` 或 JSON 字符串统一整理成 `dict`
- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L601) `_normalize_value()`
  - 把复杂工具返回值尽量整理成稳定、可序列化的值

#### 为什么 `approval_pending` 和 `approval_required` 要同时发送

这次 stream 改造新增了更语义化的：

- `approval_pending`

它表达的是：

- 这轮流式运行已经走到“待审批”状态

但项目之前已有 approval 闭环是围绕：

- `status = "approval_required"`
- `/chat/resume`

来建立的。

为了不把现有前端和既有协议直接打断，当前 runner 的策略是：

- 新增一份 `approval_pending`
- 同时继续发送兼容旧逻辑的 `approval_required`

也就是说：

- 新前端可以开始监听 `approval_pending`
- 旧前端依然可以继续按 `approval_required` 处理

这是一个兼容性过渡方案，而不是重复事件设计失误。

#### `_build_chat_response()` 在这次改造里的作用

对应位置：

- [runner.py](/Users/yangyuexiong/Desktop/exile-agent/app/ai/runtime/runner.py#L331)

它现在不只是 `/chat` 的响应构造器，也是：

- `/chat`
- `/chat/resume`
- `/chat/stream` 最终完成阶段

共享的统一结果归一化入口。

它最关键的逻辑是：

- 如果 `result.output` 是普通文本，就返回 `status="completed"`
- 如果 `result.output` 是 `DeferredToolRequests`，就返回 `status="approval_required"`

这意味着：

- “是否进入审批分支”的判断不在 endpoint
- 不在前端
- 也不散落在多个接口里

而是在 runner 里统一完成

这样三条链路共享同一套语义：

- `/chat`
- `/chat/resume`
- `/chat/stream`

#### 对这次 `runner.py` 改造的概括

可以把这次改造概括成一句话：

- `AgentRunner` 已经从“基于文本流的 stream 封装”升级成“基于运行事件流的 stream 协议桥接层”

也就是说，`runner.py` 现在在 `/chat/stream` 里承担了三件事：

- 运行编排：调 agent、读写 history、处理异常
- 协议归一化：把结果统一整理成 `AgentChatResponse`
- 事件桥接：把 PydanticAI 底层事件翻译成项目自己的 SSE 协议

## 九、后续演进方向

如果沿着当前调用链继续扩展，最合适的方向是：

1. 继续细化 SSE 事件模型
2. 在 `AgentRunner` 中增强 history 策略（摘要 / 裁剪 / processors）
3. 在 runtime 层引入 MCP manager
4. 在 agent 构建阶段接入 Skills resolver

也就是说，后续这条链会逐步演变成：

```text
endpoint
  -> service
  -> runner
  -> history / toolsets / mcp / skills
  -> agent.run(...)
```

等这几层补齐后，这个项目才会从“最小可运行 AI 骨架”升级成“可扩展 AI 基建骨架”。
