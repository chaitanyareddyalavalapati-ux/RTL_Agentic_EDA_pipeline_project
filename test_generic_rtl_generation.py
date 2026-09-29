import asyncio

from backend.agent.coder import Coder
from backend.agent.planner import Planner
from backend.agent.specification import validate_rtl


async def _build_rtl(prompt: str):
    planner = Planner()
    spec = await planner.plan(prompt)
    coder = Coder()
    return spec, await coder.rtl(spec, prompt)


def test_adder_prompt_generates_valid_rtl():
    async def run_test():
        spec, rtl = await _build_rtl("Create a 4 bit adder and run the complete EDA flow.")
        assert spec["design_type"] == "adder"
        assert validate_rtl(rtl, spec) == []
        assert "assign sum[i] = a[i] ^ b[i] ^ carry[i];" in rtl
        assert "assign carry_out = carry[4];" in rtl

    asyncio.run(run_test())


def test_generic_combinational_prompt_generates_rtl():
    async def run_test():
        spec, rtl = await _build_rtl("Design an 8-bit comparator and verify it with EDA flow.")
        assert spec["design_type"] == "comparator"
        assert "module comparator8" in rtl or "module comparator" in rtl
        assert "a == b" in rtl or "eq" in rtl

    asyncio.run(run_test())
