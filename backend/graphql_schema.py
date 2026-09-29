"""GraphQL API over the Neo4j EDA knowledge graph."""
from __future__ import annotations

from typing import Any

import strawberry
from strawberry.types import Info


def _props(value: Any) -> dict[str, Any]:
    try:
        return dict(value)
    except Exception:
        return {}


@strawberry.type
class GraphNode:
    labels: list[str]
    properties: strawberry.scalars.JSON


@strawberry.type
class GraphRelationship:
    relationship: str
    node: GraphNode


@strawberry.type
class JobGraph:
    job: GraphNode | None
    related: list[GraphRelationship]


@strawberry.type
class Failure:
    id: str
    stage: str | None
    failure_type: str | None
    artifact: str | None
    diagnosis: str | None
    log: str | None


@strawberry.type
class Repair:
    id: str
    stage: str | None
    attempt: int | None
    artifact: str | None
    outcome: str | None
    patch: str | None
    rationale: str | None


def _node(record: Any) -> GraphNode:
    labels = list(record.labels) if hasattr(record, "labels") else []
    return GraphNode(labels=labels, properties=_props(record))


def _failure(record: Any) -> Failure:
    p = _props(record)
    return Failure(id=str(p.get("id", "")), stage=p.get("stage"), failure_type=p.get("failure_type"),
                   artifact=p.get("artifact"), diagnosis=p.get("diagnosis"), log=p.get("log"))


def _repair(record: Any) -> Repair:
    p = _props(record)
    return Repair(id=str(p.get("id", "")), stage=p.get("stage"), attempt=p.get("attempt"),
                  artifact=p.get("artifact"), outcome=p.get("outcome"), patch=p.get("patch"),
                  rationale=p.get("rationale"))


@strawberry.type
class Query:
    @strawberry.field
    def graph_health(self, info: Info) -> strawberry.scalars.JSON:
        return info.context["graph"].health()

    @strawberry.field
    def job_graph(self, info: Info, job_id: str) -> JobGraph:
        result = info.context["graph"].get_job_graph(job_id)
        job = result.get("job")
        return JobGraph(
            job=GraphNode(labels=["Job"], properties=job or {}) if job else None,
            related=[GraphRelationship(relationship=x["relationship"], node=GraphNode(labels=[], properties=x["node"]))
                     for x in result.get("related", [])],
        )

    @strawberry.field
    def failures(self, info: Info, job_id: str, limit: int = 50) -> list[Failure]:
        return [_failure(x.get("f")) for x in info.context["graph"].list_failures(job_id, min(limit, 200))]

    @strawberry.field
    def repairs(self, info: Info, job_id: str, limit: int = 50) -> list[Repair]:
        return [_repair(x.get("r")) for x in info.context["graph"].list_repairs(job_id, min(limit, 200))]

    @strawberry.field
    def similar_failures(self, info: Info, text: str, limit: int = 10) -> list[Failure]:
        return [_failure(x.get("f")) for x in info.context["graph"].find_similar_failures(text, min(limit, 50))]


schema = strawberry.Schema(query=Query)
