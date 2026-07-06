"""The AI team — Knowledge Agent under a Root Orchestrator shell.

Phase 1 registers a single specialist; the root exists from day one so the multi-agent
architecture is real, not aspirational (design §4). Phase 2 adds Data and Report agents
as siblings.
"""

from __future__ import annotations

from google.adk.agents import Agent

from databridge.agents.tools import search_knowledge

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

_ROOT_INSTRUCTION = """\
You are the Root Orchestrator of the Data Bridge AI team. Route every knowledge
question about documents, projects, runbooks, meetings, or policies to the
knowledge_agent. Do not answer content questions yourself.
"""


def build_knowledge_agent() -> Agent:
    return Agent(
        name="knowledge_agent",
        model=_MODEL,
        description="Answers questions from the ingested document knowledge base, with citations.",
        instruction=_KNOWLEDGE_INSTRUCTION,
        tools=[search_knowledge],
    )


def build_root_agent() -> Agent:
    return Agent(
        name="databridge_root",
        model=_MODEL,
        description="Root orchestrator of the Data Bridge AI team.",
        instruction=_ROOT_INSTRUCTION,
        sub_agents=[build_knowledge_agent()],
    )
