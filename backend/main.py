from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from backend.db import init_db, recover_interrupted_jobs
from backend.api.routes import router
from backend.api.websocket import websocket_endpoint
from backend.graph import EdaGraph
from backend.graphql_schema import schema
from strawberry.fastapi import GraphQLRouter

init_db()
recover_interrupted_jobs()

graph = EdaGraph()
graph.init_schema()

app = FastAPI(title='RTL Agentic EDA', version='1.1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.include_router(router)
app.include_router(GraphQLRouter(schema, context_getter=lambda: {'graph': graph}), prefix='/graphql')
WEB = Path(__file__).resolve().parent / 'web'

@app.get('/', include_in_schema=False)
async def index():
    return FileResponse(WEB / 'index.html')

app.mount('/static', StaticFiles(directory=WEB), name='static')

@app.websocket('/ws/{job_id}')
async def ws(websocket: WebSocket, job_id: str):
    await websocket_endpoint(websocket, job_id)

@app.get('/health')
async def health():
    return {'status': 'ok', 'database': 'sqlite', 'neo4j': graph.health(), 'graphql': '/graphql'}
