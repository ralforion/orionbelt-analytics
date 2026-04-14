[<- Back to README](../README.md)

# AI Framework Integrations

OrionBelt Analytics exposes all its tools -- schema analysis, ontology generation,
Text-to-SQL with fan-trap prevention, GraphRAG, and charting -- through a native
MCP (Model Context Protocol) server. Any AI agent framework with MCP client support
can connect to `http://localhost:9000/mcp` and auto-discover every available tool,
resource, and prompt without manual registration.

## Supported Frameworks

| Framework | Transport / Adapter | Example File | Notes |
|---|---|---|---|
| LangChain / LangGraph | `langchain-mcp-adapters` | [`agent_example.py`](../integrations/langchain/agent_example.py) | Uses `MultiServerMCPClient` with Streamable HTTP |
| OpenAI Agents SDK | `MCPServerStreamableHttp` | [`agent_example.py`](../integrations/openai-agents-sdk/agent_example.py) | Pass MCP server directly to `Agent(mcp_servers=)` |
| CrewAI | Native MCP (`mcps=`) | [`crew_example.py`](../integrations/crewai/crew_example.py) | 3-agent crew (Analyst, Ontologist, Reporter) |
| Google ADK | `MCPToolset.from_server()` | [`agent_example.py`](../integrations/google-adk/agent_example.py) | Google Agent Development Kit integration |
| Vercel AI SDK | `experimental_createMCPClient` | [`route-example.ts`](../integrations/vercel-ai-sdk/route-example.ts) | Next.js API route example |
| n8n | MCP Client Tool node | [`workflow_ai_agent.json`](../integrations/n8n/workflow_ai_agent.json) | Import JSON workflow into n8n |
| ChatGPT Custom GPT | MCP-to-REST bridge | [`instructions.md`](../integrations/chatgpt-custom-gpt/instructions.md) | Requires an external bridge to expose MCP as REST |

Complete, runnable examples with READMEs live in the [`integrations/`](../integrations/) folder.

## Quick Start Examples

### LangChain / LangGraph

```bash
pip install langchain langchain-anthropic langgraph langchain-mcp-adapters
```

```python
import asyncio
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main() -> None:
    async with MultiServerMCPClient(
        {
            "orionbelt-analytics": {
                "url": "http://localhost:9000/mcp",
                "transport": "streamable_http",
            }
        }
    ) as client:
        tools = client.get_tools()

        agent = create_agent(
            "anthropic:claude-sonnet-4-5",
            tools,
            prompt="You are a database analyst powered by OrionBelt Analytics.",
        )

        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Connect to my PostgreSQL and analyze the public schema."}]}
        )
        print(response["messages"][-1].content)

asyncio.run(main())
```

### OpenAI Agents SDK

```bash
pip install openai-agents
```

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

async def main() -> None:
    mcp_server = MCPServerStreamableHttp(url="http://localhost:9000/mcp")

    agent = Agent(
        name="OrionBelt Analyst",
        instructions="You are a database analyst powered by OrionBelt Analytics.",
        model="gpt-4o",
        mcp_servers=[mcp_server],
    )

    result = await Runner.run(agent, "Connect to my PostgreSQL and analyze the public schema.")
    print(result.final_output)

asyncio.run(main())
```

## Prerequisites

1. Start the OrionBelt Analytics MCP server:

   ```bash
   uv run server.py
   ```

2. Confirm the server is listening on `http://localhost:9000/mcp` (or the port
   configured via `MCP_SERVER_PORT`).

3. Install the framework-specific adapter package (see the table above or the
   individual README in each `integrations/` subfolder).
