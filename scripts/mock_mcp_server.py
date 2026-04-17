"""本地联调用的最小 MCP server。

用途：
- 验证项目内 `/api/v1/agents/chat` 的 MCP 装配链路是否正常
- 不依赖第三方 filesystem / git 等 MCP server
- 作为后续接入真实 MCP server 前的最小 smoke test 基线

启动方式：

```bash
.venv/bin/python scripts/mock_mcp_server.py
```

然后在项目配置中把该脚本声明为一个 `stdio` MCP server，即可通过
`mcp_servers=["demo"]` 在 `/chat` 中启用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="exile-agent-demo-mcp",
    instructions=(
        "This is a local demo MCP server for exile-agent. "
        "Use these tools when the caller asks to verify MCP connectivity, "
        "inspect demo runtime values, or echo a short payload."
    ),
)


@mcp.tool()
def ping() -> str:
    """返回固定响应，用于确认 MCP tool 调用链是否正常。"""

    return "pong from demo mcp server"


@mcp.tool()
def echo_text(text: str) -> str:
    """原样回显输入文本，便于验证模型是否真的调用了 MCP 工具。"""

    return text


@mcp.tool()
def get_runtime_snapshot() -> dict[str, str]:
    """返回当前 demo MCP server 的最小运行时快照。"""

    return {
        "server": "exile-agent-demo-mcp",
        "transport": "stdio",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
