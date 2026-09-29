import asyncio
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.coder import Coder
from backend.agent.specification import parse_request
from backend.config import (
    KLAYOUT_CONTAINER_RUNTIME,
    OPENROAD_CELL_LEF_PATH,
    OPENROAD_PATH,
    OPENROAD_TECH_LEF_PATH,
    OPENSTA_LIBERTY_PATH,
    KLAYOUT_CONTAINER_IMAGE,
)
from backend.eda.openlane import OpenLane
from backend.eda.opensta import OpenSTA
from backend.eda.simulator import Simulator
from backend.eda.yosys import Yosys
from backend.workflow.engine import WorkflowEngine


def _real_sky130_tools_available():
    binaries = ('make', 'iverilog', 'sta', KLAYOUT_CONTAINER_RUNTIME)
    files = (
        OPENROAD_PATH,
        OPENROAD_TECH_LEF_PATH,
        OPENROAD_CELL_LEF_PATH,
        OPENSTA_LIBERTY_PATH,
    )
    renderer_available = shutil.which('rsvg-convert') or shutil.which('convert')
    if not renderer_available or not all(shutil.which(binary) for binary in binaries):
        return False
    if not all(Path(path).is_file() for path in files):
        return False
    image_check = subprocess.run(
        [KLAYOUT_CONTAINER_RUNTIME, 'image', 'exists', KLAYOUT_CONTAINER_IMAGE],
        capture_output=True, timeout=10,
    )
    if image_check.returncode != 0:
        return False
    try:
        probe = subprocess.run(
            [KLAYOUT_CONTAINER_RUNTIME, 'run', '--rm', '--entrypoint', 'yosys', KLAYOUT_CONTAINER_IMAGE, '-V'],
            capture_output=True, text=True, timeout=20,
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


REAL_SKY130_TOOLS = _real_sky130_tools_available()
SKIP_REASON = 'real Yosys container, OpenSTA, OpenROAD, or Sky130 LEF/Liberty is unavailable'
REAL_SIMULATOR = bool(
    shutil.which('make')
    and shutil.which('iverilog')
    and importlib.util.find_spec('cocotb_tools')
)


def _prepare_workspace(workspace, prompt):
    plan = parse_request(prompt)
    engine = object.__new__(WorkflowEngine)
    engine.state = SimpleNamespace(plan=plan, artifacts={})
    engine.workspace = Path(workspace)
    engine.persist = lambda: None
    (engine.workspace / 'rtl').mkdir()
    (engine.workspace / 'rtl' / 'design.sv').write_text(
        Coder._fallback_rtl(plan), encoding='utf-8'
    )
    asyncio.run(engine._ensure_flow_artifacts())
    return engine


@pytest.mark.skipif(not REAL_SIMULATOR, reason='real Icarus/Cocotb toolchain is unavailable')
def test_four_bit_ram_simulation_drives_clock_and_checks_all_addresses(tmp_path):
    plan = parse_request('Create a 4-bit RAM and run simulation')
    (tmp_path / 'rtl').mkdir()
    (tmp_path / 'testbench').mkdir()
    (tmp_path / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    (tmp_path / 'rtl' / 'design.sv').write_text(
        Coder._fallback_rtl(plan), encoding='utf-8'
    )
    (tmp_path / 'testbench' / 'test_design.py').write_text(
        Coder._fallback_testbench(plan), encoding='utf-8'
    )

    result = asyncio.run(Simulator().run(tmp_path))

    assert result['success'], result.get('output', '')
    assert result['metrics'] == {'tests': 1, 'passed': 1, 'failed': 0, 'skipped': 0}
    assert 'PASS addresses=16 width=4' in result['output']


@pytest.mark.skipif(not REAL_SIMULATOR, reason='real Icarus/Cocotb toolchain is unavailable')
def test_four_bit_rom_prompt_generates_and_simulates_read_only_table(tmp_path):
    prompt = 'Create a 4 bit ROM and run the complete EDA flow'
    plan = parse_request(prompt)
    assert plan['design_type'] == 'rom'
    assert plan['top_module'] == 'rom4x4'
    assert plan['data_width'] == 4
    assert plan['address_width'] == 4
    assert plan['depth'] == 16
    assert plan['clocked'] is False
    assert len(plan['rom_contents']) == 16
    assert plan['assumptions']

    (tmp_path / 'rtl').mkdir()
    (tmp_path / 'testbench').mkdir()
    rtl = Coder._fallback_rtl(plan)
    testbench = Coder._fallback_testbench(plan, rtl)
    assert 'always @*' in rtl
    assert 'posedge clk' not in rtl
    assert 'assign out = a + b' not in rtl
    compile(testbench, 'generated_rom_test.py', 'exec')
    (tmp_path / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    (tmp_path / 'rtl' / 'design.sv').write_text(rtl, encoding='utf-8')
    (tmp_path / 'testbench' / 'test_design.py').write_text(testbench, encoding='utf-8')

    result = asyncio.run(Simulator().run(tmp_path))

    assert result['success'], result.get('output', '')
    assert result['metrics'] == {'tests': 1, 'passed': 1, 'failed': 0, 'skipped': 0}
    assert 'PASS addresses=16 width=4' in result['output']


def test_generic_comparator_testbench_has_valid_python_indentation():
    plan = parse_request('Create a 4-bit comparator')
    testbench = Coder._fallback_testbench(plan, Coder._fallback_rtl(plan))
    compile(testbench, 'generated_comparator_test.py', 'exec')


@pytest.mark.skipif(not REAL_SKY130_TOOLS, reason=SKIP_REASON)
def test_sky130_adder_maps_then_runs_opensta_and_openroad(tmp_path):
    engine = _prepare_workspace(tmp_path, 'Create a 4-bit adder and run the complete EDA flow')

    async def run_flow():
        synthesis = await Yosys().run(engine.workspace)
        assert synthesis['success'], synthesis.get('output', '')
        mapped = (engine.workspace / 'synthesized.v').read_text(encoding='utf-8')
        assert 'sky130_fd_sc_hd__' in mapped
        assert synthesis['metrics']['cell_count'] > 0
        assert (engine.workspace / 'synthesized.svg').is_file()
        png = (engine.workspace / 'synthesized.png').read_bytes()
        assert png.startswith(b'\x89PNG\r\n\x1a\n')

        timing = await OpenSTA().run(engine.workspace)
        assert timing['success'], timing.get('output', '')
        assert isinstance(timing['metrics']['wns_ns'], (int, float))
        assert 'sky130_fd_sc_hd__' in timing.get('output', '')

        physical = await OpenLane().physical_design(engine.workspace)
        assert physical['success'], physical.get('output', '')
        assert physical['metrics']['routing'] == 'pass'
        assert (engine.workspace / 'final.def').is_file()
        assert (engine.workspace / 'physical_layout.svg').is_file()
        if physical['metrics'].get('gds_stream_out') == 'pass':
            assert (engine.workspace / 'final.gds').stat().st_size > 0

    asyncio.run(run_flow())


@pytest.mark.skipif(not REAL_SKY130_TOOLS, reason=SKIP_REASON)
def test_sky130_sequential_mapping_uses_dfflibmap(tmp_path):
    engine = _prepare_workspace(tmp_path, 'Create a D flip flop with asynchronous reset')
    synthesis_script = (engine.workspace / 'synthesis.ys').read_text(encoding='utf-8')
    assert 'dfflibmap -liberty' in synthesis_script

    async def run_flow():
        synthesis = await Yosys().run(engine.workspace)
        assert synthesis['success'], synthesis.get('output', '')
        mapped = (engine.workspace / 'synthesized.v').read_text(encoding='utf-8')
        assert 'sky130_fd_sc_hd__' in mapped
        timing = await OpenSTA().run(engine.workspace)
        assert timing['success'], timing.get('output', '')

    asyncio.run(run_flow())


@pytest.mark.skipif(not REAL_SKY130_TOOLS, reason=SKIP_REASON)
def test_sky130_ram_uses_ram_ports_for_opensta_constraints(tmp_path):
    engine = _prepare_workspace(tmp_path, 'Create a 4-bit RAM and run the complete EDA flow')
    constraints = (engine.workspace / 'constraints.sdc').read_text(encoding='utf-8')
    assert '[get_ports we]' in constraints
    assert '[get_ports {addr[*]}]' in constraints
    assert '[get_ports {write_data[*]}]' in constraints
    assert '[get_ports {read_data[*]}]' in constraints
    assert '[get_ports d]' not in constraints
    assert '[get_ports rst_n]' not in constraints

    async def run_flow():
        synthesis = await Yosys().run(engine.workspace)
        assert synthesis['success'], synthesis.get('output', '')
        timing = await OpenSTA().run(engine.workspace)
        assert timing['success'], timing.get('output', '')
        assert isinstance(timing['metrics']['wns_ns'], (int, float))

    asyncio.run(run_flow())