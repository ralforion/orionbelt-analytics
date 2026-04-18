# Vercel AI SDK Integration

TypeScript integration for the [Vercel AI SDK](https://ai-sdk.dev) that connects to the OrionBelt Analytics MCP server. All 20+ tools are auto-discovered via Vercel AI SDK's native MCP support.

## Files

| File | Purpose |
|------|---------|
| `route-example.ts` | Next.js API route example (`app/api/chat/route.ts`) |

## Prerequisites

```bash
npm install ai @ai-sdk/anthropic
# Or with OpenAI:
npm install ai @ai-sdk/openai
```

## Setup

Start OrionBelt Analytics MCP server:

```bash
uv run server.py
```

Set environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
ORIONBELT_MCP_URL=http://localhost:9000/mcp
```

## Quick Start

### Next.js API Route

Copy `route-example.ts` into your Next.js app:

```
app/
  api/
    chat/
      route.ts          # route-example.ts
```

### Standalone Script

```typescript
import { generateText } from "ai";
import { anthropic } from "@ai-sdk/anthropic";
import { experimental_createMCPClient as createMCPClient } from "ai";

const client = await createMCPClient({
  transport: {
    type: "sse",
    url: "http://localhost:9000/mcp",
  },
});

const tools = await client.tools();

const { text } = await generateText({
  model: anthropic("claude-sonnet-4-5"),
  tools,
  maxSteps: 15,
  prompt: "Connect to PostgreSQL and analyze the public schema",
});

console.log(text);
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
| `get_server_info` | Server version and capabilities |

## Using with OpenAI

Replace the model provider in the route:

```typescript
import { openai } from "@ai-sdk/openai";

const result = streamText({
  model: openai("gpt-4o"),
  // ... rest stays the same
});
```

## Using with useChat (Frontend)

Pair with Vercel AI SDK's `useChat` hook for a complete chat UI:

```tsx
"use client";
import { useChat } from "@ai-sdk/react";

export default function Chat() {
  const { messages, input, handleInputChange, handleSubmit } = useChat({
    api: "/api/chat",
  });

  return (
    <div>
      {messages.map((m) => (
        <div key={m.id}>
          <strong>{m.role}:</strong>
          <pre>{m.content}</pre>
        </div>
      ))}
      <form onSubmit={handleSubmit}>
        <input value={input} onChange={handleInputChange} placeholder="Ask about your database..." />
      </form>
    </div>
  );
}
```
