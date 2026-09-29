"""Natural-language hardware requirements and RTL conformance helpers."""

import math
import re


def _number(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_request(request, default_ram_depth=None):
    text = request.strip()
    lower = text.lower()
    is_rom = bool(re.search(r"\brom\b|read[- ]only memory", lower))
    is_ram = bool(re.search(r"\b(ram|memory)\b", lower))
    is_fifo = "fifo" in lower
    is_alu = "alu" in lower or "arithmetic logic" in lower
    is_counter = "counter" in lower
    is_register_file = "register file" in lower
    is_dff = bool(
        re.search(
            r"\b(?:d|data)\s*(?:-?type)?\s*(?:flip\s*[- ]?\s*flop|flipflop|ff|register)\b",
            lower,
        )
        or re.search(r"\bflip\s*[- ]?\s*flop\b", lower)
        or "dff" in lower
    )
    is_jk = "jk flip flop" in lower or "jk flip-flop" in lower or "jkff" in lower
    is_adder = "adder" in lower or "sum" in lower and "add" in lower
    is_subtractor = "subtract" in lower or "subtractor" in lower or "difference" in lower
    is_comparator = "comparator" in lower or "compare" in lower or "greater" in lower or "less" in lower or "equal" in lower
    is_mux = "mux" in lower or "multiplexer" in lower
    address_width = _number((
        r"\b(?:address|addr)\s*(?:width|size|bits?)\s*(?:is|=|:)?\s*(\d+)",
        r"\b(\d+)\s*[- ]?bit\s*(?:address|addr)\b",
        r"\b(\d+)\s*(?:address|addr)\s*bits?\b",
    ), lower)
    design_type = (
        "rom" if is_rom
        else "fifo" if is_fifo
        else "alu" if is_alu
        else "counter" if is_counter
        else "jk" if is_jk
        else "dff" if is_dff
        else "ram" if is_ram or is_register_file
        else "adder" if is_adder
        else "subtractor" if is_subtractor
        else "comparator" if is_comparator
        else "mux" if is_mux
        else "combinational"
    )
    width = _number((r"\b(\d+)\s*[- ]?bit\b", r"\bwidth\s*(?:is|=|:)\s*(\d+)"), lower)
    depth = _number((r"\b(\d+)\s*(?:locations?|entries?|words?|deep)\b", r"\bdepth\s*(?:(?:is|=|:)\s*)?(\d+)"), lower)
    if design_type == "rom":
        if width is None:
            width = 4
        if address_width is None:
            address_width = max(1, (depth - 1).bit_length()) if depth else 4
        if depth is None:
            depth = 1 << address_width
    if depth is None:
        if (is_ram or is_register_file or is_fifo) and default_ram_depth is not None:
            depth = int(default_ram_depth)
        elif (is_ram or is_register_file or is_fifo):
            depth = 16
    if address_width is None:
        address_width = math.ceil(math.log2(depth)) if depth and depth > 1 else (1 if depth == 1 else None)
    reset = bool(re.search(r"\b(reset|rst|clear)\b", lower))
    synchronous_reset = bool(re.search(r"\bsynchronous\s+(?:reset|rst)\b", lower))
    clocked = design_type != "rom" and bool(re.search(r"\b(clock|clk|clocked|synchronous|posedge|fifo|counter|ram|memory)\b", lower))
    operation_context = is_alu or bool(re.search(r"\b(?:operations?|ops?|supports?)\b", lower))
    operations = [op for op in ("ADD", "SUB", "AND", "OR", "XOR", "NOT") if operation_context and re.search(rf"\b{op}\b", lower, re.IGNORECASE)]
    if design_type == "alu" and not operations:
        operations = None
    name_match = re.search(r"\bmodule\s+(\w+)", lower)
    if name_match:
        module_name = name_match.group(1)
    elif design_type == "rom" and width and depth:
        module_name = f"rom{address_width}x{width}"
    elif design_type == "ram" and width and depth:
        module_name = f"ram{width}x{depth}"
    elif design_type == "fifo" and width and depth:
        module_name = f"fifo{width}x{depth}"
    elif width and design_type in {"adder", "subtractor", "comparator", "mux", "combinational"}:
        suffix = width if width else 1
        if design_type == "adder":
            module_name = f"adder{suffix}"
        elif design_type == "subtractor":
            module_name = f"subtractor{suffix}"
        elif design_type == "comparator":
            module_name = f"comparator{suffix}"
        elif design_type == "mux":
            module_name = f"mux{suffix}"
        else:
            module_name = f"combinational{suffix}"
    else:
        module_name = design_type
    missing = []
    if design_type == "rom" and width is None:
        missing.append("data width")
    if design_type in {"ram", "register_file", "fifo"} and width is None:
        missing.append("data width")
    if design_type in {"ram", "register_file", "fifo"} and depth is None:
        missing.append("memory depth")
    if design_type == "alu" and width is None:
        missing.append("data width")
    if design_type == "alu" and operations is None:
        missing.append("ALU operations")
    ports = {
        "rom": ["addr", "read_data"],
        "ram": ["clk", "we", "addr", "write_data", "read_data"],
        "register_file": ["clk", "we", "addr", "write_data", "read_data"],
        "fifo": ["clk", "rst", "wr_en", "rd_en", "write_data", "read_data", "full", "empty"],
        "counter": ["clk", "rst", "count"],
        "dff": ["clk", "rst_n", "d", "q"],
        "jk": ["clk", "rst_n", "j", "k", "q"],
        "alu": ["a", "b", "op", "result"],
        "adder": ["a", "b", "cin", "sum", "carry_out"],
        "subtractor": ["a", "b", "bin", "diff", "borrow_out"],
        "comparator": ["a", "b", "eq", "gt", "lt"],
        "mux": ["a", "b", "sel", "out"],
        "combinational": ["a", "b", "out"],
    }.get(design_type, [])
    return {
        "design_type": design_type,
        "module_name": module_name,
        "top_module": module_name,
        "language": "systemverilog",
        "description": text,
        "data_width": width,
        "width": width,
        "depth": depth,
        "address_width": address_width,
        "rom_contents": (
            [((index * 7 + 3) & ((1 << width) - 1)) for index in range(depth)]
            if design_type == "rom" and width and depth else None
        ),
        "assumptions": (
            [
                "No ROM contents were specified; use the deterministic generated lookup pattern in rom_contents.",
                "An unspecified 4-bit ROM uses 4 address bits and 4 data bits (16 entries).",
            ] if design_type == "rom" else []
        ),
        "clocked": clocked,
        "write_enable": design_type in {"ram", "register_file"},
        "read_type": (
            "asynchronous" if design_type == "rom"
            else "synchronous" if design_type in {"ram", "register_file"}
            else None
        ),
        "reset": reset,
        "reset_type": "synchronous" if synchronous_reset else ("asynchronous" if reset else None),
        "operations": operations,
        "ports": ports,
        "missing_requirements": missing,
        "status": "needs_clarification" if missing else "ready",
        "steps": ["generate_rtl", "validate_rtl", "generate_testbench", "simulate", "synthesize", "sta", "physical_design", "physical_verification", "report"],
    }


def validate_rtl(rtl, spec):
    """Check the generated top-level interface and RAM structure against the spec."""
    errors = []
    top = spec.get("top_module") or spec.get("module_name")
    if not re.search(rf"\bmodule\s+{re.escape(str(top))}\b", rtl or ""):
        errors.append(f"top module {top!r} is missing")
    for port in spec.get("ports", []):
        if not re.search(rf"\b{re.escape(port)}\b", rtl or ""):
            errors.append(f"required port {port!r} is missing")
    width = spec.get("data_width")
    depth = spec.get("depth")
    if spec.get("design_type") in {"ram", "register_file"}:
        if width and not re.search(rf"\[{width - 1}\s*:\s*0\]", rtl):
            errors.append(f"data width {width} is not represented")
        if depth and not re.search(rf"\[0\s*:\s*{depth - 1}\]", rtl):
            errors.append(f"memory depth {depth} is not represented")
    if spec.get("design_type") == "rom":
        address_width = spec.get("address_width")
        if width and not re.search(
            rf"\boutput\s+(?:(?:reg|wire|logic)\s+)?\[\s*{width - 1}\s*:\s*0\s*\]\s*read_data\b",
            rtl,
        ):
            errors.append(f"ROM output width {width} is not represented")
        if address_width and not re.search(
            rf"\binput\s+(?:(?:reg|wire|logic)\s+)?\[\s*{address_width - 1}\s*:\s*0\s*\]\s*addr\b",
            rtl,
        ):
            errors.append(f"ROM address width {address_width} is not represented")
    return errors