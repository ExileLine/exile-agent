# exile-agent

当前项目是一个基于 `FastAPI` 的服务端基础骨架，并已经完成 `Phase 1` 的 AI 最小运行基建接入。

这份 README 不重点讲“怎么用”，而是重点讲“当前 AI 调用链路是怎么跑起来的”。  
目标是让你在继续做 `toolsets / MCP / Skills / History` 之前，先把现有这条最小链路完全看明白。

当前已经具备的 AI 能力：

- 应用启动时初始化 AI runtime
- 注册最小 `chat-agent`
- 支持通过 `/api/v1/agents/chat` 触发一次 Agent 调用
- 支持 `deps_type + RunContext`
- 支持通过 `FunctionToolset` 装配基础工具
- 已提供 4 个 builtin 只读工具
- 支持真实模型和 `TestModel`

当前还没有进入：

- Redis 会话历史
- MCP 接入
- Skills 基础设施
- 流式输出
- 审批续跑
- 更完整的 `toolsets` 包装、审计与审批治理

---

## 先看结论

当前 `/api/v1/agents/chat` 的核心调用链，可以先粗看成下面这条线：

```text
FastAPI app
  -> lifespan.startup_event()
  -> init_ai_runtime()
  -> app.state 挂载 ai_runner / ai_agent_manager

POST /api/v1/agents/chat
  -> agent endpoint
  -> _build_chat_service(request)
  -> ChatService.chat(...)
  -> AgentRunner.run_chat(...)
  -> AgentManager.get_agent(...)
  -> build_chat_agent(...)
  -> agent.run(message, deps=deps)
  -> 返回 AgentChatResponse
  -> api_response(...)
```

如果要把这条链解释成人话，大概是：

1. 应用启动时，`FastAPI` 会先把 AI 运行时对象准备好。
2. 这些对象会被挂到 `app.state` 上。
3. 请求来了以后，endpoint 不自己创建 Agent，也不自己直接调模型。
4. endpoint 只是从 `app.state` 里取出已经准备好的 `ai_runner` 和 `ai_agent_manager`，组装成 `ChatService`。
5. `ChatService.chat()` 再继续调用 `AgentRunner.run_chat()`。
6. `AgentRunner` 是“真正调大模型的运行入口”，负责：
   - 确定要用哪个 Agent
   - 确定要用哪个模型
   - 构造 `AgentDeps`
   - 调用 `agent.run(...)`
   - 把结果整理成统一响应结构
7. `AgentManager` 负责“拿 Agent”，如果缓存里没有，就调用 `build_chat_agent()` 构建。
8. `build_chat_agent()` 负责定义这个 Agent 的模型、输出类型、提示词和 toolsets。
9. 最后 `pydantic_ai.Agent.run(...)` 才真正触发模型调用或测试模型执行。

你举的那个例子，方向是对的，下面会按源码把每一步写细。

---

## 当前 AI 关键目录

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
    runtime/__init__.py         # init_ai_runtime / shutdown_ai_runtime
    runtime/registry.py         # AgentRegistry
    runtime/manager.py          # AgentManager
    runtime/runner.py           # AgentRunner
    agents/__init__.py          # 默认 agent 注册入口
    agents/chat_agent.py        # build_chat_agent
    services/chat_service.py    # ChatService
