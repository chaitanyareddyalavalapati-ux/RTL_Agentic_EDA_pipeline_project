class MockAdapter:
    def __init__(self,output,metrics=None): self.output=output; self.metrics=metrics or {}
    async def run(self,workspace): return {'success':True,'return_code':0,'output':self.output,'metrics':self.metrics}
