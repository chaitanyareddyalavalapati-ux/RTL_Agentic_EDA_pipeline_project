import json
import re
from pathlib import Path
from html import escape

from backend.config import (
    MOCK_EDA,
    OPENROAD_CELL_GDS_PATH,
    OPENROAD_CELL_LEF_PATH,
    OPENROAD_CLOCK_BUFFER,
    OPENROAD_CONGESTION_ITERATIONS,
    OPENROAD_GDS_MAP_PATH,
    OPENROAD_PATH,
    OPENROAD_SDC_PATH,
    OPENROAD_TECH_LEF_PATH,
    OPENSTA_LIBERTY_PATH,
    KLAYOUT_CONTAINER_IMAGE,
    KLAYOUT_CONTAINER_RUNTIME,
    KLAYOUT_LAYER_PROPERTIES,
    KLAYOUT_STREAM_OUT_SCRIPT,
    KLAYOUT_TECH_FILE,
    LVS_ENABLED,
    LVS_EXTRACTED_SPICE,
    LVS_REFERENCE_SPICE,
    MAGIC_TECH_PATH,
    NETGEN_SETUP_PATH,
)
from backend.eda.base import MockAdapter
from backend.eda.shell import run_command, run_command_with_input


def _container_runtime_options():
    if 'podman' in Path(KLAYOUT_CONTAINER_RUNTIME).name.lower():
        return ['--cgroup-manager=cgroupfs']
    return []


