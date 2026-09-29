import asyncio
import os

from backend.config import DEFAULT_RAM_DEPTH, PLANNER_MODEL
from backend.agent.llm import OllamaClient,parse_json
from backend.agent.prompts import PLANNER_SYSTEM
from backend.agent.specification import parse_request

class Planner:
    def __init__(self): self.llm=OllamaClient(PLANNER_MODEL)

    async def plan(self, request):
        """Parse requirements first; an LLM may enrich, but never define, them."""
        fallback = parse_request(request, DEFAULT_RAM_DEPTH)
        fallback['design_name'] = fallback['top_module']
        if os.getenv('LLM_PLANNER_ENABLED', 'false').lower() != 'true':
            return fallback
        try:
            response = await asyncio.wait_for(self.llm.chat(PLANNER_SYSTEM, request), timeout=120)
            model_plan = parse_json(response)
            if isinstance(model_plan, dict):
                enriched = dict(model_plan)
                enriched.update({key: value for key, value in fallback.items() if value is not None})
                return enriched
        except Exception:
            pass
        return fallback