```

建议你看链路时，把这些文件按下面顺序理解：

1. [app/main.py](./app/main.py)
2. [app/core/lifespan.py](./app/core/lifespan.py)
3. [app/ai/runtime/__init__.py](./app/ai/runtime/__init__.py)
4. [app/api/v1/endpoints/agent.py](./app/api/v1/endpoints/agent.py)
5. [app/ai/services/chat_service.py](./app/ai/services/chat_service.py)
6. [app/ai/runtime/runner.py](./app/ai/runtime/runner.py)
7. [app/ai/runtime/manager.py](./app/ai/runtime/manager.py)
8. [app/ai/agents/chat_agent.py](./app/ai/agents/chat_agent.py)

---

## 一、应用启动阶段：`init_ai_runtime()` 是怎么被调用的

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

这里的意思是：

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

这里的语义是：

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

这一步有两个重要含义：

#### 含义 1：AI runtime 依赖于更底层的基础设施

也就是说，当前 AI 层默认认为这些资源可能会被后续 Agent 用到：

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

你可以把它理解成：

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
- `openai_api_key`
- `openai_base_url`

### Step 2. 创建 `AgentRegistry`

代码：

```python
registry = AgentRegistry()
```

`AgentRegistry` 的角色可以理解成：

- “Agent 注册表”
- “系统里有哪些 Agent，先在这里登记”

它内部存的是：

- `agent_id`
- `manifest`
- `builder`

这里有个重要点：

#### `AgentRegistry` 里不是直接存一个已经跑起来的 Agent 实例

它存的是：

- 一份描述信息 `manifest`
- 一个构造函数 `builder`

为什么这样做？

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

可以理解成：

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

这里可以这样理解：

- `AgentRegistry` 是“登记簿”
- `AgentManager` 是“管理员”

后面真正请求来了，不是 endpoint 自己去 registry 里翻，而是统一交给 `AgentManager` 去拿。

### Step 5. 创建共享 `http_client`

代码：

```python
http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
```

当前 `Phase 1` 里，这个对象还没有大量派上用场，但它已经是 `AgentDeps` 的一部分了。

也就是说，后续工具层如果要调用：

- 内部 HTTP 服务
- 第三方 API
- MCP 相关 HTTP 资源

都可以走这个共享 client。

### Step 6. 创建 `AgentRunner`

代码：

```python
runner = AgentRunner(
    settings=settings,
    agent_manager=manager,
    http_client=http_client,
)
```

`AgentRunner` 是你可以重点记住的一个对象。

在当前架构里，它是：

- 真正负责“执行一次 Agent 调用”的核心入口

也就是说：

- `AgentManager` 管理“拿哪个 Agent”
- `AgentRunner` 管理“怎么跑这次请求”

当前只实现了 `run_chat(...)`，但后面很自然会扩展成：

- `run_stream(...)`
- `resume(...)`
- `run_with_history(...)`

### Step 7. 把这些对象挂到 `app.state`

代码：

```python
app.state.ai_settings = settings
app.state.ai_agent_registry = registry
app.state.ai_agent_manager = manager
app.state.ai_http_client = http_client
app.state.ai_runner = runner
```

这一步非常关键，因为它决定了后续 endpoint 怎么拿到这些对象。

你可以把 `app.state` 理解成：

- FastAPI 应用级别的共享对象容器

为什么要放到这里？

因为这些对象的生命周期和应用一致，而不是和单个请求一致。

所以后面在 `POST /api/v1/agents/chat` 时：

- `_build_chat_service(request)` 就是通过 `request.app.state` 把 `ai_runner` 和 `ai_agent_manager` 取出来

这正是你上面举的例子里那个意思，而且当前代码也确实就是这样实现的。

---

## 三、请求阶段：`POST /api/v1/agents/chat` 是怎么一路走下去的

下面开始看一次实际请求从进来到出去的调用链。

---

## 1. 请求先经过中间件

入口在 [app/core/middleware.py](./app/core/middleware.py)。

当前最关键的一步是注入 `request_id`：

```python
request_id = headers.get("x-request-id") or headers.get("x-log-uuid") or shortuuid.uuid()
scope.setdefault("state", {})["request_id"] = request_id
```

这一步的作用是：

- 如果上游没有传 `x-request-id`
- 系统就自己生成一个 `request_id`
- 然后放到 `request.state` 对应的底层 `scope["state"]` 里

后面 endpoint 可以这样取：

```python
getattr(request.state, "request_id", None)
```

这样做的好处是：

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

### 3.1 `_build_chat_service(request)` 到底做了什么

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

这一步可以用你想要的方式解释成：

- `POST /api/v1/agents/chat` 被调用时
- endpoint 先通过 `request.app.state` 取出应用启动阶段放进去的 `ai_runner` 和 `ai_agent_manager`
- 然后用这两个对象实例化 `ChatService`

这两个对象分别是什么：

- `ai_runner` 是 `AgentRunner`
  - 它是当前“执行一次大模型调用”的核心运行入口
- `ai_agent_manager` 是 `AgentManager`
  - 它负责根据 `agent_id` 和 `model_name` 拿到正确的 Agent 实例

所以 `_build_chat_service(...)` 的意义不是“随便 new 一个 service”，而是：

- 把应用级 AI runtime 对象，桥接给当前这次请求使用

如果这里拿不到 `ai_runner` 或 `ai_agent_manager`，就说明：

- 应用启动时 `init_ai_runtime()` 没有正确执行
- 或者 app.state 没准备好

因此这里会报：

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

这一层的目的，是把“请求级信息”从 `FastAPI Request` 中抽出来，变成 AI 层可以稳定使用的结构。

为什么不把 `FastAPI Request` 直接传给 Agent 层？

因为那样会导致：

- Agent 层和 Web 框架强绑定
- 工具函数测试困难
- 后面迁移到别的入口时很麻烦

所以现在做法是：

- Web 层只提取必要字段
- 再放进 `RequestContext`

当前提取的字段有：

- `request_id`
- `user_id`
- `session_id`

这一步实际上就是在准备后面 `AgentDeps.request` 的值。

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

也就是说：

- `ChatService.chat()` 本质上只是把 endpoint 的参数继续往下转给 `AgentRunner.run_chat()`

为什么还要保留这一层？

因为它是一个稳定的服务边界。

后面如果你要加：

- 权限判断
- 业务限流
- 审计
- 参数兜底逻辑

这些逻辑比起放在 endpoint，更适合放在 `ChatService`。

所以现在虽然它看起来只是“一层转发”，但这是有意保留出来的扩展位。

---

## 5. 进入 `AgentRunner.run_chat(...)`

入口在 [app/ai/runtime/runner.py](./app/ai/runtime/runner.py)。

这一步是当前 AI 链路里最重要的运行入口。

可以把它理解成：

- “把一次 Web 请求，转成一次 Agent 运行”

它当前做了以下几件核心事情。

### 5.1 检查 AI 是否启用

```python
if not self.settings.enabled:
    raise AIDisabledError("AI 能力已关闭")
