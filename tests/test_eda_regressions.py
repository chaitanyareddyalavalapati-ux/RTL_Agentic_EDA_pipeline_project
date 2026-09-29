import asyncio
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from backend.agent.coder import Coder
from backend.agent.specification import parse_request, validate_rtl
from backend.eda.openlane import _write_layout_svg
from backend.eda.simulator import Simulator
from backend.api import routes
from backend.eda import simulator as simulator_module
from backend import db
from backend.workflow.engine import _is_environment_tool_failure


def test_common_request_types_and_flip_flop_variants():
    cases = {
        'Create a flip flop for data storage': 'dff',
        'Create a JK flip-flop with reset': 'jk',
        'Build an 8-bit comparator': 'comparator',
        'Build a 4-bit mux': 'mux',
        'Create a 5-bit counter': 'counter',
        'Create a 4-bit adder': 'adder',
    }
    for prompt, expected in cases.items():
        assert parse_request(prompt)['design_type'] == expected


def test_fallback_rtl_conformance_detects_missing_interface():
    plan = parse_request('Create a 4-bit adder')
    rtl = Coder._fallback_rtl(plan)
    assert validate_rtl(rtl, plan) == []
    assert validate_rtl(rtl.replace('carry_out', 'carry'), plan)


@pytest.mark.parametrize(
    'message',
    [
        'set sticky bit on: chmod /run/user/1000/libpod: read-only file system',
        'cannot setresgid: Invalid argument',
        'insufficient UIDs or GIDs available in user namespace',
        'OCI runtime error: unable to apply cgroup configuration; Interactive authentication required.',
    ],
)
def test_container_runtime_errors_are_classified_as_environment_failures(message):
    assert _is_environment_tool_failure(message)


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ('make', 'iverilog'))
    or importlib.util.find_spec('cocotb_tools') is None,
    reason='real Icarus and Cocotb toolchain is unavailable',
)
def test_generated_adder_bench_exhaustively_passes_with_icarus(tmp_path, monkeypatch):
    plan = parse_request('Create a 4-bit adder')
    (tmp_path / 'rtl').mkdir()
    (tmp_path / 'testbench').mkdir()
    (tmp_path / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    (tmp_path / 'rtl' / 'design.sv').write_text(Coder._fallback_rtl(plan), encoding='utf-8')
    (tmp_path / 'testbench' / 'test_design.py').write_text(
        Coder._fallback_testbench(plan), encoding='utf-8'
    )
    monkeypatch.setattr(simulator_module, 'MOCK_EDA', False)

    result = asyncio.run(Simulator().run(tmp_path))

    assert result['success'], result['output']
    assert result['metrics'] == {'tests': 1, 'passed': 1, 'failed': 0, 'skipped': 0}
    assert 'PASS vectors=512' in result['output']


def test_routed_def_is_rendered_as_a_physical_layout_svg(tmp_path):
    def_file = tmp_path / 'final.def'
    def_file.write_text(
        '''VERSION 5.8 ;
DESIGN sample ;
DIEAREA ( 0 0 ) ( 10000 10000 ) ;
COMPONENTS 1 ;
- U1 sky130_fd_sc_hd__inv_1 + PLACED ( 2500 2500 ) N ;
END COMPONENTS
PINS 1 ;
- out + NET out + DIRECTION OUTPUT + PLACED ( 9000 9000 ) N ;
END PINS
NETS 1 ;
- out ( U1 Y ) ( PIN out ) + ROUTED met1 ( 2500 2500 ) ( 9000 9000 ) ;
END NETS
END DESIGN
''',
        encoding='utf-8',
    )

    image_path = _write_layout_svg(tmp_path, def_file)

    image = image_path.read_text(encoding='utf-8')
    assert image_path.name == 'physical_layout.svg'
    assert 'cells: 1 | nets: 1' in image
    assert 'Actual routed DEF metal segments' in image


def test_physical_layout_artifact_is_served_as_an_image(tmp_path, monkeypatch):
    image_path = tmp_path / 'physical_layout.svg'
    image_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding='utf-8')
    monkeypatch.setattr(routes, 'jobs', {})
    monkeypatch.setattr(
        routes,
        'get_job',
        lambda job_id: {'job_id': job_id, 'workspace': str(tmp_path), 'artifacts': {}},
    )

    response = asyncio.run(routes.artifact('layout-test', path='physical_layout.svg'))

    assert response.media_type == 'image/svg+xml'


def test_netlist_png_artifact_is_served_as_png(tmp_path, monkeypatch):
    image_path = tmp_path / 'synthesized.png'
    image_path.write_bytes(b'\x89PNG\r\n\x1a\n')
    monkeypatch.setattr(routes, 'jobs', {})
    monkeypatch.setattr(
        routes,
        'get_job',
        lambda job_id: {'job_id': job_id, 'workspace': str(tmp_path), 'artifacts': {}},
    )

    response = asyncio.run(routes.artifact('netlist-test', path='synthesized.png'))

    assert response.media_type == 'image/png'


@pytest.mark.parametrize(
    'output,expected_detail',
    [
        ('Cocotb completed but produced no summary', 'summary was not found'),
        ('TESTS=0 PASS=0 FAIL=0 SKIP=0', 'without discovering any tests'),
    ],
)
def test_simulator_rejects_unverified_success(tmp_path, monkeypatch, output, expected_detail):
    (tmp_path / 'testbench').mkdir()
    (tmp_path / 'plan.json').write_text(json.dumps({'top_module': 'top'}), encoding='utf-8')
    (tmp_path / 'testbench' / 'test_design.py').write_text('', encoding='utf-8')
    monkeypatch.setattr(simulator_module, 'MOCK_EDA', False)

    async def successful_command(*args, **kwargs):
        return {'success': True, 'return_code': 0, 'output': output, 'metrics': {}}

    monkeypatch.setattr(simulator_module, 'run_command', successful_command)
    result = asyncio.run(Simulator().run(tmp_path))

    assert not result['success']
    assert expected_detail in result['output']


def test_restart_marks_active_jobs_interrupted_and_replayable(tmp_path, monkeypatch):
    database = tmp_path / 'pipeline.db'
    monkeypatch.setattr(db, 'DB_PATH', database)
    db.init_db()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,prompt,status,plan_json,report_json,created_at,updated_at) "
            "VALUES ('stale-job','Create a RAM','running','{}','{}','now','now')"
        )
        conn.execute(
            "INSERT INTO steps(job_id,name,status,attempts,started_at) "
            "VALUES ('stale-job','simulation','running',1,'now')"
        )

    assert db.recover_interrupted_jobs() == 1
    snapshot = db.get_job('stale-job')
    events = db.get_events('stale-job')

    assert snapshot['status'] == 'failed'
    assert snapshot['report']['interrupted'] is True
    assert snapshot['steps']['simulation']['status'] == 'failed'
    assert events[-1]['type'] == 'pipeline_failed'
    assert events[-1]['event_id'] > 0