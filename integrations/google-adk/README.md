# Google ADK Integration

Google Agent Development Kit (ADK) agent that connects to the OrionBelt Analytics MCP server. All 20+ tools are auto-discovered via ADK's native MCP support (`MCPToolset`).

## Files

| File | Purpose |
|------|---------|
| `agent_example.py` | Interactive agent using `Agent` + `Runner` with MCP session management |

## Prerequisites

```bash
pip install google-adk
```

## Setup

Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

Set your API key:

```bash
export GOOGLE_API_KEY=...
```

## Quick Start

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import MCPToolset
from google.genai import types

async def main():
    tools, cleanup = await MCPToolset.from_server(
        connection_params={"url": "http://localhost:9000/mcp"},
    )
    agent = Agent(
        name="orionbelt_analyst",
        model="gemini-2.0-flash",
        instruction="You are a database analyst. Use the tools to analyze schemas and generate SQL.",
        tools=tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_analyst", user_id="user",
    )
    runner = Runner(
        agent=agent,
        app_name="orionbelt_analyst",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text="Connect to PostgreSQL and analyze the public schema")],
    )
    async for event in runner.run_async(
        user_id="user", session_id=session.id, new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            print(event.content.parts[0].text)

    await cleanup()

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

For stdio transport:

```python
tools, cleanup = await MCPToolset.from_server(
    command="uv",
    args=["run", "--directory", "/path/to/orionbelt-analytics", "server.py"],
)
```

## Multi-Agent Example

Use ADK's sub-agent pattern:

```python
from google.adk.agents import Agent
from google.adk.tools.mcp_tool import MCPToolset

tools, cleanup = await MCPToolset.from_server(
    connection_params={"url": "http://localhost:9000/mcp"},
)

schema_agent = Agent(
    name="schema_analyst",
    model="gemini-2.0-flash",
    instruction="Analyze database schemas and generate ontologies.",
    tools=tools,
)

report_agent = Agent(
    name="report_writer",
    model="gemini-2.0-flash",
    instruction="Write data analysis reports. Delegate database queries to schema_analyst.",
    sub_agents=[schema_agent],
)
```

## Deploy to Vertex AI Agent Engine

ADK agents can be deployed to Google Cloud:

```python
from google.adk.agents import Agent
from google.adk.deploy import VertexAIAgentEngine

agent = Agent(name="orionbelt_analyst", model="gemini-2.0-flash", tools=tools)
engine = VertexAIAgentEngine(project="your-project", location="us-central1")
engine.deploy(agent)
```