```

这一步是最外层的开关保护。

### 5.2 确定这次到底要用哪个 Agent

代码：

```python
resolved_agent_id = agent_id or self.settings.default_agent
```

逻辑是：

- 如果请求体里传了 `agent_id`，优先使用请求里的
- 否则使用系统默认 `default_agent`

当前默认值是：

- `chat-agent`

### 5.3 确定这次到底要用哪个模型

代码：

```python
resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
```

这一步不是简单地看 `payload.model`，而是交给 `AgentManager` 去统一解析。

逻辑是：

- 如果请求里显式传了 `model`
  - 用请求值
- 否则
  - 用该 Agent manifest 的默认模型
- 如果 manifest 没定义
  - 再回退到系统默认模型

也就是说，模型解析规则没有散落在 endpoint，而是集中放到了 manager 层。

### 5.4 通过 `AgentManager` 获取 Agent

代码：

```python
agent = self.agent_manager.get_agent(resolved_agent_id, resolved_model)
```

这一句非常关键，因为它把控制权交给了 `AgentManager`。

此时 `AgentRunner` 不关心：

- registry 内部怎么查
- Agent 是否已经缓存
- 是否需要重新构建

它只关心一件事：

- “给我一个这次应该跑的 Agent”

这就是当前分层的意义。

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

这一段是现阶段 AI 架构里最关键的依赖注入点。

它的含义是：

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

为什么这一步这么重要？

因为后面所有带上下文的工具，都会通过：

- `ctx.deps`

访问这些资源。

这就是 `deps_type + RunContext` 在当前项目里的真正落点。

### 5.6 真正调用 Agent

代码：

```python
result = await agent.run(message, deps=deps)
```

这是当前链路里真正触发模型执行的那一行。

可以这样理解：

- 前面所有层都在做准备工作
- 到这里才真正把用户输入交给 `PydanticAI Agent`

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

这一步不是直接把 `PydanticAI` 的原始结果对象返回给接口，而是转换成项目自己的 schema：

- `AgentChatResponse`

这带来的好处是：

- API 响应结构稳定
- 后面即使底层库升级，接口层也不至于跟着乱掉

---

## 6. `AgentManager.get_agent(...)` 到底做了什么

入口在 [app/ai/runtime/manager.py](./app/ai/runtime/manager.py)。

这里的职责是：

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

可以解释成：

1. 先看缓存里有没有这个 `(agent_id, model)` 对应的 Agent
2. 如果有，直接返回
3. 如果没有，就去 registry 里找到对应的 builder
4. 调用 builder 真正构建 Agent
5. 缓存起来，后面复用

所以 `AgentManager` 的价值在于：

- 不让 endpoint 直接 new Agent
- 不让 `AgentRunner` 直接知道每个 Agent 怎么构造
- 把“获取 Agent”的逻辑集中管理

### 6.1 这里说的“缓存”到底是什么

这里提到的“如果缓存里没有，就调用 builder 构建”，缓存指的不是：

- Redis
- 数据库
- `app.state`

这里的缓存，指的是 `AgentManager` 实例内部的一个内存字典：

```python
self._cache: dict[tuple[str, str], Agent[AgentDeps, str]] = {}
```

也就是说：

- key 是 `(agent_id, model_name)`
- value 是已经构建好的 `PydanticAI Agent` 实例

可以把它理解成：

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

也就是说：

1. 先生成 `cache_key = (agent_id, resolved_model)`
2. 查询 `_cache`
3. 如果没有命中
   - 去 `registry` 找到这个 Agent 的 `builder`
   - 调用 `builder(...)`
   - 把返回的 Agent 放进 `_cache`
4. 如果命中
   - 直接返回缓存里的 Agent

所以缓存写入的时机，不是在应用启动阶段，而是在**第一次真正请求到某个 `agent_id + model_name` 组合时**。

### 6.4 举一个实际例子

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

所以如果以后你用多 worker，比如：

- 多个 `uvicorn` worker
- `gunicorn + uvicorn workers`

那么每个 worker 都会有自己独立的一份：

- `AgentManager`
- `_cache`

这是正常行为，不是 bug。

---

## 7. `build_chat_agent(...)` 到底做了什么

入口在 [app/ai/agents/chat_agent.py](./app/ai/agents/chat_agent.py)。

这一步就是当前默认 Agent 的定义位置。

可以理解成：

- “chat-agent 长什么样，就在这里定义”

### 7.1 先构造模型对象

代码：

```python
model = _build_model(settings, model_name)
```

这一步是为了兼容两种场景：

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

这里几个关键参数可以这样理解：

- `model=model`
  - 这次 Agent 真正要用的模型
- `deps_type=AgentDeps`
  - 这个 Agent 运行时依赖的类型
- `output_type=str`
  - 当前输出就是字符串
- `instructions=...`
  - 默认系统级行为约束

这一步本质上就是：

- 定义这个 Agent 的“人格 + 模型 + 依赖类型 + 输出类型”

### 7.3 装配 `builtin_toolset`

当前不是直接在 `chat-agent` 上零散注册工具，而是通过 `toolsets=[get_builtin_toolset()]` 装配一个基础工具集。

这代表当前项目已经从“Agent 内直接挂两个函数工具”，演进到“通过 `FunctionToolset` 组织基础工具”的阶段。

当前 `builtin_toolset` 提供了 4 个只读工具：

- `get_current_utc_time`
- `get_request_context`
- `get_runtime_config_summary`
- `check_runtime_resources`

这些工具仍然会被模型看到并作为普通 function tools 调用，但它们的组织方式已经变成统一的 toolset。

这样做的直接收益是：

- 工具定义不再散落在具体 Agent 文件里
- 后续可以继续拆出更多业务 toolsets
- 为后面的 wrapper / approval / audit / MCP / Skills 动态装配预留结构

#### 为什么 `FunctionToolset` 还会有 `instructions`

`FunctionToolset` 不只是“装工具的容器”，它自己也可以附带一段 instructions。

这段 instructions 不是给 Python 看的，而是给模型看的。它的作用是告诉模型：

- 这组工具是做什么的
- 什么时候应该优先使用它们
- 不要在不需要的时候乱用它们

当前 `builtin_toolset` 里的 instructions 大意是：

- 当用户询问请求元数据、当前时间、或者 AI runtime 配置摘要时，优先使用 builtin tools

为什么这段话不直接写进 `chat-agent` 的 `instructions` 里？

因为两者职责不同：

- `agent.instructions` 负责定义 Agent 的整体行为、回答风格和全局约束
- `toolset.instructions` 负责定义“这一组工具”的使用策略

这样拆开的好处是：

- 同一个 toolset 将来可以复用到多个 Agent
- Agent 本身不会堆满工具使用细节
- 后续不同 toolset 可以携带各自不同的 instructions

#### `id="builtin-toolset"` 的作用是什么

这个 `id` 不是给模型看的，而是给系统和运行时看的。可以把它理解成：

- 这个 toolset 的稳定身份标识

当前阶段它最直接的价值是：

- 可读性更强，一眼能知道这是哪个 toolset
- 后续日志、调试、排查时更容易标识来源
- 为未来多个 toolset 并存时提供稳定命名

更进一步地说，`FunctionToolset.id` 也是在为后面的能力预留结构，例如：

- wrapper / audit / approval 按 toolset 定向处理
- Skills 依赖某个指定 toolset
- MCP 动态装配后区分 builtin toolset 和外部 toolset
- durable execution 或工作流恢复时稳定识别 toolset

所以：

- `instructions` 是告诉模型“这组工具什么时候用、怎么用”
- `id` 是告诉系统“这组工具是谁”

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

所以可以理解成：

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

可以把它理解成：

- `tool` 适合“依赖请求态 / 依赖外部资源 / 依赖运行配置”的工具

#### 为什么有了 `tool` / `tool_plain` 还需要 `FunctionToolset`

因为 `tool` 和 `tool_plain` 只解决“定义一个工具”的问题，不解决“项目级组织工具”的问题。

如果所有工具都直接写在某个 Agent 里，短期能跑，但后面会出现这些问题：

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
toolsets=[get_builtin_toolset()]
```

