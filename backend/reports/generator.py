from pathlib import Path
import json
import html


def _safe_text(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value)


def build_report(state, summary=''):
    step_map = {}
    for name, step in (state.steps or {}).items():
        step_map[name] = {
            'status': step.status,
            'attempts': step.attempts,
            'metrics': step.metrics,
            'error': step.error,
            'output': step.output[-4000:] if step.output else '',
        }
    return {
        'job_id': state.job_id,
        'prompt': state.prompt,
        'status': state.status,
        'plan': state.plan,
        'steps': step_map,
        'artifacts': state.artifacts,
        'repair_history': state.repair_history,
        'review': summary,
    }


def write_report(workspace, report):
    d = workspace / 'reports'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

    rows = ''.join(
        f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(v.get('status', 'pending')))}</td><td>{v.get('attempts', 0)}</td><td><pre>{html.escape(_safe_text(v.get('output', '')))}</pre></td></tr>"
        for n, v in (report.get('steps') or {}).items()
    )

    artifact_cards = []
    for name, path in (report.get('artifacts') or {}).items():
        lower = str(path).lower()
        if lower.endswith(('.png', '.jpg', '.jpeg', '.svg')):
            artifact_cards.append(
                f'<div class="artifact-card"><h4>{html.escape(name)}</h4><img src="{html.escape(path)}" alt="{html.escape(name)}"><p>{html.escape(path)}</p></div>'
            )
        else:
            artifact_cards.append(
                f'<div class="artifact-card"><h4>{html.escape(name)}</h4><pre>{html.escape(_safe_text(path))}</pre></div>'
            )

    page = f'''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>RTL Agent Report</title>
<style>
body{{font-family:Arial, max-width:1200px; margin:40px auto; padding:0 16px; background:#0f172a; color:#e2e8f0}}
.card{{background:#111827; border:1px solid #334155; border-radius:12px; padding:16px; margin-bottom:16px}}
pre{{white-space:pre-wrap; word-break:break-word; background:#020817; padding:12px; border-radius:8px; overflow:auto}}
img{{max-width:100%; max-height:420px; border-radius:10px; border:1px solid #475569; margin-top:8px}}
.artifact-grid{{display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap:16px}}
.artifact-card{{background:#0b1220; border:1px solid #334155; border-radius:10px; padding:12px}}
th, td{{border:1px solid #334155; padding:8px; vertical-align:top; text-align:left}}
button{{background:#2563eb; color:white; border:none; padding:8px 12px; border-radius:6px}}
</style>
<body>
  <div class="card">
    <h1>RTL Agent Report</h1>
    <p><strong>Status:</strong> {html.escape(str(report.get('status', 'unknown')))}</p>
    <p><strong>Job:</strong> {html.escape(str(report.get('job_id', '')))}</p>
    <h2>Request</h2>
    <pre>{html.escape(_safe_text(report.get('prompt', '')))}</pre>
  </div>

  <div class="card">
    <h2>Plan</h2>
    <pre>{html.escape(_safe_text(report.get('plan', {})))}</pre>
  </div>

  <div class="card">
    <h2>Steps</h2>
    <table>
      <tr><th>Step</th><th>Status</th><th>Attempts</th><th>Output</th></tr>
      {rows}
    </table>
  </div>

  <div class="card">
    <h2>Artifacts</h2>
    <div class="artifact-grid">{''.join(artifact_cards) or '<p>No artifacts produced yet.</p>'}</div>
  </div>

  <div class="card">
    <h2>Repair History</h2>
    <pre>{html.escape(_safe_text(report.get('repair_history', [])))}</pre>
  </div>

  <div class="card">
    <h2>Review</h2>
    <pre>{html.escape(_safe_text(report.get('review', '')))}</pre>
  </div>
</body>
</html>
'''
    (d / 'report.html').write_text(page, encoding='utf-8')

    markdown = '''# RTL Agent Report\n\n'''
    markdown += f"**Status:** {report.get('status', 'unknown')}\n\n"
    markdown += f"**Job:** {report.get('job_id', '')}\n\n"
    markdown += f"**Request:**\n{_safe_text(report.get('prompt', ''))}\n\n"
    markdown += "## Steps\n\n"
    for name, details in (report.get('steps') or {}).items():
        markdown += f"- {name}: {details.get('status', 'pending')} ({details.get('attempts', 0)} attempts)\n"
    if report.get('artifacts'):
        markdown += "\n## Artifacts\n\n"
        for name, path in (report.get('artifacts') or {}).items():
            markdown += f"- {name}: {path}\n"
    markdown += "\n## Review\n\n"
    markdown += _safe_text(report.get('review', '')) + '\n'
    (d / 'report.md').write_text(markdown, encoding='utf-8')
