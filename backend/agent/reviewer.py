from backend.config import REVIEWER_MODEL
from backend.agent.llm import OllamaClient
from backend.agent.prompts import REVIEW_SYSTEM
class Reviewer:
    def __init__(self): self.llm=OllamaClient(REVIEWER_MODEL)
    async def summarize(self,report): return await self.llm.chat(REVIEW_SYSTEM,str(report))
