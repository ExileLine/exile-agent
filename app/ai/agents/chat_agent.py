from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import DeferredToolRequests

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.toolsets import (
    get_builtin_toolsets,
    wrap_toolsets_with_audit,
    wrap_toolsets_with_metadata_approval,
)

ChatAgentOutput = str | DeferredToolRequests


def build_chat_agent(settings: AISettings, model_name: object) -> Agent[AgentDeps, ChatAgentOutput]:
    """构造默认的 chat-agent 定义。

    这里做的是“Agent 定义态”的事情：
    - 选择模型
    - 声明 deps_type / output_type
    - 设置全局 instructions
    - 挂载默认 builtin toolsets

    它本身并不执行模型调用，真正执行发生在 `AgentRunner.run_chat(...)` 里。
    """
    model = _build_model(settings, model_name)
    agent: Agent[AgentDeps, ChatAgentOutput] = Agent[AgentDeps, ChatAgentOutput](
        model=model,
        deps_type=AgentDeps,
        # 这里显式把 `DeferredToolRequests` 放进输出类型。
        # 一旦某个工具命中了 approval / deferred 流程，PydanticAI 才能把这次 run
        # 作为“待审批结果”返回，而不是直接抛 UserError。
        output_type=[str, DeferredToolRequests],
        name="chat-agent",
        instructions=(
            "You are the default assistant for this FastAPI backend project. "
            "Be concise, factual, and implementation-oriented. "
            "Prefer answering in Chinese unless the user asks otherwise. "
            "When runtime metadata would help, use the available builtin tools instead of guessing."
        ),
        retries=settings.max_retries,
        # 这里挂的是一组 builtin toolsets，而不是单个大 toolset。
        # Agent 运行时会把这些 toolset 合并成当前这轮 run 的可用工具集合。
        # 当前装配顺序是：
        # 1. 先按 metadata 包 approval wrapper，预留敏感工具审批能力
        # 2. 再包 audit wrapper，记录真实工具执行事件
        toolsets=wrap_toolsets_with_audit(
            wrap_toolsets_with_metadata_approval(get_builtin_toolsets())
        ),
        defer_model_check=True,
    )

    return agent


def _build_model(settings: AISettings, model_name: object):
    """根据配置解析当前 Agent 应该使用的模型对象。

    当前支持两种路径：
    - 非 `openai:` 前缀：直接把字符串交给 PydanticAI 处理
    - `openai:` 前缀：显式构造 OpenAI 兼容 provider + chat model

    这样既能兼容测试态/简化场景，也能兼容真实的 OpenAI-compatible 服务端。
    """
    if not isinstance(model_name, str):
        return model_name

    if not model_name.startswith("openai:"):
        return model_name

    if not settings.openai_api_key and not settings.openai_base_url:
        return model_name

    provider = OpenAIProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    return OpenAIChatModel(model_name=model_name.removeprefix("openai:"), provider=provider)
