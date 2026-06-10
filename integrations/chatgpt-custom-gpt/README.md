# Custom GPT: OrionBelt Analytics Assistant

This directory contains instructions for creating a Custom GPT in ChatGPT that connects to the OrionBelt Analytics MCP server.

## Files

| File | Purpose |
|------|---------|
| `instructions.md` | System prompt / instructions for the Custom GPT |

## Overview

OrionBelt Analytics is an MCP server, not a REST API. ChatGPT Custom GPTs with Actions require OpenAPI/REST endpoints. There are two approaches to integrate:

### Option 1: MCP-to-REST Bridge (Recommended)

Use an MCP-to-REST bridge like [mcp-bridge](https://github.com/mcp-bridge) or a lightweight FastAPI wrapper to expose MCP tools as REST endpoints:

```python
from fastapi import FastAPI
from fastmcp import Client

app = FastAPI()
client = Client("http://localhost:9000/mcp")

@app.post("/v1/connect")
async def connect_database(db_type: str):
    async with client:
        return await client.call_tool("connect_database", {"db_type": db_type})

# Add more endpoints as needed...
```

Then create an OpenAPI spec for the bridge and configure it as a GPT Action.

### Option 2: Use via Claude Desktop / MCP-Native Clients

Since OrionBelt Analytics is designed as an MCP server, it works natively with MCP-compatible clients:

- **Claude Desktop** / **Claude Code** - Add to MCP server config
- **Cursor** / **Windsurf** - IDE MCP integration
- **Any MCP client** - Use the standard MCP protocol

## Setup Steps

### 1. Start OrionBelt Analytics

```bash
uv run server.py
```

The MCP server runs on `http://localhost:9000` by default.

### 2. Create the Custom GPT

1. Go to [ChatGPT](https://chat.openai.com) and click **Explore GPTs** > **Create**
2. In the **Configure** tab:
   - **Name:** OrionBelt Analytics
   - **Description:** Analyze database schemas, generate RDF/OWL ontologies, and execute safe SQL queries across PostgreSQL, Snowflake, Dremio, ClickHouse, and MySQL.
   - **Instructions:** Paste the contents of `instructions.md`
   - **Conversation starters:**
     - Connect to PostgreSQL and show me the schemas
     - Analyze the public schema and describe the table relationships
     - Generate an ontology for the sales schema
     - Which tables are related to customer orders?

### 3. Add Actions (with MCP-to-REST Bridge)

If using Option 1 (bridge):

1. Scroll down to **Actions** > **Create new action**
2. Paste your OpenAPI spec for the bridge
3. Replace the server URL with your bridge's URL (must be HTTPS)
4. Set authentication as needed
5. Click **Save**

## Available Tools (via MCP)

| Tool | Description |
|------|-------------|
| `connect_database` | Connect to PostgreSQL, Snowflake, Dremio, ClickHouse, or MySQL |
| `list_schemas` | Discover available database schemas |
| `discover_schema` | Analyze schema structure with relationships |
| `get_table_details` | Deep-dive into a specific table |
| `generate_ontology` | Generate RDF/OWL ontology with SQL mappings |
| `execute_sql_query` | Execute SQL with built-in validation, security, and fan-trap protection |
| `sample_table_data` | Sample rows from a table |
| `generate_chart` | Create Plotly charts from query results |
| `graphrag_query_context` | Natural language schema discovery via GraphRAG |
| `graphrag_search` | Vector search across schema elements |
| `graphrag_find_join_path` | Find join paths between tables |
| `query_sparql` | Run SPARQL queries on the RDF store |

## Notes

- GPT Actions require HTTPS. No plain HTTP, no localhost.
- For local development, use ngrok or a similar tunnel to expose the bridge.
- The MCP server must be reachable from the bridge/gateway.
- Response timeout is ~45 seconds. Schema analysis and ontology generation may take longer for large schemas.
