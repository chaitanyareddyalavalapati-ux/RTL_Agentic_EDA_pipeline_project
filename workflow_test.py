"""Small command-line smoke test for specification and workflow boundaries."""

import asyncio
import json
import os
from pathlib import Path

from backend.agent.coder import Coder
from backend.agent.planner import Planner
from backend.workflow.engine import WorkflowEngine


async def main():
    planner = Planner()
    coder = Coder()
    requests = [
        "Create a 6-bit RAM and run the complete EDA flow.",
        "Create a 6-bit RAM with 16 locations and run the complete EDA flow.",
    ]
    for request in requests:
        spec = await planner.plan(request)
        print(json.dumps({"request": request, "spec": spec}, indent=2))
        if spec["status"] == "needs_clarification":
            continue
        rtl = await coder.rtl(spec, request)
        print(rtl)

    if os.getenv("RUN_EDA", "false").lower() == "true":
        engine = WorkflowEngine("workflow-test-6bit")
        await engine.run(requests[1])
        print(json.dumps(engine.snapshot(), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())