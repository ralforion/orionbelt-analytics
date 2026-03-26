# LangChain / LangGraph Integration

LangChain agent that connects to the OrionBelt Analytics MCP server. All 20+ tools (schema analysis, ontology generation, SQL execution, GraphRAG, SPARQL) are auto-discovered via MCP.

## Files

| File | Purpose |
|------|---------|
| `agent_example.py` | Interactive agent using LangGraph with `create_agent` and MCP |

## Prerequisites

```bash
pip install langchain langchain-anthropic langgraph langchain-mcp-adapters

# Or with OpenAI:
pip install langchain langchain-openai langgraph langchain-mcp-adapters
```

## Setup

Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

## Quick Start

```python
import asyncio
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    async with MultiServerMCPClient({
        "orionbelt-analytics": {
            "url": "http://localhost:9000/mcp",
            "transport": "streamable_http",
        }
    }) as client:
        tools = client.get_tools()
        agent = create_agent("anthropic:claude-sonnet-4-5", tools)
        result = await agent.ainvoke(
            {"messages": "Connect to PostgreSQL and analyze the public schema"}
        )
        print(result["messages"][-1].content)

asyncio.run(main())
```

## Auto-Discovered Tools

All MCP tools are automatically available. Key tools include:

| Tool | Description |
|------|-------------|
| `connect_database` | Connect to PostgreSQL, Snowflake, Dremio, ClickHouse, or MySQL |
| `list_schemas` | Discover available database schemas |
| `analyze_schema` | Analyze schema structure with relationships |
| `get_table_details` | Deep-dive into a specific table |
| `generate_ontology` | Generate RDF/OWL ontology with SQL mappings |
| `validate_sql_syntax` | Validate SQL syntax, security, and fan-trap risks |
| `execute_sql_query` | Execute validated SQL with fan-trap protection |
| `sample_table_data` | Sample rows from a table |
| `generate_chart` | Create Plotly charts from query results |
| `graphrag_query_context` | Natural language schema discovery via GraphRAG |
| `graphrag_search` | Vector search across schema elements |
| `graphrag_find_join_path` | Find join paths between tables |
| `query_sparql` | Run SPARQL queries on the RDF store |
| `get_server_info` | Server version and capabilities |

## Stdio Transport

For stdio transport (e.g., subprocess):

```python
async with MultiServerMCPClient({
    "orionbelt-analytics": {
        "command": "uv",
        "args": ["run", "--directory", "/path/to/orionbelt-analytics", "server.py"],
        "transport": "stdio",
    }
}) as client:
    tools = client.get_tools()
```

## LangGraph StateGraph (Advanced)

For full control over the agent loop:

```python
import asyncio
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    async with MultiServerMCPClient({
        "orionbelt-analytics": {
            "url": "http://localhost:9000/mcp",
            "transport": "streamable_http",
        }
    }) as client:
        tools = client.get_tools()
        model = init_chat_model("anthropic:claude-sonnet-4-5")

        def call_model(state: MessagesState):
            return {"messages": model.bind_tools(tools).invoke(state["messages"])}

        builder = StateGraph(MessagesState)
        builder.add_node("call_model", call_model)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "call_model")
        builder.add_conditional_edges("call_model", tools_condition)
        builder.add_edge("tools", "call_model")
        graph = builder.compile()

        result = await graph.ainvoke(
            {"messages": [{"role": "user", "content": "Connect to PostgreSQL and show me the schema"}]}
        )
        for msg in result["messages"]:
            if msg.content:
                print(f"[{msg.type}] {msg.content[:500]}")

asyncio.run(main())
```