但它本质上是一个序列，所以完全可以扩展成：

```python
toolsets=[
    get_builtin_toolset(),
    get_ops_toolset(),
    get_business_toolset(),
]
```

也可以混合不同来源的 toolset，例如：

```python
toolsets=[
    get_builtin_toolset(),
    get_business_toolset(),
    mcp_toolset,
]
```

这意味着，后续项目完全可以按“能力来源”拆分工具，而不是把所有工具都塞进一个大 toolset 里。

#### 多个 toolset 组合时，模型看到的是什么

从模型视角看，最终它看到的是多个 toolset 合并后的可用工具集合。

也就是说：

- `builtin_toolset` 提供一批基础只读工具
- `business_toolset` 提供一批业务工具
- `mcp_toolset` 提供一批外部能力工具

最后模型会把它们当成同一轮 run 中可用的工具池来使用。

#### 为什么后面大概率会有多个 toolset

因为后续这个项目不可能永远只有一组 builtin tools。

按现在的规划，后面很可能逐步出现：

- `builtin_toolset`
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

所以项目进入更复杂阶段后，应该尽早建立工具命名规范，例如：

- `runtime_get_status`
- `ops_get_status`
- `customer_get_profile`

这样不同 toolset 的工具来源会更清晰。

#### 静态装配和动态装配的区别

