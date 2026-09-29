"""Neo4j graph integration for EDA jobs, failures, repairs and RAG evidence.

Neo4j is optional. Set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD to enable it.
All graph writes are best-effort so an unavailable Neo4j instance never breaks
an EDA run.
"""
from __future__ import annotations

import os
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - dependency is optional at import time
    GraphDatabase = None


class EdaGraph:
    def __init__(self) -> None:
        self.uri = os.getenv("NEO4J_URI", "")
        self.username = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j"))
        self.password = os.getenv("NEO4J_PASSWORD", "")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.enabled = bool(self.uri and self.password and GraphDatabase)
        self.driver = None
        if self.enabled:
            try:
                self.driver = GraphDatabase.driver(self.uri, auth=(self.username, self.password))
            except Exception:
                self.driver = None
                self.enabled = False

    def close(self) -> None:
        if self.driver:
            self.driver.close()

    def health(self) -> dict[str, Any]:
        if not self.enabled or not self.driver:
            return {"enabled": False, "connected": False}
        try:
            with self.driver.session(database=self.database) as session:
                session.run("RETURN 1 AS ok").single()
            return {"enabled": True, "connected": True}
        except Exception as exc:
            return {"enabled": True, "connected": False, "error": str(exc)}

    def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        if not self.enabled or not self.driver:
            return []
        try:
            with self.driver.session(database=self.database) as session:
                return [dict(r) for r in session.run(query, **params)]
        except Exception:
            return []

    def init_schema(self) -> None:
        self._run("CREATE INDEX job_id_index IF NOT EXISTS FOR (j:Job) ON (j.job_id)")
        self._run("CREATE INDEX artifact_path_index IF NOT EXISTS FOR (a:Artifact) ON (a.path)")
        self._run("CREATE INDEX failure_id_index IF NOT EXISTS FOR (f:Failure) ON (f.id)")
        self._run("CREATE INDEX repair_id_index IF NOT EXISTS FOR (r:Repair) ON (r.id)")

    def index_job(self, job_id: str, prompt: str, status: str) -> None:
        self._run(
            """
            MERGE (j:Job {job_id:$job_id})
            SET j.prompt=$prompt, j.status=$status, j.updated_at=datetime()
            """,
            job_id=job_id, prompt=prompt, status=status,
        )

    def index_failure(self, job_id: str, stage: str, rag_id: int | None,
                      diagnosis: dict[str, Any], log: str) -> None:
        if rag_id is None:
            return
        failure_id = f"rag-failure-{rag_id}"
        self._run(
            """
            MERGE (j:Job {job_id:$job_id})
            MERGE (f:Failure {id:$fid})
            SET f.stage=$stage, f.failure_type=$failure_type,
                f.artifact=$artifact, f.diagnosis=$diagnosis,
                f.log=$log, f.created_at=datetime()
            MERGE (j)-[:HAS_FAILURE]->(f)
            """,
            job_id=job_id, fid=failure_id, stage=stage,
            failure_type=str(diagnosis.get("failure_type") or diagnosis.get("target") or stage),
            artifact=str(diagnosis.get("artifact") or ""),
            diagnosis=str(diagnosis), log=(log or "")[-12000:],
        )

    def index_repair(self, job_id: str, stage: str, rag_id: int | None,
                     attempt: int, artifact: str, outcome: str,
                     patch: str, rationale: str, failure_rag_id: int | None = None) -> None:
        if rag_id is None:
            return
        repair_id = f"rag-repair-{rag_id}"
        query = """
            MERGE (j:Job {job_id:$job_id})
            MERGE (r:Repair {id:$rid})
            SET r.stage=$stage, r.attempt=$attempt, r.artifact=$artifact,
                r.outcome=$outcome, r.patch=$patch, r.rationale=$rationale,
                r.created_at=datetime()
            MERGE (j)-[:HAS_REPAIR]->(r)
            MERGE (a:Artifact {path:$artifact})
            MERGE (r)-[:MODIFIES]->(a)
        """
        self._run(query, job_id=job_id, rid=repair_id, stage=stage,
                  attempt=attempt, artifact=artifact, outcome=outcome,
                  patch=(patch or "")[-12000:], rationale=rationale or "")
        if failure_rag_id is not None:
            self._run(
                """
                MATCH (f:Failure {id:$fid}), (r:Repair {id:$rid})
                MERGE (f)-[:LED_TO]->(r)
                """,
                fid=f"rag-failure-{failure_rag_id}", rid=repair_id,
            )

    def index_rag_evidence(self, job_id: str, repair_rag_id: int | None,
                           evidence: list[dict[str, Any]]) -> None:
        if repair_rag_id is None:
            return
        rid = f"rag-repair-{repair_rag_id}"
        for item in evidence:
            eid = f"rag-doc-{item.get('id')}"
            self._run(
                """
                MERGE (r:Repair {id:$rid})
                MERGE (e:Evidence {id:$eid})
                SET e.kind=$kind, e.title=$title, e.outcome=$outcome,
                    e.score=$score, e.content=$content
                MERGE (r)-[:USED_EVIDENCE]->(e)
                """,
                rid=rid, eid=eid, kind=item.get("kind", ""),
                title=item.get("title", ""), outcome=item.get("outcome", ""),
                score=float(item.get("score", 0) or 0), content=item.get("content", "")[-8000:],
            )

    def get_job_graph(self, job_id: str) -> dict[str, Any]:
        rows = self._run(
            """
            MATCH (j:Job {job_id:$job_id})
            OPTIONAL MATCH (j)-[rel]->(n)
            RETURN j, collect({type:type(rel), node:n}) AS related
            """, job_id=job_id,
        )
        if not rows:
            return {"job": None, "related": []}
        row = rows[0]
        job = dict(row["j"])
        related = []
        for item in row["related"]:
            node = item.get("node")
            if node is not None:
                related.append({"relationship": item.get("type"), "node": dict(node)})
        return {"job": job, "related": related}

    def list_failures(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._run(
            """
            MATCH (j:Job {job_id:$job_id})-[:HAS_FAILURE]->(f:Failure)
            RETURN f ORDER BY f.created_at DESC LIMIT $limit
            """, job_id=job_id, limit=limit,
        )

    def list_repairs(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._run(
            """
            MATCH (j:Job {job_id:$job_id})-[:HAS_REPAIR]->(r:Repair)
            RETURN r ORDER BY r.created_at DESC LIMIT $limit
            """, job_id=job_id, limit=limit,
        )

    def find_similar_failures(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        # Full-text indexes are optional; this fallback uses CONTAINS so the
        # GraphQL API remains useful on a fresh Neo4j database.
        terms = [t.lower() for t in (text or "").split() if len(t) > 3][:8]
        if not terms:
            return []
        clauses = " OR ".join("toLower(coalesce(f.log,'')) CONTAINS $t%d" % i for i in range(len(terms)))
        params = {f"t{i}": t for i, t in enumerate(terms)}
        params["limit"] = limit
        return self._run(
            f"MATCH (f:Failure) WHERE {clauses} RETURN f LIMIT $limit", **params
        )
