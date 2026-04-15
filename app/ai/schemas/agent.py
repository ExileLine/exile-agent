from pydantic import BaseModel, Field


class AgentManifest(BaseModel):
    agent_id: str = Field(description="Agent 唯一标识")
    name: str = Field(description="Agent 名称")
    description: str = Field(description="Agent 描述")
    default_model: str = Field(description="默认模型")
    supports_stream: bool = Field(default=False, description="是否支持流式输出")
