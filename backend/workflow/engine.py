import json
import shutil
import os
import json as _json
import subprocess
import tempfile
import asyncio
from datetime import datetime, timezone

from backend.config import WORKSPACE_ROOT, MAX_RETRIES, REVIEWER_MODEL, OPENSTA_LIBERTY_PATH
from backend.config import YOSYS_GENERATE_GRAPH
from backend.db import init_db, save_snapshot
from backend.workflow.models import JobState, StepResult
from backend.workflow.events import event_bus
from backend.agent.planner import Planner
from backend.agent.coder import Coder
from backend.agent.reviewer import Reviewer
from backend.agent.llm import OllamaClient, parse_json
from backend.agent.prompts import DEBUG_SYSTEM
from backend.agent.specification import validate_rtl
from backend.rag import EdaRAG
from backend.graph import EdaGraph
from backend.eda.simulator import Simulator
from backend.eda.yosys import Yosys
from backend.eda.opensta import OpenSTA
from backend.eda.openlane import OpenLane
from backend.reports.generator import build_report, write_report

init_db()

_ENVIRONMENT_FAILURE_MARKERS = (
    'set sticky bit on',
    'cannot setresgid',
    'insufficient UIDs or GIDs',
    'oci runtime error',
    'unable to apply cgroup configuration',
    'interactive authentication required',
    'permission denied while trying to connect to the Docker daemon',
    'cannot connect to the Podman socket',
    'container runtime is unavailable',
)


def _is_environment_tool_failure(output):
    text = str(output or '').lower()
    return any(marker.lower() in text for marker in _ENVIRONMENT_FAILURE_MARKERS)


