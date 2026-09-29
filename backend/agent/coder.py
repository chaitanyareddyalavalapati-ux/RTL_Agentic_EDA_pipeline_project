import re
import json
import asyncio
import os

from backend.config import CODER_MODEL
from backend.agent.llm import OllamaClient, strip_fences, parse_json
from backend.agent.prompts import CODER_SYSTEM, TB_SYSTEM, FLOW_REPAIR_SYSTEM, REPAIR_PATCH_SYSTEM
from backend.agent.specification import validate_rtl

_MODULE = re.compile(r'(?ms)^[ \t]*module\b.*?^[ \t]*endmodule\b[ \t]*;?')
_SIM_ONLY = re.compile(r'\binitial\b|\$finish|\$stop|\$display|\$monitor|\$dumpfile|\$dumpvars', re.I)


def _with_hint(user, hint):
    if not hint:
        return user
    return f'{user}\nThe previous attempt failed: {hint}\nAvoid repeating that failure.'


def keep_synthesizable(text, top=''):
    if not text:
        return text
    modules = _MODULE.findall(text)
    if not modules:
        return text
    clean = [m for m in modules if not _SIM_ONLY.search(m)]
    if not clean or len(clean) == len(modules):
        return text
    if top and not any(re.search(rf'\bmodule\s+{re.escape(top)}\b', m) for m in clean):
        return text
    directives = [l for l in text.splitlines() if l.strip().startswith('`') and 'timescale' not in l]
    body = '\n\n'.join(m.strip() for m in clean)
    return ('\n'.join(directives) + '\n\n' + body).strip() if directives else body


class Coder:
    def __init__(self):
        self.llm = OllamaClient(CODER_MODEL)

    async def rtl(self, plan, request, hint=''):
        if plan.get('status') == 'needs_clarification':
            raise ValueError('Missing requirements: ' + ', '.join(plan.get('missing_requirements', [])))
        if os.getenv('LLM_CODEGEN_ENABLED', 'false').lower() != 'true':
            fallback = self._fallback_rtl(plan)
            errors = validate_rtl(fallback, plan)
            if errors:
                raise ValueError('Generated RTL does not conform: ' + '; '.join(errors))
            return keep_synthesizable(fallback, str((plan or {}).get('top_module') or ''))
        try:
            response = await asyncio.wait_for(self.llm.chat(
                CODER_SYSTEM,
                _with_hint(f'Structured specification:\n{json.dumps(plan, sort_keys=True)}\nOriginal request:\n{request}', hint),
            ), timeout=120)
            text = strip_fences(response)
        except Exception:
            text = self._fallback_rtl()
        text = keep_synthesizable(text, str((plan or {}).get('top_module') or ''))
        errors = validate_rtl(text, plan)
        if errors:
            raise ValueError('Generated RTL does not conform: ' + '; '.join(errors))
        return text

    @staticmethod
    def _fallback_rtl(spec):
        design_type = spec.get('design_type')
        name = spec.get('top_module') or 'rtl_design'
        width = int(spec.get('data_width') or 1)
        depth = int(spec.get('depth') or 1)
        address_width = int(spec.get('address_width') or max(1, (depth - 1).bit_length()))
        if design_type == 'rom':
            contents = spec.get('rom_contents') or [0] * depth
            entries = '\n'.join(
                f"            {address_width}'d{address}: read_data = {width}'h{value:0{max(1, (width + 3) // 4)}X};"
                for address, value in enumerate(contents[:depth])
            )
            return f'''module {name} (
    input wire [{address_width - 1}:0] addr,
    output reg [{width - 1}:0] read_data
);
    always @* begin
        case (addr)
{entries}
            default: read_data = '0;
        endcase
    end
endmodule
'''
        if design_type in {'ram', 'register_file'}:
            return f'''module {name} (
    input wire clk,
    input wire we,
    input wire [{address_width - 1}:0] addr,
    input wire [{width - 1}:0] write_data,
    output reg [{width - 1}:0] read_data
);
    reg [{width - 1}:0] mem [0:{depth - 1}];
    always @(posedge clk) begin
        if (we) mem[addr] <= write_data;
        read_data <= mem[addr];
    end
endmodule
'''
        if design_type == 'counter':
            return f'''module {name} (
    input wire clk,
    input wire rst,
    output reg [{width - 1}:0] count
);
    always @(posedge clk) begin
        if (rst) count <= 0;
        else count <= count + 1'b1;
    end
endmodule
'''
        if design_type == 'alu':
            return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    input wire [2:0] op,
    output reg [{width - 1}:0] result
);
    always @* begin
        case (op)
            3'd0: result = a + b;
            3'd1: result = a - b;
            3'd2: result = a & b;
            3'd3: result = a | b;
            default: result = '0;
        endcase
    end
