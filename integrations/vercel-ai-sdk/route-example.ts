/**
 * Example: Next.js API route using Vercel AI SDK with OrionBelt Analytics MCP.
 *
 * Place this file at: app/api/chat/route.ts
 *
 * Prerequisites:
 *   npm install ai @ai-sdk/anthropic
 *
 * Environment variables:
 *   ANTHROPIC_API_KEY=sk-ant-...
 *   ORIONBELT_MCP_URL=http://localhost:9000/mcp
 */

import { streamText } from "ai";
import { anthropic } from "@ai-sdk/anthropic";
import { experimental_createMCPClient as createMCPClient } from "ai";

const SYSTEM_PROMPT = `You are a database intelligence assistant powered by OrionBelt Analytics.
You help users analyze database schemas, generate ontologies, and write safe SQL queries.

Workflow:
1. Call connect_database to establish a connection (postgresql, snowflake, dremio, clickhouse, or mysql).
2. Call list_schemas to discover available schemas.
3. Call discover_schema to get the schema structure with relationships.
4. Use get_table_details for deep-dive into specific tables.
5. Call generate_ontology to create RDF/OWL ontology with SQL mappings.
6. Use validate_sql_syntax before running queries.
7. Use execute_sql_query to run validated SQL (set checklist_completed=true).
8. Use generate_chart to visualize query results.

Rules:
- Always validate SQL before execution using validate_sql_syntax.
- Set checklist_completed=true when calling execute_sql_query after validation.
- Fan-trap warnings must be resolved before executing multi-fact queries.
- Present SQL in code blocks. Explain ontology triples in plain language.`;

export async function POST(req: Request) {
  const { messages } = await req.json();

  const mcpUrl = process.env.ORIONBELT_MCP_URL || "http://localhost:9000/mcp";

  const client = await createMCPClient({
    transport: {
      type: "sse",
      url: mcpUrl,
    },
  });

  const tools = await client.tools();

  const result = streamText({
    model: anthropic("claude-sonnet-4-5"),
    system: SYSTEM_PROMPT,
    messages,
    tools,
    maxSteps: 15,
  });

  return result.toDataStreamResponse();
}
