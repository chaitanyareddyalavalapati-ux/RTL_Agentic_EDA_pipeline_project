import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from backend.db import get_job, list_jobs
from backend.workflow.engine import WorkflowEngine

router = APIRouter(prefix='/api')
jobs: dict[str, WorkflowEngine] = {}
pipeline_tasks: dict[str, asyncio.Task] = {}

def _discover_artifacts(snapshot: dict) -> dict:
    """Expose tool outputs created after the workflow registered its source files."""
    workspace_value = snapshot.get('workspace') or (
        f"runs/{snapshot.get('job_id')}" if snapshot.get('job_id') else None
    )
    if not workspace_value:
        return snapshot
    workspace = Path(workspace_value)
    artifacts = dict(snapshot.get('artifacts') or {})
    if workspace.is_dir():
        for path in workspace.rglob('*'):
            if path.is_file() and path.name not in {'eda_pipeline.db'}:
                artifacts.setdefault(str(path.relative_to(workspace)), str(path))
    snapshot['artifacts'] = artifacts
    return snapshot

class DesignRequest(BaseModel):
    prompt: str

@router.post('/design')
async def create_design(request: DesignRequest):
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(422, 'Prompt must not be empty')
    job_id = str(uuid4())
    engine = WorkflowEngine(job_id)
    # Persist before scheduling the background task. This removes the WebSocket race.
    engine.state.prompt = prompt
    engine.state.status = 'created'
    engine.workspace.mkdir(parents=True, exist_ok=True)
    engine.persist()
    jobs[job_id] = engine
    task = asyncio.create_task(engine.run(prompt), name=f'pipeline-{job_id}')
    pipeline_tasks[job_id] = task
    task.add_done_callback(lambda _task, pipeline_id=job_id: pipeline_tasks.pop(pipeline_id, None))
    return {'job_id': job_id, 'status': 'started'}

@router.get('/designs')
async def get_designs(limit: int = Query(50, ge=1, le=200)):
    return {'jobs': list_jobs(limit)}

@router.get('/design/{job_id}')
async def get_design(job_id: str):
    if job_id in jobs:
        return _discover_artifacts(jobs[job_id].snapshot())
    snapshot = get_job(job_id)
    if snapshot is None:
        raise HTTPException(404, 'Job not found')
    return _discover_artifacts(snapshot)

@router.get('/design/{job_id}/artifact')
async def artifact(job_id: str, path: str = Query(...)):
    # Only serve paths explicitly registered by the workflow.
    snapshot = jobs[job_id].snapshot() if job_id in jobs else get_job(job_id)
    if snapshot is None:
        raise HTTPException(404, 'Job not found')
    snapshot = _discover_artifacts(snapshot)
    file_name = snapshot.get('artifacts', {}).get(path)
    if not file_name:
        raise HTTPException(404, 'Artifact not available yet')
    file_path = Path(file_name).resolve()
    workspace = (Path(snapshot.get('workspace', '')) if snapshot.get('workspace') else None)
    if not file_path.is_file():
        raise HTTPException(404, 'Artifact file is missing')
    if file_path.suffix.lower() == '.png':
        return FileResponse(file_path, media_type='image/png', filename=file_path.name)
    if file_path.suffix.lower() == '.svg':
        return FileResponse(file_path, media_type='image/svg+xml', filename=file_path.name)
    return PlainTextResponse(
        file_path.read_text(encoding='utf-8', errors='replace'),
        media_type='text/plain; charset=utf-8',
    )

@router.get('/graph/health')
async def graph_health():
    from backend.graph import EdaGraph
    return EdaGraph().health()

@router.get('/graph/{job_id}')
async def graph_job(job_id: str):
    from backend.graph import EdaGraph
    return EdaGraph().get_job_graph(job_id)


@router.get('/rag/health')
async def rag_health():
    """Report SQLite and Qdrant RAG backend status."""
    from backend.rag import EdaRAG
    return EdaRAG().health()


@router.post('/rag/reindex')
async def rag_reindex():
    """Backfill/rebuild Qdrant vectors from SQLite RAG documents."""
    from backend.rag import EdaRAG
    rag = EdaRAG()
    count = rag.reindex_vectors()
    return {'indexed_documents': count, 'health': rag.health()}
