"""Agent Tool 注册 · 给未来 B2 Agent Runtime 用。

PRD § B.5 Workflow as Tool for Agent: Agent 把每个 workflow 当黑盒工具调,
不看 workflow 内部步骤 · workflow 实现完全复用,只在 Agent 端注册一份 schema。

接入路径(B2 Agent Runtime ready 时):
    from tg_site_collector.agent_tools import SITE_COLLECTOR_TOOL_SPEC
    agent.register_tool(SITE_COLLECTOR_TOOL_SPEC)
"""

from tg_site_collector.agent_tools.site_collector_tool import (
    SITE_COLLECTOR_TOOL_SPEC,
    site_collector_tool_handler,
)

__all__ = [
    "SITE_COLLECTOR_TOOL_SPEC",
    "site_collector_tool_handler",
]
