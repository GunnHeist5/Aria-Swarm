"""Drive the analyst loop with a scripted LLM stub (dealflow pattern) — no
network, no API key."""

from types import SimpleNamespace

from app.agent import analyst
from app.db import control, tenant as tdb


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(name, tool_input, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _response(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        return self.responses.pop(0)


def _tenant():
    tenant_id = control.create_tenant("Loop Co")
    return tenant_id


def test_tool_loop_round_trip():
    tenant_id = _tenant()
    llm = ScriptedLLM([
        _response([_tool_block("list_datasets", {})], stop_reason="tool_use"),
        _response([_text_block("No data uploaded yet — please upload a CSV.")]),
    ])
    result = analyst.run_analysis(tenant_id, "What drove Q2?", llm=llm)
    assert result.status == "done"
    assert "upload" in result.answer
    # second call saw the tool result fed back
    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "No datasets" in tool_result["content"]


def test_unknown_tool_returns_error_not_crash():
    tenant_id = _tenant()
    llm = ScriptedLLM([
        _response([_tool_block("read_other_tenant", {"tenant_id": "t_deadbeef0000"})], stop_reason="tool_use"),
        _response([_text_block("done")]),
    ])
    result = analyst.run_analysis(tenant_id, "q", llm=llm)
    assert result.status == "done"
    err = llm.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True


def test_tool_specs_carry_no_tenant_parameter():
    from app.agent.tools import TOOL_SPECS

    for spec in TOOL_SPECS:
        assert "tenant" not in str(spec["input_schema"]).lower()


def test_refusal_recorded():
    tenant_id = _tenant()
    llm = ScriptedLLM([_response([], stop_reason="refusal")])
    result = analyst.run_analysis(tenant_id, "q", llm=llm)
    assert result.status == "refused"


def test_analysis_and_usage_recorded():
    tenant_id = _tenant()
    llm = ScriptedLLM([_response([_text_block("Answer.")])])
    result = analyst.run_analysis(tenant_id, "quantify it", llm=llm)
    analyses = tdb.list_analyses(tenant_id)
    assert analyses[0]["analysis_id"] == result.analysis_id
    assert analyses[0]["status"] == "done"
    assert control.analyses_this_month(tenant_id) == 1


def test_engagement_context_only_uses_approved_methods():
    from app.db import knowledge

    tenant_id = _tenant()
    knowledge.create_draft("playbook", "Secret draft method", "confidential draft", None, "trainer_chat")
    llm = ScriptedLLM([_response([_text_block("ok")])])
    analyst.run_analysis(tenant_id, "secret draft method", llm=llm)
    assert "confidential draft" not in llm.calls[0]["system"]