endmodule
'''
        if design_type == 'fifo':
            return f'''module {name} (
    input wire clk,
    input wire rst,
    input wire wr_en,
    input wire rd_en,
    input wire [{width - 1}:0] write_data,
    output reg [{width - 1}:0] read_data,
    output wire full,
    output wire empty
);
    reg [{width - 1}:0] mem [0:{depth - 1}];
    reg [{address_width}:0] count;
    reg [{address_width - 1}:0] wr_ptr, rd_ptr;
    assign full = (count == {depth});
    assign empty = (count == 0);
    always @(posedge clk) begin
        if (rst) begin count <= 0; wr_ptr <= 0; rd_ptr <= 0; end
        else begin
            if (wr_en && !full) begin mem[wr_ptr] <= write_data; wr_ptr <= wr_ptr + 1'b1; end
            if (rd_en && !empty) begin read_data <= mem[rd_ptr]; rd_ptr <= rd_ptr + 1'b1; end
        end
    end
endmodule
'''
        if design_type == 'dff':
            return f'''module {name} (
    input wire clk,
    input wire rst_n,
    input wire d,
    output reg q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            q <= 1'b0;
        else
            q <= d;
    end
endmodule
'''
        if design_type in {'adder', 'subtractor', 'comparator', 'mux', 'combinational'}:
            if design_type == 'adder':
                return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    input wire cin,
    output wire [{width - 1}:0] sum,
    output wire carry_out
);
    wire [{width}:0] carry;
    assign carry[0] = cin;
    genvar i;
    generate
        for (i = 0; i < {width}; i = i + 1) begin : add_bits
            assign sum[i] = a[i] ^ b[i] ^ carry[i];
            assign carry[i + 1] = (a[i] & b[i]) | (carry[i] & (a[i] ^ b[i]));
        end
    endgenerate
    assign carry_out = carry[{width}];
endmodule
'''
            if design_type == 'subtractor':
                return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    input wire bin,
    output wire [{width - 1}:0] diff,
    output wire borrow_out
);
    wire [{width}:0] borrow;
    assign borrow[0] = bin;
    genvar i;
    generate
        for (i = 0; i < {width}; i = i + 1) begin : sub_bits
            assign diff[i] = a[i] ^ b[i] ^ borrow[i];
            assign borrow[i + 1] = (~a[i] & (b[i] | borrow[i])) | (b[i] & borrow[i]);
        end
    endgenerate
    assign borrow_out = borrow[{width}];
endmodule
'''
            if design_type == 'comparator':
                return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    output wire eq,
    output wire gt,
    output wire lt
);
    assign eq = (a == b);
    assign gt = (a > b);
    assign lt = (a < b);
endmodule
'''
            if design_type == 'mux':
                return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    input wire sel,
    output wire [{width - 1}:0] out
);
    assign out = sel ? b : a;
endmodule
'''
            return f'''module {name} (
    input wire [{width - 1}:0] a,
    input wire [{width - 1}:0] b,
    output wire [{width - 1}:0] out
);
    assign out = a + b;
