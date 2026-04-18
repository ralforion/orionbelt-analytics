"""Example: OrionBelt Analytics crew using CrewAI with MCP.

Prerequisites:
    pip install crewai

Start OrionBelt Analytics MCP server first:
    uv run server.py

Then run this script:
    export OPENAI_API_KEY=sk-...
    python crew_example.py
"""

from __future__ import annotations

from crewai import Agent, Crew, Task

MCP_SERVER_CONFIG = {
    "url": "http://localhost:9000/mcp",
    "transport": "streamable_http",
}

# Agent 1: Schema Analyst — connects, analyzes, and generates ontologies
schema_analyst = Agent(
    role="Schema Analyst",
    goal="Connect to databases, analyze schemas, and generate RDF/OWL ontologies.",
    backstory=(
        "You are a senior data architect with expertise in database schema analysis "
        "and semantic modeling. You use OrionBelt Analytics tools to connect to databases, "
        "analyze table structures and relationships, and generate ontologies that map "
        "business concepts to SQL tables. You always start by connecting and listing schemas."
    ),
    mcps=[MCP_SERVER_CONFIG],
    verbose=True,
)

# Agent 2: Query Analyst — writes and executes validated SQL
query_analyst = Agent(
    role="Query Analyst",
    goal="Write safe SQL queries based on schema analysis, validate them, and execute.",
    backstory=(
        "You are a SQL expert who writes precise queries based on schema information. "
        "You always check for fan-trap risks before execution. "
        "You use sample_table_data to preview data before "
        "calling execute_sql_query with checklist_completed=true (validation is built-in)."
    ),
    mcps=[MCP_SERVER_CONFIG],
    verbose=True,
)

# Agent 3: Report Writer — formats the results into a readable report
report_writer = Agent(
    role="Report Writer",
    goal="Write clear, concise data analysis reports based on schema analysis and query results.",
    backstory=(
        "You are a technical writer who translates database analysis results "
        "into readable reports for business stakeholders. You explain table "
        "relationships, ontology mappings, and query results in plain language."
    ),
    verbose=True,
)

# Task 1: Analyze the database schema
analyze_task = Task(
    description=(
        "1. Connect to PostgreSQL using connect_database.\n"
        "2. List available schemas using list_schemas.\n"
        "3. Analyze the 'public' schema using analyze_schema.\n"
        "4. Get details for the most important tables.\n"
        "5. Generate an ontology for the schema."
    ),
    expected_output=(
        "A summary of the schema structure including tables, columns, "
        "relationships, and the generated ontology description."
    ),
    agent=schema_analyst,
)

# Task 2: Query the database
query_task = Task(
    description=(
        "Based on the schema analysis:\n"
        "1. Write a SQL query to show the top 10 most populated tables.\n"
        "2. Execute the query using execute_sql_query with checklist_completed=true.\n"
        "4. Sample data from the largest table."
    ),
    expected_output=(
        "The validated SQL query, execution results, and a sample of data "
        "from the largest table."
    ),
    agent=query_analyst,
    context=[analyze_task],
)

# Task 3: Write a report
report_task = Task(
    description=(
        "Based on the schema analysis and query results, write a short report that:\n"
        "1. Summarizes the database structure and key tables.\n"
        "2. Describes the table relationships found.\n"
        "3. Shows the query results and data samples.\n"
        "4. Recommends areas for further analysis."
    ),
    expected_output="A formatted markdown report summarizing the database analysis.",
    agent=report_writer,
    context=[analyze_task, query_task],
)

crew = Crew(
    agents=[schema_analyst, query_analyst, report_writer],
    tasks=[analyze_task, query_task, report_task],
    verbose=True,
)

if __name__ == "__main__":
    result = crew.kickoff()
    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    print(result)