def _write_layout_svg(workspace: Path, def_file: Path) -> Path:
    """Render the actual OpenROAD DEF floorplan, cells, pins, and nets."""
    text = def_file.read_text(encoding='utf-8', errors='replace')
    die = re.search(r'DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)', text)
    x0, y0, x1, y1 = (map(int, die.groups()) if die else (0, 0, 100000, 100000))
    width, height = max(x1 - x0, 1), max(y1 - y0, 1)
    left, top, right, bottom = 150, 130, 1050, 720

    def point(x, y):
        return left + (x - x0) / width * (right - left), bottom - (y - y0) / height * (bottom - top)

    components, component_map = [], {}
    block = re.search(r'COMPONENTS\s+\d+\s*;(.*?)END COMPONENTS', text, re.DOTALL)
    if block:
        for match in re.finditer(r'-\s+(\S+)\s+(\S+).*?\+\s+PLACED\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)', block.group(1), re.DOTALL):
            name, cell_type, x, y, _ = match.groups()
            px, py = point(int(x), int(y))
            item = {'name': name, 'type': cell_type, 'x': px, 'y': py}
            components.append(item)
            component_map[name] = item

    pins = {}
    pin_block = re.search(r'PINS\s+\d+\s*;(.*?)END PINS', text, re.DOTALL)
    if pin_block:
        for match in re.finditer(r'-\s+(\S+).*?\+\s+PLACED\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)', pin_block.group(1), re.DOTALL):
            pins[match.group(1)] = point(int(match.group(2)), int(match.group(3)))

    nets = []
    for net_section in ('NETS', 'SPECIALNETS'):
        net_block = re.search(rf'{net_section}\s+\d+\s*;(.*?)END {net_section}', text, re.DOTALL)
        if not net_block:
            continue
        for match in re.finditer(r'-\s+(\S+)\s+(.*?);', net_block.group(1), re.DOTALL):
            name, connections = match.groups()
            points = []
            routed = []
            for instance, pin in re.findall(r'\(\s*(\S+)\s+(\S+)\s*\)', connections):
                if instance == 'PIN' and pin in pins:
                    points.append(pins[pin])
                elif instance in component_map:
                    cell = component_map[instance]
                    points.append((cell['x'], cell['y']))
            for layer, sx, sy, ex, ey in re.findall(
                r'(?:ROUTED|NEW)\s+(\S+)(?:\s+-?\d+(?:\.\d+)?)?(?:\s+\+\s+SHAPE\s+\S+)?\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(\*|-?\d+)\s+(\*|-?\d+)\s*\)',
                connections,
            ):
                start = (int(sx), int(sy))
                end = (
                    start[0] if ex == '*' else int(ex),
                    start[1] if ey == '*' else int(ey),
                )
                routed.append((layer, point(*start), point(*end)))
            if routed or len(points) >= 2:
                nets.append((name, points, routed))

    colors = ['#22d3ee', '#facc15', '#fb7185', '#a78bfa', '#4ade80', '#fb923c']
    net_svg = []
    layer_colors = {'li1': '#94a3b8', 'met1': '#22d3ee', 'met2': '#facc15', 'met3': '#fb7185', 'met4': '#a78bfa', 'met5': '#4ade80'}
    all_routed = []
    for index, (name, points, routed) in enumerate(nets):
        all_routed.extend(routed)
        color = colors[index % len(colors)]
        for layer, start, end in routed:
            route_color = layer_colors.get(layer, color)
            net_svg.append(f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" stroke="{route_color}" stroke-width="3" opacity="0.84"/>')
        if routed:
            label_x, label_y = routed[0][1]
        elif points:
            label_x, label_y = points[0]
        else:
            continue
        net_svg.append(f'<text x="{label_x + 5:.1f}" y="{label_y - 5:.1f}" fill="{color}" font-size="10">{escape(name)}</text>')

    cell_svg = []
    for index, cell in enumerate(components):
        color = colors[index % len(colors)]
        cell_svg.append(f'<rect x="{cell["x"] - 17:.1f}" y="{cell["y"] - 12:.1f}" width="34" height="24" fill="{color}" fill-opacity="0.32" stroke="{color}" stroke-width="1.4"/>')
        cell_svg.append(f'<text x="{cell["x"] - 14:.1f}" y="{cell["y"] + 3:.1f}" fill="#f8fafc" font-size="8">{escape(cell["name"])}</text>')

    detail_points = [(cell['x'], cell['y']) for cell in components]
    detail_points += [point for _, start, end in all_routed for point in (start, end)]
    if detail_points:
        detail_x0 = max(left, min(x for x, _ in detail_points) - 90)
        detail_x1 = min(right, max(x for x, _ in detail_points) + 90)
        detail_y0 = max(top, min(y for _, y in detail_points) - 70)
        detail_y1 = min(bottom, max(y for _, y in detail_points) + 70)
    else:
        detail_x0, detail_y0, detail_x1, detail_y1 = left, top, right, bottom
    detail_width = max(detail_x1 - detail_x0, 1)
    detail_height = max(detail_y1 - detail_y0, 1)

    pin_svg = ''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#f8fafc" stroke="#facc15" stroke-width="2"/>' for x, y in pins.values())
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 820">
<rect width="1200" height="820" fill="#050816"/>
<rect x="20" y="20" width="1160" height="780" rx="8" fill="#0b1020" stroke="#334155"/>
<text x="48" y="60" fill="#f8fafc" font-family="sans-serif" font-size="25" font-weight="bold">OpenROAD physical design</text>
<text x="48" y="88" fill="#94a3b8" font-family="sans-serif" font-size="15">Routed DEF: {escape(def_file.name)} | cells: {len(components)} | nets: {len(nets)} | die: {width / 1000:.1f} x {height / 1000:.1f} um</text>
<rect x="{left}" y="{top}" width="{right - left}" height="{bottom - top}" fill="#111827" stroke="#e2e8f0" stroke-width="2"/>
<g>{''.join(net_svg)}</g><g>{''.join(cell_svg)}</g><g>{pin_svg}</g>
<rect x="700" y="42" width="450" height="270" rx="6" fill="#080d1a" stroke="#facc15" stroke-width="2"/>
<text x="720" y="70" fill="#facc15" font-size="16" font-weight="bold">Routed detail</text>
<g transform="translate(720 88) scale({min(410 / detail_width, 205 / detail_height):.4f}) translate({-detail_x0:.1f} {-detail_y0:.1f})">{''.join(net_svg)}<g>{''.join(cell_svg)}</g></g>
<text x="48" y="756" fill="#5eead4" font-family="sans-serif" font-size="14">Actual routed DEF metal segments: li1 / met1 / met2 / met3 / met4 / met5</text>
<text x="48" y="778" fill="#94a3b8" font-family="sans-serif" font-size="13">OpenROAD routing and GDS stream-out; DRC/LVS evidence is in drc.log and lvs.log</text>
</svg>'''
    output = workspace / 'physical_layout.svg'
    output.write_text(svg, encoding='utf-8')
    return output


class OpenLane:
    async def physical_design(self,workspace):
        if MOCK_EDA: return await MockAdapter('MOCK: OpenLane/OpenROAD passed',{'floorplan':'pass','placement':'pass','cts':'pass','routing':'pass'}).run(workspace)
        workspace = Path(workspace).resolve()
        config = json.loads((workspace / 'config.json').read_text())
        top = config.get('DESIGN_NAME', 'adder4')
        netlist = workspace / 'synthesized.v'
        match = re.search(r'\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)', netlist.read_text(encoding='utf-8', errors='replace'))
        if match:
            top = match.group(1)
        required_files = {
            'OpenROAD executable (OPENROAD_PATH)': OPENROAD_PATH,
            'technology LEF (OPENROAD_TECH_LEF_PATH)': OPENROAD_TECH_LEF_PATH,
            'standard-cell LEF (OPENROAD_CELL_LEF_PATH)': OPENROAD_CELL_LEF_PATH,
            'standard-cell Liberty (OPENSTA_LIBERTY_PATH)': OPENSTA_LIBERTY_PATH,
        }
        missing = [
            f'{label}: {path or "<unset>"}'
            for label, path in required_files.items()
            if not path or not Path(path).is_file()
        ]
        if missing:
            return {
                'success': False,
                'output': 'OpenROAD routing prerequisites are missing:\n- ' + '\n- '.join(missing),
            }

        streamout_files = {
            'standard-cell GDS (OPENROAD_CELL_GDS_PATH)': OPENROAD_CELL_GDS_PATH,
            'GDS layer map (OPENROAD_GDS_MAP_PATH)': OPENROAD_GDS_MAP_PATH,
            'KLayout technology file (KLAYOUT_TECH_FILE)': KLAYOUT_TECH_FILE,
            'KLayout layer properties (KLAYOUT_LAYER_PROPERTIES)': KLAYOUT_LAYER_PROPERTIES,
        }
        missing_streamout = [
            f'{label}: {path or "<unset>"}'
            for label, path in streamout_files.items()
            if not path or not Path(path).is_file()
        ]
        if not KLAYOUT_STREAM_OUT_SCRIPT:
            missing_streamout.append('KLayout stream-out script (KLAYOUT_STREAM_OUT_SCRIPT): <unset>')
        sdc_path = Path(OPENROAD_SDC_PATH) if OPENROAD_SDC_PATH else workspace / 'constraints.sdc'
        script = workspace / 'openroad.tcl'
        script.write_text(f'''read_lef {{{OPENROAD_TECH_LEF_PATH}}}
read_lef {{{OPENROAD_CELL_LEF_PATH}}}
read_liberty {{{OPENSTA_LIBERTY_PATH}}}
read_verilog synthesized.v
link_design {top}
''' +(f'''read_sdc {{{sdc_path}}}
''' if sdc_path.is_file() else '')+f'''initialize_floorplan -die_area {{0 0 100 100}} -core_area {{10 10 90 90}} -site unithd
add_global_connection -defer_connection -net {{VDD}} -inst_pattern {{.*}} -pin_pattern {{VPWR}} -power
add_global_connection -defer_connection -net {{VDD}} -inst_pattern {{.*}} -pin_pattern {{VPB}}
add_global_connection -defer_connection -net {{VSS}} -inst_pattern {{.*}} -pin_pattern {{VGND}} -ground
add_global_connection -defer_connection -net {{VSS}} -inst_pattern {{.*}} -pin_pattern {{VNB}}
global_connect
set_voltage_domain -name {{CORE}} -power {{VDD}} -ground {{VSS}}
define_pdn_grid -name {{grid}} -voltage_domains {{CORE}}
add_pdn_stripe -grid {{grid}} -layer {{met1}} -width {{0.48}} -pitch {{5.44}} -offset {{0}} -followpins
add_pdn_stripe -grid {{grid}} -layer {{met4}} -width {{1.6}} -pitch {{27.14}} -offset {{13.57}}
add_pdn_stripe -grid {{grid}} -layer {{met5}} -width {{1.6}} -pitch {{27.2}} -offset {{13.6}}
add_pdn_connect -grid {{grid}} -layers {{met1 met4}}
add_pdn_connect -grid {{grid}} -layers {{met4 met5}}
pdngen
make_tracks li1 -x_pitch 0.46 -y_pitch 0.46 -x_offset 0.23 -y_offset 0.23
make_tracks met1 -x_pitch 0.34 -y_pitch 0.34 -x_offset 0.17 -y_offset 0.17
make_tracks met2 -x_pitch 0.46 -y_pitch 0.46 -x_offset 0.23 -y_offset 0.23
make_tracks met3 -x_pitch 0.46 -y_pitch 0.46 -x_offset 0.23 -y_offset 0.23
make_tracks met4 -x_pitch 0.46 -y_pitch 0.46 -x_offset 0.23 -y_offset 0.23
make_tracks met5 -x_pitch 0.46 -y_pitch 0.46 -x_offset 0.23 -y_offset 0.23
place_pins -hor_layers met1 -ver_layers met2
global_placement -density 0.7
estimate_parasitics -placement
repair_design
detailed_placement
if {{[llength [get_clocks *]] > 0}} {{
  clock_tree_synthesis -root_buf {{{OPENROAD_CLOCK_BUFFER}}}
  repair_clock_nets
  detailed_placement
}}
set_wire_rc -clock -layer met2
set_wire_rc -signal -layer met2
global_route -guide route.guide -congestion_iterations {OPENROAD_CONGESTION_ITERATIONS}
detailed_route
if {{![design_is_routed]}} {{
    error "OpenROAD reports unrouted nets after detailed_route"
}}
write_def final.def
report_design_area
exit
''', encoding='utf-8')
        result = await run_command([OPENROAD_PATH, str(script.resolve())], workspace, 1800)
        (workspace / 'physical_design.log').write_text(result.get('output', ''), encoding='utf-8')
        area = re.search(
            r'Design area\s+([\d.]+)\s+um\^2\s+([\d.]+)%\s+utilization',
            result.get('output', ''),
            re.IGNORECASE,
        )
        def_file = workspace / 'final.def'
        gds_file = workspace / 'final.gds'
        # DEF is already a real routed physical result; render it even when
        # later GDS stream-out fails so the dashboard can show useful evidence.
        if def_file.is_file():
            _write_layout_svg(workspace, def_file)
        if '[ERROR' in result.get('output', '') or 'Error:' in result.get('output', ''):
            result['success'] = False
        if result.get('success') and def_file.is_file():
            if missing_streamout:
                result['metrics'] = {
                    'floorplan': 'pass',
                    'placement': 'pass',
                    'cts': 'pass or no clocks present',
                    'routing': 'pass',
                    'routed_def': 'design_is_routed pass',
                    'gds_stream_out': 'not configured',
                }
                result['output'] += (
                    '\nRouted DEF and physical layout were generated; GDS stream-out was skipped '
                    'because prerequisites are missing:\n- ' + '\n- '.join(missing_streamout)
                )
                return result
            stream_command = [
                KLAYOUT_CONTAINER_RUNTIME, 'run', '--rm',
                *_container_runtime_options(),
                '-v', f'{workspace.resolve()}:/work',
                '-v', f'{Path(OPENROAD_CELL_LEF_PATH).parent.resolve()}:/inputs/cell_lef:ro',
                '-v', f'{Path(OPENROAD_CELL_GDS_PATH).parent.resolve()}:/inputs/cell_gds:ro',
                '-v', f'{Path(OPENROAD_GDS_MAP_PATH).parent.resolve()}:/inputs/gds_map:ro',
                '-v', f'{Path(KLAYOUT_TECH_FILE).parent.resolve()}:/inputs/klayout_tech:ro',
                '-v', f'{Path(KLAYOUT_LAYER_PROPERTIES).parent.resolve()}:/inputs/klayout_props:ro',
                KLAYOUT_CONTAINER_IMAGE,
                KLAYOUT_STREAM_OUT_SCRIPT,
                '-o', '/work/final.gds',
                '-l', f'/inputs/cell_lef/{Path(OPENROAD_CELL_LEF_PATH).name}',
                '-T', f'/inputs/klayout_tech/{Path(KLAYOUT_TECH_FILE).name}',
                '-P', f'/inputs/klayout_props/{Path(KLAYOUT_LAYER_PROPERTIES).name}',
                '-M', f'/inputs/gds_map/{Path(OPENROAD_GDS_MAP_PATH).name}',
                '-w', f'/inputs/cell_gds/{Path(OPENROAD_CELL_GDS_PATH).name}',
                '-t', top,
                '/work/final.def',
            ]
            stream_result = await run_command(stream_command, workspace, 1800)
            result['output'] = result.get('output', '') + '\n' + stream_result.get('output', '')
            result['success'] = stream_result.get('success', False)
        if result.get('success') and def_file.is_file() and gds_file.is_file() and gds_file.stat().st_size > 0:
            result['metrics'] = {
                'floorplan': 'pass',
                'placement': 'pass',
                'cts': 'pass or no clocks present',
                'routing': 'pass',
                'routed_def': 'design_is_routed pass',
                'gds_stream_out': 'pass',
            }
            if area:
                result['metrics'].update({
                    'area_um2': float(area.group(1)),
                    'utilization_pct': float(area.group(2)),
                })
        elif result.get('success'):
            result['success'] = False
            result['output'] = result.get('output', '') + '\nOpenROAD did not produce a routed final.def and non-empty final.gds'
        return result
    async def physical_verification(self,workspace):
        if MOCK_EDA: return await MockAdapter('MOCK: DRC/LVS passed',{'drc':'pass','lvs':'pass'}).run(workspace)
        workspace = Path(workspace).resolve()
        gds_file = workspace / 'final.gds'
        if not gds_file.is_file() or gds_file.stat().st_size == 0:
            return {'success': False, 'output': 'Physical verification requires a non-empty final.gds'}
        if not MAGIC_TECH_PATH or not Path(MAGIC_TECH_PATH).is_dir():
            return {'success': False, 'output': 'DRC requires MAGIC_TECH_PATH pointing to the Sky130 Magic technology directory'}
        drc_command = [
            KLAYOUT_CONTAINER_RUNTIME, 'run', '--rm', '-i',
            *_container_runtime_options(),
            '-v', f'{workspace}:/work',
            '-v', f'{Path(MAGIC_TECH_PATH).resolve()}:/pdk/magic:ro',
            KLAYOUT_CONTAINER_IMAGE,
            'magic', '-dnull', '-noconsole', '-T', 'sky130A', '/dev/stdin',
        ]
        drc_script = '\n'.join([
            'gds read /work/final.gds',
            'load ' + re.search(r'^DESIGN\s+(\S+)', (workspace / 'final.def').read_text(), re.MULTILINE).group(1),
            'select top cell',
            'drc check',
            'drc count',
            'quit -noprompt',
        ]) + '\n'
        drc_result = await run_command_with_input(drc_command, workspace, drc_script, 1800)
        (workspace / 'drc.log').write_text(drc_result.get('output', ''), encoding='utf-8')
        drc_match = re.search(r'Total DRC errors found:\s*(\d+)', drc_result.get('output', ''))
        drc_pass = drc_result.get('success') and drc_match and int(drc_match.group(1)) == 0
        output = 'DRC: ' + ('PASS (0 errors)' if drc_pass else 'FAIL\n' + drc_result.get('output', ''))
        if not LVS_ENABLED:
            output += '\nLVS: DISABLED (set LVS_ENABLED=true to run Netgen LVS)'
            return {'success': bool(drc_pass), 'output': output, 'metrics': {'drc': 'pass' if drc_pass else 'fail', 'lvs': 'disabled'}}
        reference_spice = Path(LVS_REFERENCE_SPICE) if LVS_REFERENCE_SPICE else workspace / 'reference_pdn.spice'
        extracted_spice = Path(LVS_EXTRACTED_SPICE) if LVS_EXTRACTED_SPICE else workspace / 'extracted_pdn.spice'
        netgen_setup = Path(NETGEN_SETUP_PATH) if NETGEN_SETUP_PATH else None
        if netgen_setup is None:
            candidates = list(Path('/home/mirafra/.volare').glob('volare/sky130/versions/*/sky130A/libs.tech/netgen/sky130A_setup.tcl'))
            netgen_setup = candidates[0] if candidates else None
        if not all((reference_spice.is_file(), extracted_spice.is_file(), netgen_setup and netgen_setup.is_file())):
            output += '\nLVS: NOT RUN (configure LVS_REFERENCE_SPICE, LVS_EXTRACTED_SPICE, and NETGEN_SETUP_PATH with real SPICE netlists)'
            return {'success': False, 'output': output, 'metrics': {'drc': 'pass' if drc_pass else 'fail', 'lvs': 'not run'}}
        top_match = re.search(r'^DESIGN\s+(\S+)', (workspace / 'final.def').read_text(), re.MULTILINE)
        top = top_match.group(1) if top_match else 'top'
        lvs_command = [
            KLAYOUT_CONTAINER_RUNTIME, 'run', '--rm',
            *_container_runtime_options(),
            '-v', f'{workspace}:/work',
            '-v', f'{reference_spice.parent.resolve()}:/inputs/reference:ro',
            '-v', f'{extracted_spice.parent.resolve()}:/inputs/extracted:ro',
            '-v', f'{netgen_setup.parent.resolve()}:/inputs/netgen:ro',
            KLAYOUT_CONTAINER_IMAGE, 'netgen', '-batch', 'lvs',
            f'/inputs/extracted/{extracted_spice.name} {top}',
            f'/inputs/reference/{reference_spice.name} {top}',
            f'/inputs/netgen/{netgen_setup.name}', '/work/lvs.log',
        ]
        lvs_result = await run_command(lvs_command, workspace, 1800)
        (workspace / 'lvs.log').write_text(lvs_result.get('output', ''), encoding='utf-8')
        lvs_output = lvs_result.get('output', '')
        lvs_pass = (
            lvs_result.get('success')
            and not re.search(r'failed|mismatch|error|could not open|not match', lvs_output, re.IGNORECASE)
            and bool(re.search(r'netlists match|final result:\s*(?:pass|match)', lvs_output, re.IGNORECASE))
        )
        output += '\nLVS: ' + ('PASS' if lvs_pass else 'FAIL\n' + lvs_result.get('output', ''))
        return {'success': bool(drc_pass and lvs_pass), 'output': output, 'metrics': {'drc': 'pass' if drc_pass else 'fail', 'lvs': 'pass' if lvs_pass else 'fail'}}