endmodule
'''
        raise ValueError(f'No deterministic RTL template for design type {design_type!r}; enable LLM code generation')

    @staticmethod
    def _fallback_testbench(plan=None, rtl=''):
        plan = plan or {}
        module_name = str(plan.get('top_module') or 'design_under_test')
        ports = plan.get('ports') or ['clk', 'rst_n', 'd', 'q']
        port_names = [p for p in ports if p not in {'clk'}]
        design_type = plan.get('design_type')
        width = max(1, int(plan.get('data_width') or 1))
        if design_type == 'adder':
            return '''import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def adder_functional_tests(dut):
    width = __WIDTH__
    mask = (1 << width) - 1
    vectors = [(0, 0, 0), (mask, mask, 1), (mask, 0, 1), (0, mask, 0)]
    if width <= 4:
        vectors = [(a, b, cin) for a in range(mask + 1) for b in range(mask + 1) for cin in (0, 1)]
    else:
        import random
        generator = random.Random(2026)
        vectors.extend((generator.randrange(mask + 1), generator.randrange(mask + 1), generator.randrange(2)) for _ in range(128))

    for a, b, cin in vectors:
        dut.a.value = a
        dut.b.value = b
        dut.cin.value = cin
        await Timer(1, 'ns')
        total = a + b + cin
        assert int(dut.sum.value) == (total & mask), f"sum mismatch for {a}+{b}+{cin}"
        assert int(dut.carry_out.value) == (total >> width), f"carry mismatch for {a}+{b}+{cin}"

    dut._log.info("PASS vectors=%d", len(vectors))
'''.replace('__WIDTH__', str(width))
        if plan.get('design_type') == 'dff':
            return """import cocotb
from cocotb.triggers import Timer

async def tick(dut):
    dut.clk.value = 0
    await Timer(1, 'ns')
    dut.clk.value = 1
    await Timer(1, 'ns')

@cocotb.test()
async def dff_smoke_test(dut):
    dut.rst_n.value = 0
    dut.d.value = 0
    await tick(dut)
    await tick(dut)
    await tick(dut)

    dut.rst_n.value = 1
    for val in [0, 1, 0, 1]:
        dut.d.value = val
        await tick(dut)
        assert dut.q.value == val, f\"Expected q={val}, got {{dut.q.value}}\"

    dut._log.info(\"DFF smoke test passed\")
"""
        if plan.get('design_type') in {'ram', 'register_file'}:
            width = max(1, int(plan.get('data_width') or 1))
            depth = max(1, int(plan.get('depth') or 1))
            return '''import cocotb
from cocotb.triggers import Timer

async def tick(dut):
    dut.clk.value = 0
    await Timer(1, unit="ns")
    dut.clk.value = 1
    await Timer(1, unit="ns")
    dut.clk.value = 0

@cocotb.test()
async def ram_address_tests(dut):
    width = __WIDTH__
    depth = __DEPTH__
    mask = (1 << width) - 1
    expected = {}

    dut.clk.value = 0
    dut.we.value = 0
    dut.addr.value = 0
    dut.write_data.value = 0
    await Timer(1, unit="ns")

    for address in range(depth):
        value = 0 if address == 0 else mask if address == depth - 1 else (address * 7 + 3) & mask
        dut.we.value = 1
        dut.addr.value = address
        dut.write_data.value = value
        await tick(dut)
        expected[address] = value

    dut.we.value = 0
    for address, value in expected.items():
        dut.addr.value = address
        await tick(dut)
        actual = int(dut.read_data.value)
        assert actual == value, f"RAM[{address}] expected {value}, got {actual}"

    dut._log.info("PASS addresses=%d width=%d", depth, width)
'''.replace('__WIDTH__', str(width)).replace('__DEPTH__', str(depth))

        if plan.get('design_type') == 'rom':
            contents = tuple(plan.get('rom_contents') or [])
            return f'''import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def rom_address_tests(dut):
    contents = {contents!r}
    for address, expected in enumerate(contents):
        dut.addr.value = address
        await Timer(1, unit="ns")
        actual = int(dut.read_data.value)
        assert actual == expected, f"ROM[{{address}}] expected {{expected}}, got {{actual}}"
    dut._log.info("PASS addresses=%d width=%d", len(contents), {width})
