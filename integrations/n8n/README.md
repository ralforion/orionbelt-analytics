# n8n Integration

n8n AI Agent workflow that connects to the OrionBelt Analytics MCP server. All 20+ tools are auto-discovered via n8n's native MCP Client Tool node.

## Files

| File | Purpose |
|------|---------|
| `workflow_ai_agent.json` | AI Agent workflow: chat-based database analysis with MCP tools |

## Prerequisites

- [n8n](https://n8n.io) (self-hosted or cloud)
- OrionBelt Analytics MCP server running and accessible from n8n

## Setup

1. Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

2. In n8n, set the environment variable `ORIONBELT_MCP_URL`:
   - Self-hosted: add `ORIONBELT_MCP_URL=http://localhost:9000/mcp` to your n8n environment
   - n8n Cloud: Settings > Variables > add `ORIONBELT_MCP_URL`

3. Import the workflow: n8n menu > Import from File > select the JSON file

## AI Agent Workflow

`workflow_ai_agent.json` creates a chat-based AI agent with OrionBelt Analytics tools:

```
Chat Trigger --> AI Agent
                   |-- OpenAI Chat Model (gpt-4o)
                   |-- OrionBelt Analytics MCP (auto-discovers all tools)
```

- Users chat with the agent in natural language
- The MCP Client Tool auto-discovers all 20+ tools from OrionBelt Analytics
- No manual HTTP configuration needed - MCP handles tool discovery
- Requires an OpenAI API key in n8n credentials

**Example conversations:**
- "Connect to PostgreSQL and list the schemas"
- "Analyze the public schema and show me the table relationships"
- "Generate an ontology for the sales schema"
- "Write a query to show the top 10 customers by revenue"

## Auto-Discovered Tools

The MCP Client Tool auto-discovers all tools. Key tools include:

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

## Customization

### Change the MCP URL

The MCP Client Tool uses `{{ $env.ORIONBELT_MCP_URL }}`. Set this variable in n8n:

- Local: `http://localhost:9000/mcp`
- Docker: `http://host.docker.internal:9000/mcp`
- Cloud: `https://your-orionbelt-host:9000/mcp`

### Use Anthropic Instead of OpenAI

Replace the "OpenAI Chat Model" node with an "Anthropic Chat Model" node and select `claude-sonnet-4-5`.
