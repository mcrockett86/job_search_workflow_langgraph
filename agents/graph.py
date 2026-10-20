"""State definitions and LangGraph graph construction."""

from __future__ import annotations

import logging
from typing import Any, Optional

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class JobState(TypedDict):
    """State for the job-processing graph."""

    id_run: str
    job_url: dict[str, Any]
    job_description: Optional[str]
    is_match: Optional[bool]
    company_name: Optional[str]
    job_title: Optional[str]
    job_type: Optional[str]
    salary_min: Optional[float]
    salary_max: Optional[float]
    messages: Annotated[list[AnyMessage], add_messages]


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------
async def route_job(state: JobState) -> str:
    """Decide whether the job is a match or not."""
    return "match" if state["is_match"] else "not_match"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def create_job_graph() -> StateGraph:  # noqa: PLR0915 — graph wiring only
    """Build and compile the job-processing LangGraph.

    Returns a compiled ``StateGraph`` ready to be invoked via
    :meth:`~langgraph.graph.state.CompiledGraph.ainvoke`.
    """
    builder = StateGraph(JobState)

    # Nodes
    from agents.nodes import (  # local import — avoids circular imports at module level
        read_job_description,
        classify_job_description,
        extract_job_details,
        handle_bad_fit,
        handle_good_fit,
    )

    builder.add_node("read_job_description", read_job_description)
    builder.add_node("classify_job_description", classify_job_description)
    builder.add_node("extract_job_details", extract_job_details)
    builder.add_node("handle_bad_fit", handle_bad_fit)
    builder.add_node("handle_good_fit", handle_good_fit)

    # Edges
    builder.add_edge(START, "read_job_description")
    builder.add_edge("read_job_description", "classify_job_description")

    builder.add_conditional_edges(
        "classify_job_description",
        route_job,
        {
            "not_match": "handle_bad_fit",
            "match": "handle_good_fit",
        },
    )

    builder.add_edge("handle_bad_fit", END)
    builder.add_edge("handle_good_fit", "extract_job_details")
    builder.add_edge("extract_job_details", END)

    checkpointer = InMemorySaver()
    graph = builder.compile(checkpointer=checkpointer)

    # Save a PNG of the graph for debugging.
    mermaid_png_data = graph.get_graph().draw_mermaid_png()
    with open("job_graph.png", "wb") as f:  # noqa: PTH123 — fixed output path
        f.write(mermaid_png_data)

    return graph