class WorkflowEngine:
    """Runs the RTL-to-physical-design flow with bounded autonomous repair.

    Repair is artifact-aware: the debugger selects the smallest workspace file
    to modify, the coder produces a minimal patch, and the failed stage
    is re-run. Every repair is backed up and recorded in repair_history.
    """

    REPAIRABLE = {
        'rtl': {'rtl/design.sv'},
        'testbench': {'testbench/test_design.py'},
        'synthesis': {'synthesis.ys'},
        'sta': {'sta.tcl', 'constraints.sdc'},
        'constraints': {'constraints.sdc', 'sta.tcl'},
        'physical_design': {'config.json', 'openlane/config.json', 'synthesis.ys', 'sta.tcl'},
        'physical_verification': {'verify.sh', 'config.json', 'openlane/config.json'},
        'flow': {'synthesis.ys', 'sta.tcl', 'constraints.sdc', 'config.json', 'openlane/config.json', 'verify.sh'},
    }

    def __init__(self, job_id):
        self.state = JobState(job_id, '')
        self.workspace = WORKSPACE_ROOT / job_id
        self.planner = Planner()
        self.coder = Coder()
        self.reviewer = Reviewer()
        self.debugger = OllamaClient(REVIEWER_MODEL)
        self.rag = EdaRAG()
        self.graph = EdaGraph()
        self.graph.init_schema()
        self.simulator = Simulator()
        self.yosys = Yosys()
        self.sta = OpenSTA()
        self.openlane = OpenLane()

    def snapshot(self):
        return {
            'job_id': self.state.job_id,
            'prompt': self.state.prompt,
            'status': self.state.status,
            'plan': self.state.plan,
            'steps': {k: vars(v) for k, v in self.state.steps.items()},
            'artifacts': self.state.artifacts,
            'repair_history': self.state.repair_history,
            'report': self.state.report,
            'workspace': str(self.workspace),
        }

    def persist(self):
        save_snapshot(self.snapshot(), datetime.now(timezone.utc).isoformat())
        try:
            self.graph.index_job(self.state.job_id, self.state.prompt, self.state.status)
        except Exception:
            pass

    async def emit(self, event_type, **data):
        self.persist()
        await event_bus.emit(self.state.job_id, event_type, **data)
        self.persist()

    async def write(self, rel, text):
        p = self.workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        self.state.artifacts[rel] = str(p)
        self.persist()
        return p

    async def run(self, prompt):
        self.state.prompt = prompt
        self.state.status = 'planning'
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.persist()
        await self.emit('pipeline_started', prompt=prompt)
        try:
            await self.emit('step_started', step='planning')
            self.state.plan = await self.planner.plan(prompt)
            await self.write('plan.json', json.dumps(self.state.plan, indent=2))
            await self.write('design_spec.json', json.dumps(self.state.plan, indent=2))
            if self.state.plan.get('status') == 'needs_clarification':
                missing = ', '.join(self.state.plan.get('missing_requirements', []))
                self.state.status = 'needs_clarification'
                self.state.report = build_report(self.state, f'Missing mandatory specification: {missing}.')
                write_report(self.workspace, self.state.report)
                self.persist()
                await self.emit('pipeline_needs_clarification', missing=self.state.plan.get('missing_requirements', []), report=self.state.report)
                return
            self.state.status = 'running'
            await self.emit('step_completed', step='planning', status='passed', result=self.state.plan)

            rtl = await self.generate('rtl', lambda hint: self.coder.rtl(self.state.plan, prompt, hint), 'rtl/design.sv')
            if rtl is None:
                return await self.fail('rtl')
            validation_errors = validate_rtl(rtl, self.state.plan)
            validation = {'status': 'passed' if not validation_errors else 'failed', 'errors': validation_errors}
            self.state.steps['validate_rtl'] = StepResult('validate_rtl')
            self.state.steps['validate_rtl'].finish(not validation_errors, json.dumps(validation), validation)
            await self.write('validation_results.json', json.dumps(validation, indent=2))
            if validation_errors:
                return await self.fail('validate_rtl')
            tb = await self.generate('testbench', lambda hint: self.coder.testbench(self.state.plan, rtl, hint), 'testbench/test_design.py')
            if tb is None:
                return await self.fail('testbench')

            await self._ensure_flow_artifacts()

            for name, runner in [
                ('simulation', self.simulator.run),
                ('synthesis', self.yosys.run),
                ('sta', self.sta.run),
                ('physical_design', self.openlane.physical_design),
                ('physical_verification', self.openlane.physical_verification),
            ]:
                if not await self.tool(name, runner):
                    return await self.fail(name)

            self.state.status = 'completed'
            try:
                review = await asyncio.wait_for(
                    self.reviewer.summarize(self.snapshot()),
                    timeout=15,
                )
            except (asyncio.TimeoutError, Exception) as error:
                review = f'Automated review unavailable: {type(error).__name__}. EDA stages completed successfully.'
            self.state.report = build_report(self.state, review)
            write_report(self.workspace, self.state.report)
            self.persist()
            await self.emit('pipeline_completed', report=self.state.report)
        except asyncio.CancelledError:
            self.state.status = 'failed'
            self.state.report = build_report(self.state, 'Pipeline cancelled while the server was restarting.')
            write_report(self.workspace, self.state.report)
            self.persist()
            raise
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            self.state.status = 'failed'
            self.state.report = build_report(self.state, f'Unhandled exception: {e}\n\n{error_detail}')
            write_report(self.workspace, self.state.report)
            self.persist()
            await self.emit('pipeline_failed', error=str(e), traceback=error_detail, report=self.state.report)

    async def _ensure_flow_artifacts(self):
        """Create repairable baseline flow files before downstream execution."""
        top = self.state.plan.get('top_module', 'top')
        design_name = self.state.plan.get('design_name', top)
        design_type = self.state.plan.get('design_type')
        clocked_interfaces = {
            'dff': (['d', 'rst_n'], ['q']),
            'jk': (['j', 'k', 'rst_n'], ['q']),
            'counter': (['rst'], ['count']),
            'ram': (['we', 'addr', 'write_data'], ['read_data']),
            'fifo': (['rst', 'wr_en', 'rd_en', 'write_data'], ['read_data', 'full', 'empty']),
        }
        is_sequential = design_type in clocked_interfaces
        synthesis_mapping = (
            f'dfflibmap -liberty {OPENSTA_LIBERTY_PATH}\n'
            if is_sequential else ''
        )
        if is_sequential:
            inputs, outputs = clocked_interfaces[design_type]

            def port_collection(names):
                return ' '.join(f'[get_ports {{{name}[*]}}]' if name in {'addr', 'write_data', 'read_data', 'count'} else f'[get_ports {name}]' for name in names)

            timing_constraints = (
                'create_clock -name clk -period 10.0 [get_ports clk]\n'
                + ''.join(
                    f'set_input_delay 1.0 -clock clk {port}\n'
                    for port in [port_collection([name]) for name in inputs]
                )
                + ''.join(
                    f'set_output_delay 1.0 -clock clk {port}\n'
                    for port in [port_collection([name]) for name in outputs]
                )
            )
        else:
            timing_constraints = (
                f'# Timing constraints for {top}.\n'
                '# Use a virtual clock so combinational input-to-output paths are timed.\n'
                'create_clock -name virtual_clk -period 10.0\n'
                'set_input_delay 0.0 -clock virtual_clk [all_inputs]\n'
                'set_output_delay 0.0 -clock virtual_clk [all_outputs]\n'
            )
        await self.write(
            'synthesis.ys',
            f'read_verilog -sv rtl/design.sv\n'
            f'hierarchy -check -top {top}\n'
            f'synth -top {top} -noabc\n'
            f'{synthesis_mapping}'
            f'abc -liberty {OPENSTA_LIBERTY_PATH}\n'
            'clean\n'
            'write_verilog -noattr synthesized.v\n'
            'write_json synthesized.json\n'
            + 'stat\n',
        )
        await self.write(
            'constraints.sdc',
            timing_constraints,
        )
        await self.write(
            'sta.tcl',
            f'# OpenSTA script for {top}.\n'
            f'read_liberty {OPENSTA_LIBERTY_PATH}\n'
            'read_verilog synthesized.v\n'
            f'link_design {top}\n'
            'read_sdc constraints.sdc\n'
            'report_checks\n'
            'report_wns\n'
            'report_tns\n'
            'exit\n',
        )
        await self.write(
            'config.json',
            json.dumps({
                'DESIGN_NAME': design_name,
                'VERILOG_FILES': ['rtl/design.sv'],
                'CLOCK_PERIOD': 10.0,
                'CLOCK_PORT': '',
            }, indent=2) + '\n',
        )
        await self.write(
            'verify.sh',
            '#!/usr/bin/env bash\nset -euo pipefail\n\n# Physical verification hook.\n# Replace/add PDK-specific DRC/LVS commands when configured.\necho "No PDK-specific DRC/LVS command configured"\n',
        )

    async def debug(self, stage, error, log):
        try:
            query = f"{stage} {error} {log[-5000:]}"
            rag_context = self.rag.format_context(query, stage=stage, limit=5)
            prompt = (
                f'Stage: {stage}\nFailure: {error}\nLog:\n{log[-12000:]}\n\n'
                f'Preserved design specification:\n{json.dumps(self.state.plan, sort_keys=True)}\n\n'
                f'{rag_context}\n\n'
                'Use the retrieved evidence to improve diagnosis when relevant. ' 
                'Historical repairs are suggestions only; verify them against the current log and artifact.'
            )
            result = parse_json(await self.debugger.chat(DEBUG_SYSTEM, prompt))
            if not isinstance(result, dict):
                raise ValueError('Debugger did not return an object')
            return result
        except Exception as e:
            return {
                'diagnosis': error,
                'target': stage if stage in self.REPAIRABLE else 'flow',
                'artifact': self._default_artifact(stage),
                'action': 'modify',
                'confidence': 0.1,
                'patch_instructions': f'Debugger unavailable: {type(e).__name__}. Inspect the stage log and make the smallest safe correction.',
            }

    def _default_artifact(self, stage):
        return {
            'rtl': 'rtl/design.sv',
            'testbench': 'testbench/test_design.py',
            'synthesis': 'synthesis.ys',
            'sta': 'sta.tcl',
            'physical_design': 'config.json',
            'physical_verification': 'verify.sh',
        }.get(stage)

    def _select_repair_artifact(self, stage, diagnosis):
        allowed = self.REPAIRABLE.get(stage, set()) | self.REPAIRABLE.get(str((diagnosis or {}).get('target') or '').lower(), set())
        requested = str((diagnosis or {}).get('artifact') or '').strip().replace('\\', '/')
        if requested in allowed and (self.workspace / requested).is_file():
            return requested

        target = str((diagnosis or {}).get('target') or '').lower()
        if target in self.REPAIRABLE:
            for rel in self.REPAIRABLE[target]:
                if rel in allowed and (self.workspace / rel).is_file():
                    return rel
        default = self._default_artifact(stage)
        if default and default in allowed and (self.workspace / default).is_file():
            return default
        return None

    def _safe_rel(self, rel):
        """Return a normalized workspace-relative path or reject path traversal."""
        rel = str(rel or '').replace('\\', '/').strip()
        if not rel or rel.startswith('/') or rel.startswith('../') or '/..' in rel.split('/'):
            return None
        p = (self.workspace / rel).resolve()
        try:
            p.relative_to(self.workspace.resolve())
        except ValueError:
            return None
        return rel

    def _validate_patch_text(self, patch, rel):
        """Reject patches that can touch anything except the selected existing file."""
        if not patch or not patch.strip():
            raise ValueError('empty patch')
        lines = patch.splitlines()
        headers = []
        for line in lines:
            if line.startswith('--- ') or line.startswith('+++ '):
                headers.append(line.split('\t', 1)[0].split(' ', 1)[1].strip())
        if len(headers) < 2 or len(headers) % 2:
            raise ValueError('patch does not contain complete unified-diff headers')
        expected = rel
        for raw in headers:
            path = raw
            if path.startswith('a/') or path.startswith('b/'):
                path = path[2:]
            if path != expected:
                raise ValueError(f'patch attempts to modify {path!r}, expected {expected!r}')
        if any('\x00' in line for line in lines):
            raise ValueError('patch contains NUL bytes')
        return patch

    async def _apply_unified_patch(self, rel, patch):
        """Dry-run then apply a zero-fuzz patch using the system patch utility."""
        self._validate_patch_text(patch, rel)
        with tempfile.NamedTemporaryFile('w', suffix='.patch', delete=False, encoding='utf-8') as f:
            f.write(patch)
            patch_file = f.name
        try:
            check = subprocess.run(
                ['patch', '--dry-run', '--batch', '--forward', '--fuzz=0', '-p0', '-i', patch_file],
                cwd=str(self.workspace), text=True, capture_output=True, timeout=30,
            )
            if check.returncode != 0:
                raise ValueError(f'patch dry-run failed: {check.stdout[-3000:]}')
            applied = subprocess.run(
                ['patch', '--batch', '--forward', '--fuzz=0', '-p0', '-i', patch_file],
                cwd=str(self.workspace), text=True, capture_output=True, timeout=30,
            )
            if applied.returncode != 0:
                raise ValueError(f'patch application failed: {applied.stdout[-3000:]}')
        finally:
            try:
                os.unlink(patch_file)
            except OSError:
                pass

    async def _validate_candidate(self, rel):
        """Fast pre-flight validation before an expensive EDA stage is rerun."""
        path = self.workspace / rel
        if not path.is_file() or path.stat().st_size == 0:
            return False, 'candidate artifact is missing or empty'

        suffix = path.suffix.lower()
        checks = []
        if suffix == '.json':
            def json_check():
                _json.loads(path.read_text(encoding='utf-8'))
            checks.append(('json', json_check))
        elif suffix == '.py':
            checks.append(('python', lambda: subprocess.run(
                ['python', '-m', 'py_compile', str(path)],
                cwd=str(self.workspace), text=True, capture_output=True, timeout=30,
                check=True,
            )))
        elif path.name.endswith('.sh'):
            checks.append(('shell', lambda: subprocess.run(
                ['bash', '-n', str(path)], cwd=str(self.workspace), text=True,
                capture_output=True, timeout=30, check=True,
            )))
        elif suffix in {'.sv', '.v'}:
            # Prefer a real syntax check when available; otherwise keep the stage
            # runner as the authoritative validator.
            if shutil.which('iverilog'):
                top = self.state.plan.get('top_module', 'top')
                checks.append(('systemverilog', lambda: subprocess.run(
                    ['iverilog', '-g2012', '-s', str(top), '-t', 'null', str(path)],
                    cwd=str(self.workspace), text=True, capture_output=True, timeout=60, check=True,
                )))

        for name, check in checks:
            try:
                result = check()
                if hasattr(result, 'stderr') and result.stderr:
                    return False, f'{name} validation failed: {result.stderr[-3000:]}'
            except Exception as e:
                detail = getattr(e, 'stderr', None) or str(e)
                return False, f'{name} validation failed: {detail[-3000:]}'
        return True, 'pre-flight validation passed'

    def _quality_gate(self, stage, out):
        """Reject successful commands that still report an obvious quality violation."""
        metrics = out.get('metrics') or {}
        if stage == 'sta':
            wns = metrics.get('wns_ns')
            if isinstance(wns, (int, float)) and wns < 0:
                return False, f'STA quality gate rejected negative WNS ({wns} ns)'
        if stage == 'physical_verification':
            for key in ('drc', 'lvs'):
                if key in metrics and str(metrics[key]).lower() not in {'pass', 'passed', '0', 'clean', 'disabled'}:
                    return False, f'physical verification quality gate rejected {key}={metrics[key]!r}'
        return True, 'quality gate passed'

    async def _rollback(self, rel, backup):
        target = self.workspace / rel
        if backup.exists():
            shutil.copy2(backup, target)
            self.state.artifacts[rel] = str(target)

    def _deterministic_repair(self, rel, original, diagnosis, log):
        """Handle common broken Cocotb testbench patterns without waiting on an LLM patch."""
        rel = str(rel or '').replace('\\', '/')
        if rel != 'testbench/test_design.py':
            return None
        lower = original.lower()
        if 'setattr(dut' in lower or 'setattr( dut' in lower or '.value = 0' not in lower and 'setattr' in lower:
            fallback = self.coder._fallback_testbench(self.state.plan or {}, self.state.steps.get('rtl', {}).result if hasattr(self.state.steps.get('rtl', {}), 'result') else '')
            if fallback.strip() and fallback.strip() != original.strip():
                return fallback
        return None

    async def apply_repair(self, stage, diagnosis, log, attempt, runner, failure_rag_id=None):
        """Generate -> validate -> execute -> rollback on any bad candidate.

        The repair is always represented as a unified diff. The original file is
        restored whenever the patch is invalid, pre-flight validation fails, the
        EDA stage fails, or its output violates a quality gate.
        """
        action = str((diagnosis or {}).get('action') or 'modify').lower()
        if action in {'environment', 'none'}:
            return None

        rel = self._select_repair_artifact(stage, diagnosis)
        rel = self._safe_rel(rel)
        if not rel:
            return None
        path = self.workspace / rel
        if not path.is_file():
            return None

        original = path.read_text(encoding='utf-8', errors='replace')
        backup_rel = f'.repair_backups/attempt_{attempt}/{rel}'
        backup = self.workspace / backup_rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        self.state.artifacts[backup_rel] = str(backup)

        target = str((diagnosis or {}).get('target') or stage).lower()
        fallback_repair = self._deterministic_repair(rel, original, diagnosis, log)
        try:
            if fallback_repair is not None:
                path.write_text(fallback_repair, encoding='utf-8')
                patch = ''
                rationale = 'Deterministic repair: replaced invalid Cocotb signal assignments with a valid fallback smoke test.'
            else:
                rag_query = f"{stage} {target} {diagnosis} {log[-5000:]}"
                rag_hits = self.rag.search(rag_query, stage=stage, artifact=rel, limit=5)
                rag_context = self.rag.format_context(
                    rag_query, stage=stage, artifact=rel, limit=5,
                )
                repair_context = f"{log[-8000:]}\n\n{rag_context}"
                patch_result = await self.coder.repair_patch(
                    target, original, diagnosis, repair_context, path=rel,
                )
                patch = patch_result.get('patch', '')
                rationale = patch_result.get('rationale', '')
                self._validate_patch_text(patch, rel)
                await self._apply_unified_patch(rel, patch)
        except Exception as e:
            await self._rollback(rel, backup)
            record = {
                'stage': stage, 'attempt': attempt, 'artifact': rel,
                'backup': backup_rel, 'status': 'patch_rejected',
                'error': f'{type(e).__name__}: {e}',
            }
            self.state.repair_history.append(record)
            try:
                repair_rag_id = self.rag.index_repair(self.state.job_id, stage, attempt, diagnosis or {}, rel, patch if 'patch' in locals() else '', '', 'patch_rejected', str(e))
                self.graph.index_repair(self.state.job_id, stage, repair_rag_id, attempt, rel, 'patch_rejected', patch if 'patch' in locals() else '', '', failure_rag_id)
            except Exception:
                pass
            self.persist()
            return None

        valid, validation_detail = await self._validate_candidate(rel)
        if not valid:
            await self._rollback(rel, backup)
            self.state.repair_history.append({
                'stage': stage, 'attempt': attempt, 'artifact': rel,
                'backup': backup_rel, 'status': 'validation_failed',
                'validation': validation_detail,
            })
            try:
                repair_rag_id = self.rag.index_repair(self.state.job_id, stage, attempt, diagnosis or {}, rel, patch, rationale, 'validation_failed', validation_detail)
                self.graph.index_repair(self.state.job_id, stage, repair_rag_id, attempt, rel, 'validation_failed', patch, rationale, failure_rag_id)
            except Exception:
                pass
            self.persist()
            return None

        try:
            out = await runner(self.workspace)
        except Exception as e:
            out = {'success': False, 'return_code': -1,
                   'output': f'{type(e).__name__}: {e}', 'metrics': {}}

        quality_ok, quality_detail = self._quality_gate(stage, out)
        if not out.get('success') or not quality_ok:
            await self._rollback(rel, backup)
            self.state.repair_history.append({
                'stage': stage, 'attempt': attempt, 'artifact': rel,
                'backup': backup_rel, 'status': 'rolled_back',
                'runner_success': bool(out.get('success')),
                'runner_output': out.get('output', '')[-5000:],
                'metrics': out.get('metrics', {}),
                'quality_gate': quality_detail,
                'rationale': rationale,
            })
            try:
                repair_rag_id = self.rag.index_repair(self.state.job_id, stage, attempt, diagnosis or {}, rel, patch, rationale, 'rolled_back', out.get('output', ''), out.get('metrics', {}))
                self.graph.index_repair(self.state.job_id, stage, repair_rag_id, attempt, rel, 'rolled_back', patch, rationale, failure_rag_id)
            except Exception:
                pass
            self.persist()
            return None

        self.state.repair_history.append({
            'stage': stage, 'attempt': attempt, 'artifact': rel,
            'backup': backup_rel, 'status': 'accepted',
            'validation': validation_detail,
            'quality_gate': quality_detail,
            'metrics': out.get('metrics', {}),
            'rationale': rationale,
        })
        try:
            repair_rag_id = self.rag.index_repair(self.state.job_id, stage, attempt, diagnosis or {}, rel, patch, rationale, 'accepted', out.get('output', ''), out.get('metrics', {}))
            self.graph.index_repair(self.state.job_id, stage, repair_rag_id, attempt, rel, 'accepted', patch, rationale, failure_rag_id)
            self.graph.index_rag_evidence(self.state.job_id, repair_rag_id, rag_hits)
        except Exception:
            pass
        self.persist()
        return {'artifact': rel, 'result': out}

    async def generate(self, name, fn, rel):
        s = self.state.steps.setdefault(name, StepResult(name))
        hint = ''
        for attempt in range(1, MAX_RETRIES + 1):
            s.attempts = attempt
            s.start()
            self.persist()
            await self.emit('step_started', step=name, attempt=attempt)
            try:
                x = await fn(hint)
                if not x.strip():
                    raise RuntimeError('Generated artifact was empty after fence stripping')
                await self.write(rel, x)
                s.finish(True, 'Generated successfully')
                self.persist()
                await self.emit('step_completed', step=name, status='passed', attempt=attempt)
                return x
            except Exception as e:
                err = f'{type(e).__name__}: {e}'.strip()
                s.finish(False, error=err)
                d = await self.debug(name, err, '')
                hint = d.get('diagnosis') or err
                self.state.repair_history.append({'stage': name, 'attempt': attempt, 'diagnosis': d})
                self.persist()
                await self.emit('agent_repair', step=name, attempt=attempt, diagnosis=d, repaired=None)
        return None

    async def tool(self, name, runner):
        s = self.state.steps.setdefault(name, StepResult(name))
        for attempt in range(1, MAX_RETRIES + 1):
            s.attempts = attempt
            s.start()
            self.persist()
            await self.emit('step_started', step=name, attempt=attempt)
            try:
                stage_timeout = 30 if name == 'synthesis' else 1800
                out = await asyncio.wait_for(runner(self.workspace), timeout=stage_timeout)
            except Exception as e:
                out = {'success': False, 'return_code': -1, 'output': f'{type(e).__name__}: {e}', 'metrics': {}}

            s.finish(
                out['success'],
                out.get('output', ''),
                out.get('metrics', {}),
                None if out['success'] else out.get('output', ''),
            )
            if out['success'] and name == 'synthesis':
                for rel in ('synthesized.v', 'synthesized.json', 'synthesized.svg', 'synthesized.dot', 'synthesized.png'):
                    path = self.workspace / rel
                    if path.is_file():
                        self.state.artifacts[rel] = str(path)
            if out['success'] and name == 'simulation':
                for rel in ('results.xml', 'Makefile', 'sim_build/sim.vvp', 'wave.vcd', 'dump.vcd'):
                    path = self.workspace / rel
                    if path.is_file():
                        self.state.artifacts[rel] = str(path)
            if out['success'] and name == 'sta':
                for rel in ('sta.tcl', 'constraints.sdc', 'sta.log', 'sta_report.txt', 'sta_mapped.log', 'synthesis.ys'):
                    path = self.workspace / rel
                    if path.is_file():
                        self.state.artifacts[rel] = str(path)
            if name in {'physical_design', 'physical_verification'}:
                for path in self.workspace.rglob('*'):
                    if path.is_file() and path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.svg', '.def', '.lef', '.log', '.rpt', '.json', '.v', '.tcl', '.sdc', '.sh'}:
                        self.state.artifacts[str(path.relative_to(self.workspace))] = str(path)
            self.persist()
            if out['success']:
                await self.emit('step_completed', step=name, status='passed', attempt=attempt, metrics=s.metrics, output=s.output[-3000:])
                return True

            await self.emit('step_failed', step=name, attempt=attempt, error=s.error, output=s.output[-3000:])
            if _is_environment_tool_failure(s.output):
                diagnosis = {
                    'action': 'environment',
                    'diagnosis': 'The container runtime could not start; no RTL repair was attempted.',
                    'patch_instructions': 'Use a session with working rootless Podman/Docker user namespaces or configure a native Yosys/ABC toolchain.',
                }
                self.state.repair_history.append({
                    'stage': name,
                    'attempt': attempt,
                    'diagnosis': diagnosis,
                })
                self.persist()
                await self.emit('agent_repair', step=name, attempt=attempt, diagnosis=diagnosis, repaired=None)
                return False

            deterministic_environment_failure = (
                name == 'physical_verification'
                and any(marker in s.output for marker in (
                    'LVS: NOT RUN',
                    'configure LVS_REFERENCE_SPICE',
                    'DRC requires MAGIC_TECH_PATH',
                    'Physical verification requires a non-empty final.gds',
                ))
            )
            if deterministic_environment_failure:
                self.state.repair_history.append({
                    'stage': name,
                    'attempt': attempt,
                    'diagnosis': {
                        'action': 'environment',
                        'diagnosis': 'Physical verification prerequisites are missing; LLM repair skipped.',
                    },
                })
                self.persist()
                return False
            d = await self.debug(name, s.error or s.output, s.output)
            failure_rag_id = None
            try:
                failure_rag_id = self.rag.index_failure(self.state.job_id, name, s.output, d)
                self.graph.index_failure(self.state.job_id, name, failure_rag_id, d, s.output)
            except Exception as rag_error:
                await self.emit('rag_error', step=name, error=str(rag_error))
            self.state.repair_history.append({'stage': name, 'attempt': attempt, 'diagnosis': d, 'rag_failure_id': failure_rag_id})
            self.persist()

            repaired = None
            if attempt < MAX_RETRIES:
                repaired = await self.apply_repair(name, d, s.output, attempt, runner, failure_rag_id)
            await self.emit('agent_repair', step=name, attempt=attempt, diagnosis=d, repaired=repaired)
            if repaired:
                # apply_repair already executed and validated the candidate; use
                # that authoritative result rather than the original failed output.
                repaired_out = repaired['result']
                s.finish(True, repaired_out.get('output', ''), repaired_out.get('metrics', {}))
                self.persist()
                await self.emit(
                    'step_completed', step=name, status='passed', attempt=attempt,
                    repaired=True, artifact=repaired['artifact'],
                    metrics=repaired_out.get('metrics', {}),
                    output=repaired_out.get('output', '')[-3000:],
                )
                return True

        return False

    async def fail(self, stage):
        self.state.status = 'failed'
        self.state.report = build_report(self.state, f'Pipeline stopped at {stage}.')
        write_report(self.workspace, self.state.report)
        self.persist()
        await self.emit('pipeline_failed', failed_step=stage, report=self.state.report)
