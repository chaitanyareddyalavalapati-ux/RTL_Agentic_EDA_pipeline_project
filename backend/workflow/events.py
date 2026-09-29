import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from backend.db import add_event


class EventBus:
    def __init__(self):
        self.queues = defaultdict(list)

    def subscribe(self, job_id):
        q = asyncio.Queue()
        self.queues[job_id].append(q)
        return q

    def unsubscribe(self, job_id, q):
        if q in self.queues[job_id]:
            self.queues[job_id].remove(q)

    async def emit(self, job_id, event_type, **data):
        event = {
            'type': event_type,
            'job_id': job_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **data,
        }
        event['event_id'] = add_event(job_id, event, event['timestamp'])
        for q in list(self.queues[job_id]):
            await q.put(event)


event_bus = EventBus()
