# OpenAI Agents SDK Integration

OpenAI Agents SDK agent that connects to the OrionBelt Analytics MCP server. All 20+ tools are auto-discovered via native MCP support.

## Files

| File | Purpose |
|------|---------|
| `agent_example.py` | Interactive agent using `Agent` + `Runner` with MCP |

## Prerequisites

```bash
pip install openai-agents
```

## Setup

Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

## Quick Start

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

async def main():
    mcp_server = MCPServerStreamableHttp(url="http://localhost:9000/mcp")
    agent = Agent(
        name="OrionBelt Analyst",
        model="gpt-4o",
        mcp_servers=[mcp_server],
        instructions="You are a database analyst. Use the tools to analyze schemas and generate SQL.",
    )
    result = await Runner.run(agent, "Connect to PostgreSQL and analyze the public schema")
    print(result.final_output)

asyncio.run(main())
```

## Auto-Discovered Tools

All MCP tools are automatically available. Key tools include:

| Tool | Description |
|------|-------------|
| `connect_database` | Connect to PostgreSQL, Snowflake, Dremio, ClickHouse, or MySQL |
| `list_schemas` | Discover available database schemas |
| `discover_schema` | Analyze schema structure with relationships |
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

## Stdio Transport

For stdio transport (subprocess):

```python
from agents.mcp import MCPServerStdio

mcp_server = MCPServerStdio(
    command="uv",
    args=["run", "--directory", "/path/to/orionbelt-analytics", "server.py"],
)
```

## Multi-Agent Example

Combine OrionBelt tools with other agents using handoffs:

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

mcp_server = MCPServerStreamableHttp(url="http://localhost:9000/mcp")

data_agent = Agent(
    name="Schema Analyst",
    model="gpt-4o",
    mcp_servers=[mcp_server],
    instructions="You analyze database schemas, generate ontologies, and execute SQL queries.",
)

report_agent = Agent(
    name="Report Writer",
    model="gpt-4o",
    instructions="You write clear data analysis reports based on schema analysis and query results.",
    handoffs=[data_agent],
)
```
