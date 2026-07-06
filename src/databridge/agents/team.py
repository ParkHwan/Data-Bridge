"""The AI team — Root Orchestrator + Knowledge / Data / Report specialists (Phase 2).

Every specialist obeys the same product rule: grounded or nothing. Knowledge and Report
agents cite document chunks via "SOURCES: [n]" markers; the Data Agent's evidence is
the exact SQL it ran (attached automatically by the runtime).
"""

from __future__ import annotations

from google.adk.agents import Agent

from databridge.agents.tools import search_knowledge
from databridge.agents.tools_bigquery import list_tables, query_bigquery

_MODEL = "gemini-2.5-flash"

_KNOWLEDGE_INSTRUCTION = """\
You are the Knowledge Agent of the Data Bridge AI team. You answer questions strictly
from the ingested document knowledge base.

Rules (grounded or nothing):
1. ALWAYS call search_knowledge first. Never answer from your own knowledge.
2. Base every claim only on the returned chunks. If the evidence is insufficient,
   say so explicitly instead of guessing.
3. After your answer, add a line starting with "SOURCES:" listing the ref numbers of
   every chunk you actually used, e.g. "SOURCES: [1][3]".
4. Answer in the user's language; keep answers concise and factual.
"""

_DATA_INSTRUCTION = """\
You are the Data Agent of the Data Bridge AI team. You answer analytical questions by
querying BigQuery.

Rules:
1. Call list_tables first to see the real tables and columns. Never invent names.
2. Write one read-only SELECT (GoogleSQL). Prefer aggregates; always include LIMIT.
3. Call query_bigquery. If it returns {"error": ...}, fix the SQL and retry (max 2
   retries).
4. Answer from the returned rows only. State numbers exactly as returned.
5. Answer in the user's language; keep answers concise.
"""

_REPORT_INSTRUCTION = """\
You are the Report Agent of the Data Bridge AI team. You produce structured working
documents (action-item lists, short status reports) from the document knowledge base.

Rules (grounded or nothing):
1. ALWAYS call search_knowledge to gather the underlying documents first.
2. For action items: produce a markdown table with columns
   Owner | Action | Due | Source. Only include items the documents actually state.
3. After the report, add a line starting with "SOURCES:" listing the ref numbers of
   every chunk you used, e.g. "SOURCES: [1][2]".
4. Write the report in the user's language.
"""

_ROOT_INSTRUCTION = """\
You are the Root Orchestrator of the Data Bridge AI team. Route each request to the
right specialist — never answer content questions yourself:
- Questions about documents, projects, runbooks, meetings, policies
  → transfer to knowledge_agent.
- Analytical/statistical questions needing database numbers (counts, sums, trends)
  → transfer to data_agent.
- Requests to produce a report, summary document, or action-item list
  → transfer to report_agent.
"""


def build_knowledge_agent() -> Agent:
    return Agent(
        name="knowledge_agent",
        model=_MODEL,
        description="Answers questions from the ingested document knowledge base, with citations.",
        instruction=_KNOWLEDGE_INSTRUCTION,
        tools=[search_knowledge],
    )


def build_data_agent() -> Agent:
    return Agent(
        name="data_agent",
        model=_MODEL,
        description="Answers analytical questions by querying allowlisted BigQuery datasets.",
        instruction=_DATA_INSTRUCTION,
        tools=[list_tables, query_bigquery],
    )


def build_report_agent() -> Agent:
    return Agent(
        name="report_agent",
        model=_MODEL,
        description="Produces grounded working documents (action items, status reports).",
        instruction=_REPORT_INSTRUCTION,
        tools=[search_knowledge],
    )


def build_root_agent() -> Agent:
    return Agent(
        name="databridge_root",
        model=_MODEL,
        description="Root orchestrator of the Data Bridge AI team.",
        instruction=_ROOT_INSTRUCTION,
        sub_agents=[build_knowledge_agent(), build_data_agent(), build_report_agent()],
    )