当前 `chat-agent` 里写的是：

```python
toolsets=[get_builtin_toolset()]
```

这属于静态装配，也就是：

- 这个 Agent 每次运行都会固定带上这组 toolset

后面更成熟的做法通常是：

- Agent 定义阶段挂一部分固定 toolsets
- Runner 执行阶段再根据请求动态补更多 toolsets

例如：

- 所有请求都带 `builtin_toolset`
- 某些请求额外带业务 toolset
- 某些请求额外带 skill toolset
- 某些请求额外带 MCP toolset

也就是说，后面这条链大概率会从：

```python
toolsets=[get_builtin_toolset()]
```

演进成：

```python
toolsets=[
    get_builtin_toolset(),
    get_business_toolset(),
    *resolved_skill_toolsets,
    *resolved_mcp_toolsets,
]
```

所以你可以把当前这一步理解成：

- 先把单个 `builtin_toolset` 跑通
- 后续再把它扩展成多个 toolset 的组合装配机制

#### 当前项目里的推荐用法

按照现在这套架构，建议这样选：

- 工具是纯函数：优先 `tool_plain`
- 工具依赖 `ctx.deps`：优先 `tool`
- 一组工具要被统一挂载、复用、治理：放进 `FunctionToolset`

所以在当前阶段：

- `get_current_utc_time` 是 `tool_plain`
- `get_request_context` 是 `tool`
- `builtin_toolset` 是 `FunctionToolset`

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

所以对外接口返回的不是底层库对象，而是你项目自己的标准格式：

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

## 四、把整条链再压缩成一句一句的人话

下面用更接近你想要的方式，把调用链再解释一遍。

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
9. `AgentRunner` 会先解析这次请求到底要用哪个：
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

如果把这些逻辑全部堆在 endpoint 里，理论上也能跑，但后面会非常难扩展。

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

---

## 六、当前这条链已经验证了什么

目前已经验证通过：

- 应用启动时可以初始化 AI runtime
- `/api/v1/agents/chat` 可以真实触发 AgentRunner
- `AgentManager` 可以正确获取和缓存 Agent
- `build_chat_agent()` 可以正确构造 Agent
- `chat-agent` 可以通过 `FunctionToolset` 装配 builtin tools
- `AgentDeps` 可以被传入 `agent.run(...)`
- `RunContext[AgentDeps]` 工具可以访问请求上下文
- 可以使用 `TestModel`
- 可以使用真实 OpenAI 兼容 provider

---

## 七、当前这条链还没覆盖什么

当前它还是最小闭环，只覆盖：

- 单次 chat 调用
- 单 Agent
- 少量基础工具

还没覆盖：

- `FunctionToolset`
- 历史消息存储
- 多轮上下文恢复
- `run_stream`
- `resume`
- MCP manager
- Skills resolver

所以现在最准确的定位是：

- “AI 最小运行链路已经打通”
- 但“AI 平台级能力还没铺开”

---

## 八、下一步最自然的演进方向

如果沿着当前调用链继续扩展，最合适的方向是：

1. 在 `AgentRunner` 前后补 toolsets 装配逻辑
2. 在 `ChatService` 或 `AgentRunner` 中接入历史消息
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
