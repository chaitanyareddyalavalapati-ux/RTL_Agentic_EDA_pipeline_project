"""Hybrid RAG for EDA failures and repair experience.

SQLite remains the system of record and lexical index (FTS5). Qdrant stores
semantic embeddings for the same documents. Retrieval combines:
  1) structured metadata filtering,
  2) SQLite FTS5 lexical retrieval,
  3) Qdrant vector similarity,
  4) lightweight token-overlap + metadata/outcome reranking.

Qdrant and the embedding model are optional at runtime. If the vector service or
embedding package/model is unavailable, the RAG gracefully falls back to FTS5,
so the EDA pipeline does not become dependent on the vector service.

Every observed failure and every repair outcome becomes searchable evidence.
Successful, rejected, validation-failed and rolled-back repairs are retained;
negative repair experience is deliberately treated as useful memory.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import (
    RAG_EMBEDDING_MODEL,
    RAG_VECTOR_ENABLED,
    QDRANT_COLLECTION,
    QDRANT_URL,
    QDRANT_API_KEY,
)
from backend.db import DB_PATH

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]{1,}|\b\d+\b")
_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "stage", "error",
    "failed", "failure", "the", "was", "are", "into", "during", "then",
}


def _tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOP
    }


class EdaRAG:
    """Persistent hybrid RAG with SQLite + Qdrant.

    The public API intentionally keeps the original project's method names so
    the workflow engine does not need to know which retrieval backend is used.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._vector_client = None
        self._embedder = None
        self._vector_ready = False
        self._vector_warning_logged = False
        self.init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    kind TEXT NOT NULL,
                    stage TEXT,
                    tool TEXT,
                    artifact TEXT,
                    failure_type TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    outcome TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rag_stage ON rag_documents(stage);
                CREATE INDEX IF NOT EXISTS idx_rag_failure_type ON rag_documents(failure_type);
                CREATE INDEX IF NOT EXISTS idx_rag_artifact ON rag_documents(artifact);
                CREATE INDEX IF NOT EXISTS idx_rag_tool ON rag_documents(tool);

                CREATE VIRTUAL TABLE IF NOT EXISTS rag_documents_fts USING fts5(
                    title, content, failure_type, stage, tool, artifact,
                    content='rag_documents', content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS rag_ai AFTER INSERT ON rag_documents BEGIN
                    INSERT INTO rag_documents_fts(
                        rowid,title,content,failure_type,stage,tool,artifact
                    )
                    VALUES (
                        new.id,new.title,new.content,new.failure_type,
                        new.stage,new.tool,new.artifact
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS rag_ad AFTER DELETE ON rag_documents BEGIN
                    INSERT INTO rag_documents_fts(
                        rag_documents_fts,rowid,title,content,
                        failure_type,stage,tool,artifact
                    )
                    VALUES (
                        'delete',old.id,old.title,old.content,
                        old.failure_type,old.stage,old.tool,old.artifact
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS rag_au AFTER UPDATE ON rag_documents BEGIN
                    INSERT INTO rag_documents_fts(
                        rag_documents_fts,rowid,title,content,
                        failure_type,stage,tool,artifact
                    )
                    VALUES (
                        'delete',old.id,old.title,old.content,
                        old.failure_type,old.stage,old.tool,old.artifact
                    );
                    INSERT INTO rag_documents_fts(
                        rowid,title,content,failure_type,stage,tool,artifact
                    )
                    VALUES (
                        new.id,new.title,new.content,new.failure_type,
                        new.stage,new.tool,new.artifact
                    );
                END;
                """
            )

        # Qdrant/embedding initialization is lazy. The local EDA app can start
        # even when Docker/Qdrant is not running yet.

    # ------------------------------------------------------------------
    # Vector store
    # ------------------------------------------------------------------

    def _ensure_vector_store(self) -> bool:
        if self._vector_ready:
            return True
        if not RAG_VECTOR_ENABLED:
            return False

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models

            self._vector_client = QdrantClient(
                url=QDRANT_URL,
                api_key=QDRANT_API_KEY or None,
                timeout=5,
            )

            # Verify the Qdrant service before downloading/loading the embedding
            # model. This keeps the fallback path fast when Qdrant is offline.
            collections = self._vector_client.get_collections().collections
            self._load_embedder()
            vector_size = int(self._embedder.get_sentence_embedding_dimension())
            names = {c.name for c in collections}
            if QDRANT_COLLECTION not in names:
                self._vector_client.create_collection(
                    collection_name=QDRANT_COLLECTION,
                    vectors_config=models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    ),
                )

            self._vector_ready = True
            return True
        except Exception as exc:
            if not self._vector_warning_logged:
                logger.warning(
                    "Vector RAG unavailable; using SQLite FTS5 fallback: %s",
                    exc,
                )
                self._vector_warning_logged = True
            self._vector_client = None
            self._embedder = None
            return False

    def _load_embedder(self):
        if self._embedder is not None:
            return self._embedder
        from sentence_transformers import SentenceTransformer

        self._embedder = SentenceTransformer(RAG_EMBEDDING_MODEL)
        return self._embedder

    def _embedding_text(
        self,
        *,
        kind: str,
        stage: str | None,
        tool: str,
        artifact: str,
        failure_type: str,
        title: str,
        content: str,
    ) -> str:
        # Do not embed huge raw logs as the whole representation. Metadata and
        # the diagnostic/repair narrative are more stable retrieval signals.
        compact = (content or "")[-10000:]
        return (
            f"Kind: {kind}\n"
            f"Stage: {stage or ''}\n"
            f"Tool: {tool}\n"
            f"Artifact: {artifact}\n"
            f"Failure type: {failure_type}\n"
            f"Title: {title}\n"
            f"Experience:\n{compact}"
        )

    def _upsert_vector(self, document_id: int, *, kind: str, stage: str | None,
                       tool: str, artifact: str, failure_type: str,
                       title: str, content: str, outcome: str,
                       metadata: dict[str, Any] | None = None,
                       created_at: str = "") -> bool:
        if not self._ensure_vector_store():
            return False

        try:
            from qdrant_client.http import models

            text = self._embedding_text(
                kind=kind, stage=stage, tool=tool, artifact=artifact,
                failure_type=failure_type, title=title, content=content,
            )
            vector = self._load_embedder().encode(
                text, normalize_embeddings=True
            ).tolist()

            payload = {
                "document_id": document_id,
                "kind": kind,
                "stage": stage or "",
                "tool": tool or "",
                "artifact": artifact or "",
                "failure_type": failure_type or "",
                "title": title or "",
                "outcome": outcome or "",
                "created_at": created_at,
                "metadata": metadata or {},
            }
            self._vector_client.upsert(
                collection_name=QDRANT_COLLECTION,
                points=[
                    models.PointStruct(
                        id=document_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
                wait=True,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to index RAG document %s in Qdrant: %s", document_id, exc)
            return False

    def reindex_vectors(self, batch_size: int = 32) -> int:
        """Backfill Qdrant from existing SQLite RAG documents.

        Safe to run repeatedly because Qdrant points use the SQLite document id.
        """
        if not self._ensure_vector_store():
            return 0

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rag_documents ORDER BY id"
            ).fetchall()

        if not rows:
            return 0

        count = 0
        embedder = self._load_embedder()
        from qdrant_client.http import models

        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            texts = [
                self._embedding_text(
                    kind=r["kind"], stage=r["stage"], tool=r["tool"] or "",
                    artifact=r["artifact"] or "",
                    failure_type=r["failure_type"] or "",
                    title=r["title"] or "", content=r["content"],
                )
                for r in batch
            ]
            vectors = embedder.encode(
                texts, batch_size=batch_size, normalize_embeddings=True
            ).tolist()

            points = []
            for row, vector in zip(batch, vectors):
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                points.append(
                    models.PointStruct(
                        id=int(row["id"]),
                        vector=vector,
                        payload={
                            "document_id": int(row["id"]),
                            "kind": row["kind"],
                            "stage": row["stage"] or "",
                            "tool": row["tool"] or "",
                            "artifact": row["artifact"] or "",
                            "failure_type": row["failure_type"] or "",
                            "title": row["title"] or "",
                            "outcome": row["outcome"] or "",
                            "created_at": row["created_at"],
                            "metadata": metadata,
                        },
                    )
                )
            self._vector_client.upsert(
                collection_name=QDRANT_COLLECTION,
                points=points,
                wait=True,
            )
            count += len(points)
        return count

    # ------------------------------------------------------------------
    # Document indexing
    # ------------------------------------------------------------------

    def add_document(
        self, *, job_id: str | None, kind: str, stage: str | None,
        content: str, title: str = "", tool: str = "", artifact: str = "",
        failure_type: str = "", metadata: dict[str, Any] | None = None,
        outcome: str = ""
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        metadata = metadata or {}
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO rag_documents
                (job_id,kind,stage,tool,artifact,failure_type,title,content,
                 metadata_json,outcome,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, kind, stage, tool, artifact, failure_type, title,
                    content, json.dumps(metadata), outcome, now,
                ),
            )
            document_id = int(cur.lastrowid)

        self._upsert_vector(
            document_id,
            kind=kind, stage=stage, tool=tool, artifact=artifact,
            failure_type=failure_type, title=title, content=content,
            outcome=outcome, metadata=metadata, created_at=now,
        )
        return document_id

    def index_failure(
        self, job_id: str, stage: str, log: str,
        diagnosis: dict[str, Any] | None = None
    ) -> int:
        diagnosis = diagnosis or {}
        failure_type = str(
            diagnosis.get("failure_type") or diagnosis.get("target") or stage
        )
        artifact = str(diagnosis.get("artifact") or "")
        tool = str(diagnosis.get("tool") or stage)
        diagnosis_text = json.dumps(diagnosis, sort_keys=True)
        content = (
            f"Stage: {stage}\n"
            f"Tool: {tool}\n"
            f"Failure type: {failure_type}\n"
            f"Artifact: {artifact}\n"
            f"Diagnosis: {diagnosis_text}\n"
            f"Log:\n{(log or '')[-12000:]}"
        )
        return self.add_document(
            job_id=job_id, kind="failure", stage=stage, tool=tool,
            artifact=artifact, failure_type=failure_type,
            title=f"{stage} failure: {failure_type}", content=content,
            metadata={"diagnosis": diagnosis}, outcome="failed",
        )

    def index_repair(
        self, job_id: str, stage: str, attempt: int,
        diagnosis: dict[str, Any], artifact: str, patch: str,
        rationale: str, outcome: str, runner_output: str = "",
        metrics: dict[str, Any] | None = None
    ) -> int:
        diagnosis = diagnosis or {}
        failure_type = str(
            diagnosis.get("failure_type") or diagnosis.get("target") or stage
        )
        tool = str(diagnosis.get("tool") or stage)
        content = (
            f"Stage: {stage}\n"
            f"Tool: {tool}\n"
            f"Failure type: {failure_type}\n"
            f"Artifact: {artifact}\n"
            f"Diagnosis: {json.dumps(diagnosis, sort_keys=True)}\n"
            f"Repair rationale: {rationale}\n"
            f"Outcome: {outcome}\n"
            f"Patch:\n{patch}\n"
            f"Runner output:\n{(runner_output or '')[-6000:]}\n"
            f"Metrics: {json.dumps(metrics or {}, sort_keys=True)}"
        )
        return self.add_document(
            job_id=job_id, kind="repair", stage=stage, tool=tool,
            artifact=artifact, failure_type=failure_type,
            title=f"{stage} repair: {failure_type} ({outcome})",
            content=content,
            metadata={
                "diagnosis": diagnosis,
                "attempt": attempt,
                "patch": patch,
                "rationale": rationale,
                "metrics": metrics or {},
            },
            outcome=outcome,
        )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_fts_query(terms: set[str]) -> str:
        return " OR ".join(
            '"' + t.replace('"', '""') + '"'
            for t in list(terms)[:32]
        )

    def _fts_search(
        self, terms: set[str], *, stage: str, artifact: str,
        tool: str, limit: int
    ) -> dict[int, tuple[sqlite3.Row, float]]:
        if not terms:
            return {}
        fts_query = self._safe_fts_query(terms)
        where = [
            "rag_documents_fts MATCH ?",
            "(?='' OR d.stage=? OR d.stage IS NULL)",
            "(?='' OR d.artifact=? OR d.artifact IS NULL)",
            "(?='' OR d.tool=? OR d.tool IS NULL)",
        ]
        params: list[Any] = [
            fts_query, stage, stage, artifact, artifact, tool, tool
        ]
        sql = f"""
            SELECT d.*, bm25(rag_documents_fts) AS rank
            FROM rag_documents_fts f
            JOIN rag_documents d ON d.id=f.rowid
            WHERE {' AND '.join(where)}
            ORDER BY rank
            LIMIT ?
        """
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = {}
        for row in rows:
            # SQLite BM25 is lower-is-better; convert it into a bounded signal.
            rank = float(row["rank"])
            bm25_signal = 1.0 / (1.0 + max(rank, 0.0))
            text = " ".join([row["title"] or "", row["content"] or ""])
            overlap = len(terms & _tokens(text)) / max(1, len(terms))
            lexical = min(1.0, 0.65 * overlap + 0.35 * bm25_signal)
            results[int(row["id"])] = (row, lexical)
        return results

    def _vector_search(
        self, query: str, *, stage: str, artifact: str,
        tool: str, limit: int
    ) -> dict[int, tuple[dict[str, Any], float]]:
        if not query or not self._ensure_vector_store():
            return {}

        try:
            from qdrant_client.http import models

            must = []
            if stage:
                must.append(
                    models.FieldCondition(
                        key="stage", match=models.MatchValue(value=stage)
                    )
                )
            if artifact:
                must.append(
                    models.FieldCondition(
                        key="artifact", match=models.MatchValue(value=artifact)
                    )
                )
            if tool:
                must.append(
                    models.FieldCondition(
                        key="tool", match=models.MatchValue(value=tool)
                    )
                )

            query_vector = self._load_embedder().encode(
                self._embedding_text(
                    kind="query", stage=stage, tool=tool,
                    artifact=artifact, failure_type="",
                    title="Current EDA failure", content=query,
                ),
                normalize_embeddings=True,
            ).tolist()

            query_filter = models.Filter(must=must) if must else None
            hits = self._vector_client.search(
                collection_name=QDRANT_COLLECTION,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=max(limit, 1),
                with_payload=True,
            )

            result = {}
            for hit in hits:
                payload = hit.payload or {}
                document_id = int(payload.get("document_id", hit.id))
                result[document_id] = (payload, float(hit.score))
            return result
        except Exception as exc:
            logger.warning("Qdrant search failed; using lexical RAG: %s", exc)
            return {}

    def search(
        self, query: str, *, stage: str = "", artifact: str = "",
        tool: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        terms = _tokens(query)
        if not terms:
            return []

        candidate_count = max(limit * 6, 20)
        lexical = self._fts_search(
            terms, stage=stage, artifact=artifact, tool=tool,
            limit=candidate_count,
        )

        # If a new Qdrant collection is empty but SQLite contains historical
        # documents, backfill it once. This makes upgrades transparent.
        vector = self._vector_search(
            query, stage=stage, artifact=artifact, tool=tool,
            limit=candidate_count,
        )
        if self._vector_ready and not vector:
            try:
                vector_count = self._vector_client.count(
                    collection_name=QDRANT_COLLECTION,
                    exact=True,
                ).count
                if vector_count == 0:
                    self.reindex_vectors()
                    vector = self._vector_search(
                        query, stage=stage, artifact=artifact, tool=tool,
                        limit=candidate_count,
                    )
            except Exception:
                pass

        candidate_ids = set(lexical) | set(vector)
        if not candidate_ids:
            return []

        rows_by_id: dict[int, sqlite3.Row] = {}
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM rag_documents WHERE id IN ({placeholders})",
                    list(candidate_ids),
                ).fetchall()
            rows_by_id = {int(row["id"]): row for row in rows}

        candidates = []
        for document_id in candidate_ids:
            row = rows_by_id.get(document_id)
            if row is None:
                continue

            lexical_score = lexical.get(document_id, (None, 0.0))[1]
            vector_payload, semantic_score = vector.get(
                document_id, ({}, 0.0)
            )
            text = " ".join([row["title"] or "", row["content"] or ""])
            overlap = len(terms & _tokens(text)) / max(1, len(terms))

            stage_score = 1.0 if stage and row["stage"] == stage else 0.0
            artifact_score = 1.0 if artifact and row["artifact"] == artifact else 0.0
            tool_score = 1.0 if tool and row["tool"] == tool else 0.0

            # Accepted repairs are useful evidence, but outcome is deliberately
            # a small feature rather than a dominant ranking signal.
            outcome_score = (
                1.0 if row["outcome"] == "accepted"
                else 0.45 if row["outcome"] == "validation_failed"
                else 0.30 if row["outcome"] == "rolled_back"
                else 0.20
            )

            # Hybrid score. Exact metadata is valuable in EDA; semantic
            # similarity is the main signal; lexical overlap catches tool
            # errors, identifiers and exact command names.
            score = (
                0.45 * float(semantic_score)
                + 0.25 * float(lexical_score)
                + 0.10 * overlap
                + 0.07 * stage_score
                + 0.05 * artifact_score
                + 0.04 * tool_score
                + 0.04 * outcome_score
            )

            candidates.append(
                (
                    score,
                    row,
                    float(semantic_score),
                    float(lexical_score),
                    vector_payload,
                )
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        result = []
        for score, row, semantic, lexical_score, vector_payload in candidates[:limit]:
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "stage": row["stage"],
                    "tool": row["tool"],
                    "artifact": row["artifact"],
                    "failure_type": row["failure_type"],
                    "outcome": row["outcome"],
                    "title": row["title"],
                    "content": row["content"][-8000:],
                    "score": round(score, 4),
                    "semantic_score": round(semantic, 4),
                    "lexical_score": round(lexical_score, 4),
                    "retrieval_backend": (
                        "hybrid" if vector else "fts5"
                    ),
                }
            )
        return result

    def format_context(
        self, query: str, *, stage: str = "", artifact: str = "",
        tool: str = "", limit: int = 5
    ) -> str:
        hits = self.search(
            query, stage=stage, artifact=artifact, tool=tool, limit=limit
        )
        if not hits:
            return "No relevant historical RAG evidence was found."

        parts = [
            "RETRIEVED EDA EXPERIENCE "
            "(evidence only; validate against the current artifact and tools):"
        ]
        for i, hit in enumerate(hits, 1):
            parts.append(
                f"\n--- Evidence {i} | {hit['kind']} | {hit['title']} "
                f"| outcome={hit['outcome']} | score={hit['score']} "
                f"| semantic={hit['semantic_score']} "
                f"| lexical={hit['lexical_score']} ---\n"
                f"{hit['content']}"
            )
        return "\n".join(parts)

    def health(self) -> dict[str, Any]:
        """Return RAG backend status for debugging/operations."""
        vector_count = None
        vector_error = None
        if self._ensure_vector_store():
            try:
                vector_count = self._vector_client.count(
                    collection_name=QDRANT_COLLECTION, exact=True
                ).count
            except Exception as exc:
                vector_error = str(exc)

        with self._connect() as conn:
            sqlite_count = conn.execute(
                "SELECT COUNT(*) FROM rag_documents"
            ).fetchone()[0]

        return {
            "sqlite_documents": sqlite_count,
            "vector_enabled": RAG_VECTOR_ENABLED,
            "qdrant_url": QDRANT_URL if RAG_VECTOR_ENABLED else "",
            "qdrant_collection": QDRANT_COLLECTION if RAG_VECTOR_ENABLED else "",
            "qdrant_documents": vector_count,
            "qdrant_error": vector_error,
            "embedding_model": RAG_EMBEDDING_MODEL if RAG_VECTOR_ENABLED else "",
            "backend": "hybrid" if self._vector_ready else "fts5",
        }
