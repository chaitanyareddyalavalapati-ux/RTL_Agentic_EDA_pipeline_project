from backend.config import MOCK_EDA
from backend.eda.base import MockAdapter
from backend.eda.shell import run_command
import re
class OpenSTA:
    async def run(self,workspace):
        if MOCK_EDA:
            return await MockAdapter('MOCK: OpenSTA passed', {
                'wns_ns': 0.42, 'tns_ns': 0.0,
                'setup_slack_ns': 0.42, 'hold_slack_ns': 0.42,
                'setup_paths': 0, 'hold_paths': 0,
            }).run(workspace)
        result = await run_command(['sta', '-exit', 'sta.tcl'], workspace, 300)
        output = result.get('output', '')
        metrics = {}
        wns = re.search(r'wns\s+max\s+([-+]?\d+(?:\.\d+)?)', output, re.IGNORECASE)
        tns = re.search(r'tns\s+max\s+([-+]?\d+(?:\.\d+)?)', output, re.IGNORECASE)
        metrics['wns_ns'] = float(wns.group(1)) if wns else None
        metrics['tns_ns'] = float(tns.group(1)) if tns else None
        path_slacks = [
            float(value)
            for value in re.findall(
                r'([-+]?\d+(?:\.\d+)?)\s+slack\s+\((?:MET|VIOLATED)\)',
                output,
                re.IGNORECASE,
            )
        ]
        if path_slacks and (metrics['wns_ns'] is None or metrics['wns_ns'] == 0.0):
            metrics['wns_ns'] = min(path_slacks)
        setup = re.search(r'(?:setup|max).*?slack\s*[: ]\s*([-+]?\d+(?:\.\d+)?)', output, re.IGNORECASE)
        hold = re.search(r'(?:hold|min).*?slack\s*[: ]\s*([-+]?\d+(?:\.\d+)?)', output, re.IGNORECASE)
        metrics['setup_slack_ns'] = float(setup.group(1)) if setup else metrics['wns_ns']
        metrics['hold_slack_ns'] = float(hold.group(1)) if hold else None
        metrics['setup_paths'] = len(path_slacks)
        metrics['hold_paths'] = 0
        result['metrics'] = metrics
        return result
