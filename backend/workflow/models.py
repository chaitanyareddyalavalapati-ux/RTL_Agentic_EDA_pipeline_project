from dataclasses import dataclass,field
from datetime import datetime
@dataclass
class StepResult:
    name:str; status:str='pending'; attempts:int=0; started_at:str|None=None; ended_at:str|None=None; output:str=''; metrics:dict=field(default_factory=dict); error:str|None=None
    def start(self): self.status='running'; self.started_at=datetime.utcnow().isoformat()
    def finish(self,ok,output='',metrics=None,error=None): self.status='passed' if ok else 'failed'; self.ended_at=datetime.utcnow().isoformat(); self.output=output[-12000:]; self.metrics=metrics or {}; self.error=error
@dataclass
class JobState:
    job_id:str; prompt:str; status:str='created'; plan:dict=field(default_factory=dict); steps:dict=field(default_factory=dict); artifacts:dict=field(default_factory=dict); report:dict=field(default_factory=dict); repair_history:list=field(default_factory=list)
