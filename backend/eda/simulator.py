import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

from backend.config import MOCK_EDA
from backend.eda.base import MockAdapter
from backend.eda.shell import run_command


class Simulator:
    async def run(self, workspace):
        if MOCK_EDA:
            return await MockAdapter('MOCK: 32 tests passed', {'tests': 32, 'passed': 32, 'failed': 0}).run(workspace)

        # Fail fast with a clear reason instead of hanging on a missing simulation toolchain.
        if shutil.which('iverilog') is None:
            return {
                'success': False,
                'return_code': 127,
                'output': 'Simulation failed: iverilog is not installed or not on PATH.',
                'metrics': {},
            }
        cocotb_tools = importlib.util.find_spec('cocotb_tools')
        if cocotb_tools is None or not cocotb_tools.origin:
            return {
                'success': False,
                'return_code': 127,
                'output': f'Simulation failed: cocotb is not installed for {sys.executable}.',
                'metrics': {},
            }
        makefile_sim = Path(cocotb_tools.origin).resolve().parent / 'makefiles' / 'Makefile.sim'
        if not makefile_sim.is_file():
            return {
                'success': False,
                'return_code': 127,
                'output': f'Simulation failed: Cocotb Makefile.sim was not found at {makefile_sim}.',
                'metrics': {},
            }

        plan = json.loads((workspace / 'plan.json').read_text(encoding='utf-8'))
        top = plan.get('top_module', 'adder4')
        testbench = workspace / 'testbench' / 'test_design.py'
        if not testbench.exists():
            return {
                'success': False,
                'return_code': 1,
                'output': 'Simulation failed: no generated Cocotb testbench was found at testbench/test_design.py.',
                'metrics': {},
            }

        makefile = workspace / 'Makefile'
        makefile.write_text(
            'TOPLEVEL_LANG = verilog\n'
            f'TOPLEVEL = {top}\n'
            'COCOTB_TEST_MODULES = test_design\n'
            'SIM = icarus\n'
            'COMPILE_ARGS += -g2012\n'
            'VERILOG_SOURCES = $(CURDIR)/rtl/design.sv\n'
            'export PYTHONPATH := $(CURDIR)/testbench:$(PYTHONPATH)\n'
            f'include {makefile_sim}\n',
            encoding='utf-8',
        )
        # Keep a broken or waiting simulator from holding the whole pipeline.
        timeout = max(1, int(os.getenv('SIMULATION_TIMEOUT_SECONDS', '60')))
        result = await run_command(
            ['make', '-s', '-f', 'Makefile', f'PYTHON_BIN={sys.executable}'],
            workspace,
            timeout,
        )
        summary = re.search(
            r'\bTESTS=(\d+)\s+PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)\b',
            result.get('output', ''),
        )
        if summary:
            result['metrics'] = {
                'tests': int(summary.group(1)),
                'passed': int(summary.group(2)),
                'failed': int(summary.group(3)),
                'skipped': int(summary.group(4)),
            }
            if result['metrics']['tests'] == 0:
                result['success'] = False
                result['output'] += '\nSimulation failed: Cocotb completed without discovering any tests.'
            elif result['metrics']['failed'] > 0:
                result['success'] = False
        elif result.get('success'):
            result['success'] = False
            result['output'] += '\nSimulation failed: Cocotb test summary was not found in simulator output.'
        return result
