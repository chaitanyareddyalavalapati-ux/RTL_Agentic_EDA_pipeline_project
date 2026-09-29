from backend.agent.specification import parse_request


def test_dff_request_is_classified_as_sequential():
    plan = parse_request('Create a d flip flop and run the entire EDA flow using real EDA tools')
    assert plan['design_type'] == 'dff'
    assert plan['top_module'] == 'dff'
    assert plan['ports'] == ['clk', 'rst_n', 'd', 'q']


def test_dff_variants_are_not_misclassified_as_arithmetic():
    for prompt in [
        'Build a d flipflop with async reset and Q output',
        'Implement a D-type register with clock and reset',
        'Create a flip flop for data storage using real EDA tools',
    ]:
        plan = parse_request(prompt)
        assert plan['design_type'] == 'dff', prompt
        assert 'clk' in plan['ports']
        assert 'd' in plan['ports']


def test_combinational_request_stays_combinational():
    plan = parse_request('Create a 4 bit adder and run the complete EDA flow.')
    assert plan['design_type'] == 'adder'
    assert plan['top_module'] == 'adder4'
