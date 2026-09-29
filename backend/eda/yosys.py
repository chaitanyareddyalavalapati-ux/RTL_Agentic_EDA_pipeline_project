from backend.config import (
    KLAYOUT_CONTAINER_IMAGE,
    KLAYOUT_CONTAINER_RUNTIME,
    MOCK_EDA,
    OPENSTA_LIBERTY_PATH,
)
from backend.eda.base import MockAdapter
from backend.eda.shell import run_command
import os
import re
import shutil
import math
from pathlib import Path
from html import escape


def _write_netlist_svg(workspace):
    """Create a fast, dependency-free connected netlist preview from synthesized.v."""
    workspace = Path(workspace)
    netlist = workspace / 'synthesized.v'
    text = netlist.read_text(encoding='utf-8', errors='replace')
    input_names = set(re.findall(r'\binput(?:\s+\[[^]]+\])?\s+([^;]+);', text))
    input_names = {name.strip() for group in input_names for name in group.split(',')}
    output_names = set(re.findall(r'\boutput(?:\s+\[[^]]+\])?\s+([^;]+);', text))
    output_names = {name.strip() for group in output_names for name in group.split(',')}
    instances = []
    for match in re.finditer(
        r'\bsky130_fd_sc_hd__([A-Za-z0-9_]+)\s+(\S+)\s*\((.*?)\);',
        text,
        re.DOTALL,
    ):
        pins = re.findall(r'\.(\w+)\s*\(\s*([^()]+?)\s*\)', match.group(3))
        instances.append({'type': match.group(1), 'name': match.group(2), 'pins': pins})

    columns = min(8, max(2, math.ceil(math.sqrt(max(len(instances), 1) * 1.5))))
    rows = max(1, math.ceil(len(instances) / columns))
    width, height = 500 + columns * 220, max(620, 300 + rows * 90)
    cell_positions = {}
    cells = []
    for index, instance in enumerate(instances):
        x = 250 + (index % columns) * 220
        y = 240 + (index // columns) * 90
        cell_positions[instance['name']] = (x, y)
        pin_labels = ''.join(
            f'<text x="{x + (194 if is_output else -4)}" y="{y + 20 + pin_index * 18}" fill="#f6c85f" font-size="10" text-anchor="{alignment}">{escape(pin)}</text>'
            for pin_index, (pin, _) in enumerate(instance['pins'])
            for is_output in [pin.upper() in {'Y', 'X', 'Q', 'QN', 'O', 'Z', 'CO', 'COUT'}]
            for alignment in ['start' if is_output else 'end']
        )
        cells.append(
            f'<rect x="{x}" y="{y}" width="190" height="62" rx="8" fill="#244b9b" stroke="#9bb4ff" stroke-width="2"/>'
            f'<text x="{x + 12}" y="{y + 25}" fill="#ffffff" font-size="14" font-weight="bold">{escape(instance["type"])}</text>'
            f'<text x="{x + 12}" y="{y + 46}" fill="#b9c7e8" font-size="12">{escape(instance["name"])}</text>'
            f'{pin_labels}'
        )

    net_uses = {}
    for instance in instances:
        for pin, net in instance['pins']:
            net_uses.setdefault(net.strip(), []).append((instance['name'], pin))

    wire_lines = []
    net_labels = []
    for net, uses in net_uses.items():
        points = []
        for instance_name, pin in uses:
            x, y = cell_positions[instance_name]
            pin_index = next(i for i, item in enumerate(next(item['pins'] for item in instances if item['name'] == instance_name)) if item[0] == pin)
            pin_y = y + 20 + pin_index * 18
            is_output = pin.upper() in {'Y', 'X', 'Q', 'QN', 'O', 'Z', 'CO', 'COUT'}
            points.append((x + (190 if is_output else 0), pin_y))
        if net in input_names:
            points.append((90, 130 + len(net_labels) * 32))
        if net in output_names:
            points.append((width - 90, 130 + len(net_labels) * 32))
        if len(points) >= 2:
            source = points[0]
            for target in points[1:]:
                wire_lines.append(
                    f'<line x1="{source[0]}" y1="{source[1]}" x2="{target[0]}" y2="{target[1]}" stroke="#5ad48a" stroke-width="3" marker-end="url(#arrow)"/>'
                )
            net_labels.append(f'<text x="{min(point[0] for point in points) + 6}" y="{min(point[1] for point in points) - 6}" fill="#8ee6ad" font-size="11">{escape(net)}</text>')

    port_labels = ''.join(
        f'<text x="{30 if name in input_names else width - 160}" y="{130 + index * 32}" fill="#f6c85f" font-size="14">{escape(name)}</text>'
        for index, name in enumerate(sorted(input_names | output_names))
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#5ad48a"/></marker></defs>
<rect width="{width}" height="{height}" fill="#0b1020"/>
<rect x="25" y="30" width="{width - 50}" height="{height - 55}" fill="#121a2e" stroke="#58a6ff" stroke-width="3"/>
<text x="55" y="72" fill="#e8ecf5" font-family="sans-serif" font-size="24">Synthesized netlist graph</text>
<text x="55" y="98" fill="#9da8bd" font-family="sans-serif" font-size="16">Sky130 standard cells: {len(instances)} | green lines are net connections</text>
<g>{''.join(wire_lines)}</g>
<g>{''.join(cells)}</g>
<g font-family="sans-serif">{''.join(net_labels)}{port_labels}</g>
</svg>'''
    output = workspace / 'synthesized.svg'
    output.write_text(svg, encoding='utf-8')
    return output


class Yosys:
    async def run(self,workspace):
        if MOCK_EDA:
            return await MockAdapter('MOCK: Yosys synthesis passed', {'cell_count': 34, 'area': 1234.0}).run(workspace)

        workspace = Path(workspace).resolve()
        liberty = Path(OPENSTA_LIBERTY_PATH).expanduser().resolve()
        if not liberty.is_file():
            return {
                'success': False,
                'return_code': 1,
                'output': f'Synthesis requires a readable standard-cell Liberty file: {liberty}',
                'metrics': {},
            }

        runtime = KLAYOUT_CONTAINER_RUNTIME
        image = KLAYOUT_CONTAINER_IMAGE
        runtime_dir = workspace / '.container-runtime'
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_env = os.environ.copy()
        runtime_env['XDG_RUNTIME_DIR'] = str(runtime_dir)
        runtime_options = (
            ['--cgroup-manager=cgroupfs']
            if 'podman' in Path(runtime).name.lower()
            else []
        )
        command = [
            runtime, 'run', '--rm',
            *runtime_options,
            '-v', f'{workspace}:/work',
            '-v', f'{liberty.parent}:{liberty.parent}:ro',
            '-w', '/work',
            '--entrypoint', 'yosys', image,
            '-Q', '-s', 'synthesis.ys',
        ]
        try:
            result = await run_command(command, workspace, 180, env=runtime_env)
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)
        if not result['success']:
            return result
        netlist = workspace / 'synthesized.v'
        if not netlist.is_file() or netlist.stat().st_size == 0:
            return {
                'success': False,
                'return_code': 1,
                'output': result.get('output', '') + '\nYosys did not produce a mapped synthesized.v netlist.',
                'metrics': {},
            }
        svg = _write_netlist_svg(workspace)
        png = workspace / 'synthesized.png'
        rsvg = shutil.which('rsvg-convert')
        imagemagick = shutil.which('convert')
        if rsvg:
            render_command = [rsvg, '--output', str(png), str(svg)]
        elif imagemagick:
            render_command = [
                imagemagick, '-background', '#0b1020', '-density', '48',
                str(svg), '-resize', '1600x1600>', str(png),
            ]
        else:
            return {
                'success': False,
                'return_code': 127,
                'output': result.get('output', '') + '\nNetlist PNG generation requires rsvg-convert or ImageMagick convert.',
                'metrics': {},
            }
        rendered = await run_command(render_command, workspace, 30)
        if (not rendered.get('success') or not png.is_file() or png.stat().st_size == 0) and imagemagick:
            rendered = await run_command(
                [imagemagick, '-background', '#0b1020', '-density', '24', str(svg), '-resize', '1200x1200>', str(png)],
                workspace,
                30,
            )
        if not rendered.get('success') or not png.is_file() or png.stat().st_size == 0:
            return {
                'success': False,
                'return_code': rendered.get('return_code', 1),
                'output': result.get('output', '') + '\nNetlist PNG generation failed:\n' + rendered.get('output', ''),
                'metrics': {},
            }

        cells = re.findall(r'^\s+sky130_fd_sc_hd__\S+\s+(\d+)$', result.get('output', ''), re.MULTILINE)
        result['metrics'] = {'cell_count': sum(int(count) for count in cells)} if cells else result.get('metrics', {})

        return result
