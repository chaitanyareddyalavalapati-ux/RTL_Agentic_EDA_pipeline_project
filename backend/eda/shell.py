import asyncio
async def run_command(command,cwd,timeout=300,env=None):
    p=await asyncio.create_subprocess_exec(*command,cwd=str(cwd),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(p.communicate(), timeout)
        code = p.returncode
    except asyncio.TimeoutError:
        p.kill(); await p.wait(); return {'success':False,'return_code':-1,'output':'TIMEOUT','metrics':{}}
    return {'success':code==0,'return_code':code,'output':output.decode(errors='replace'),'metrics':{}}

async def run_command_with_input(command,cwd,input_text,timeout=300):
    p=await asyncio.create_subprocess_exec(*command,cwd=str(cwd),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(p.communicate(input_text.encode()), timeout)
        code = p.returncode
    except asyncio.TimeoutError:
        p.kill(); await p.wait(); return {'success':False,'return_code':-1,'output':'TIMEOUT','metrics':{}}
    return {'success':code==0,'return_code':code,'output':output.decode(errors='replace'),'metrics':{}}
