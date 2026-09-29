from fastapi import WebSocket, WebSocketDisconnect

from backend.db import get_events, get_job
from backend.workflow.events import event_bus


async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()

    if get_job(job_id) is None:
        await websocket.send_json({'type': 'job_not_found', 'job_id': job_id})
        await websocket.close(code=1008)
        return

    q = event_bus.subscribe(job_id)
    last_event_id = 0
    try:
        # Replay events already persisted in SQLite so refresh/reconnect does not
        # leave the dashboard empty.
        for event in get_events(job_id):
            last_event_id = max(last_event_id, int(event.get('event_id', 0)))
            await websocket.send_json(event)

        snapshot = get_job(job_id)
        if snapshot:
            await websocket.send_json({'type': 'state_snapshot', **snapshot})

        while True:
            event = await q.get()
            event_id = int(event.get('event_id', 0))
            if event_id and event_id <= last_event_id:
                continue
            last_event_id = max(last_event_id, event_id)
            await websocket.send_json(event)
            if event['type'] in ('pipeline_completed', 'pipeline_failed'):
                break
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(job_id, q)