'''

        output_like = {
            'read_data', 'out', 'sum', 'diff', 'q', 'eq', 'gt', 'lt', 'carry_out',
            'borrow_out', 'full', 'empty', 'result'
        }
        assignments = []
        for name in port_names:
            if name in output_like:
                continue
            assignments.append(f"    dut.{name}.value = 0")
        setup = chr(10).join(assignments) if assignments else "    pass"
        design_type = plan.get('design_type')
        if design_type == 'mux':
            checks = f"""    mask = (1 << {width}) - 1
    for sel, expected in [(0, 0), (1, 0), (0, mask), (1, mask)]:
        dut.sel.value = sel
        dut.a.value = 0 if sel == 0 else mask
        dut.b.value = mask if sel == 0 else 0
        await Timer(1, 'ns')
        assert int(dut.out.value) == expected
"""
        elif design_type == 'comparator':
            checks = f"""    mask = (1 << {width}) - 1
    for a, b in [(0, 0), (0, 1), (1, 0), (mask, mask), (mask, 3 & mask)]:
        dut.a.value, dut.b.value = a, b
        await Timer(1, 'ns')
        assert int(dut.eq.value) == int(a == b)
        assert int(dut.gt.value) == int(a > b)
        assert int(dut.lt.value) == int(a < b)
"""
        else:
            checks = f"""    mask = (1 << {width}) - 1
    for a, b in [(0, 0), (1 & mask, 2 & mask), (mask, 1 & mask), (mask, mask)]:
        dut.a.value, dut.b.value = a, b
        await Timer(1, 'ns')
        assert int(dut.out.value) == ((a + b) & mask)
"""
        return f'''import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def generated_design_boundary_tests(dut):
{setup}
{checks}    dut._log.info("Generated testbench passed boundary cases for module {module_name}")
'''

    async def testbench(self, plan, rtl, hint=''):
        if os.getenv('LLM_CODEGEN_ENABLED', 'false').lower() != 'true':
            return self._fallback_testbench(plan, rtl)
        try:
            response = await asyncio.wait_for(self.llm.chat(
                TB_SYSTEM,
                _with_hint(f'Plan:\n{plan}\nRTL:\n{rtl}', hint),
            ), timeout=120)
            return strip_fences(response)
        except Exception:
            return self._fallback_testbench(plan, rtl)

    async def repair_patch(self, target, artifact, diagnosis, log, path=''):
        """Ask the LLM for a machine-applicable unified diff, never a blind replacement."""
        system = REPAIR_PATCH_SYSTEM
        if target == 'rtl':
            system += '\nThe artifact is synthesizable SystemVerilog; preserve synthesizability and the existing module interface unless the log proves it is wrong.'
        elif target == 'testbench':
            system += '\nThe artifact is a Cocotb Python testbench; preserve valid Python and the existing DUT interface unless the log proves it is wrong.'
        else:
            system += '\nThe artifact is an EDA flow/configuration file; preserve tool-specific syntax and existing valid settings.'

        prompt = (
            f'Stage target: {target}\n'
            f'Artifact path: {path}\n'
            f'Diagnosis: {diagnosis}\n'
            f'Failure log:\n{log}\n\n'
            f'Current complete artifact:\n{artifact}\n\n'
            'Return JSON only with exactly: patch, rationale. '
            'patch must be a standard unified diff for THIS ONE artifact only. '
            'Use the repository-relative path in both --- and +++ headers. '
            'Make the smallest possible change. Do not return the full artifact. '
            'Do not change any other file. If no safe patch exists, return patch as an empty string.'
        )
        raw = await self.llm.chat(system, prompt)
        result = parse_json(raw)
        if not isinstance(result, dict):
            raise ValueError('Repair response is not a JSON object')
        patch = result.get('patch', '')
        if not isinstance(patch, str):
            raise ValueError('Repair patch must be a string')
        return {'patch': patch, 'rationale': result.get('rationale', '')}
