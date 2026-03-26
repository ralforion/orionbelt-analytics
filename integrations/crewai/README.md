# CrewAI Integration

CrewAI multi-agent crew that connects to the OrionBelt Analytics MCP server. All 20+ tools are auto-discovered via CrewAI's native MCP support.

## Files

| File | Purpose |
|------|---------|
| `crew_example.py` | Three-agent crew: Schema Analyst + Query Analyst + Report Writer |

## Prerequisites

```bash
pip install crewai
```

## Setup

Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

## Quick Start

```python
from crewai import Agent, Crew, Task

agent = Agent(
    role="Database Analyst",
    goal="Analyze database schemas and execute safe SQL queries.",
    backstory="You are a data architect using OrionBelt Analytics.",
    mcps=[{
        "url": "http://localhost:9000/mcp",
        "transport": "streamable_http",
    }],
)

task = Task(
    description="Connect to PostgreSQL, analyze the public schema, and generate an ontology.",
    expected_output="Schema analysis summary with ontology description.",
    agent=agent,
)

crew = Crew(agents=[agent], tasks=[task])
result = crew.kickoff()
print(result)
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

## Multi-Agent Crew Example

The `crew_example.py` demonstrates a three-agent crew:

1. **Schema Analyst** — Connects to the database, analyzes schemas, and generates ontologies
2. **Query Analyst** — Writes validated SQL queries and executes them safely
3. **Report Writer** — Formats analysis results into a readable report

```bash
export OPENAI_API_KEY=sk-...
python crew_example.py
```

## Stdio Transport

For stdio transport:

```python
agent = Agent(
    role="Database Analyst",
    goal="Analyze database schemas.",
    backstory="You are a data architect.",
    mcps=[{
        "command": "uv",
        "args": ["run", "--directory", "/path/to/orionbelt-analytics", "server.py"],
        "transport": "stdio",
    }],
)
```
