"""Training Gym offline suite: scripted stubs for analyst LLM, grader, and
splitter — no network, no API key."""

import json
from types import SimpleNamespace

import pytest

from app import config
from app.db import control, gym, knowledge, tenant as tdb
from app.gym import grader as grader_mod, runner, split


# --- stubs (house style: helpers live in the test file) ---------------------

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
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.responses.pop(0)


class ScriptedGrader:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system, user_text):
        self.calls.append({"system": system, "user_text": user_text})
        return self.replies.pop(0)


PASS_JSON = '{"verdict": "pass", "score": 0.95, "gaps": []}'
FAIL_JSON = '{"verdict": "fail", "score": 0.2, "gaps": ["never normalized by market growth"]}'


def _set_with_problem(kind="conceptual", confirm=True):
    set_id = gym.create_problem_set("Drills", "brand_analytics", "test")
    problem_id = gym.add_problem(
        set_id, "Is flat TRx in a growing market stable?",
        "No — flat volume in a growing market is share erosion.",
        expected_steps_md="Normalize by market growth.", kind=kind,
    )
    if confirm and kind == "conceptual":
        gym.confirm_problem(problem_id)
    return set_id, problem_id


# --- ingest -----------------------------------------------------------------

def test_split_creates_draft_problems():
    splitter = lambda system, text: json.dumps([
        {"prompt": "Q1?", "expected_steps": "s", "answer_key": "A1", "kind": "conceptual", "domain": "general"},
        {"prompt": "Q2?", "expected_steps": "", "answer_key": "A2", "kind": "data", "domain": "brand_analytics"},
    ])
    set_id = split.ingest_pasted_text("some doc", "Set", llm=splitter)
    problems = gym.list_problems(set_id)
    assert len(problems) == 2
    assert all(p["status"] == "draft" for p in problems)
    with pytest.raises(ValueError):
        runner.run_set(set_id)  # nothing confirmed -> refuse


def test_tabular_ingest_needs_no_llm():
    csv = b"Problem,Answer,Kind\nWhat is X?,X is Y,conceptual\nCrunch it,42,data\n"
    set_id = split.ingest_problem_document("drills.csv", csv, "Tab set")
    problems = gym.list_problems(set_id)
    assert [p["kind"] for p in problems] == ["conceptual", "data"]


def test_tabular_missing_columns_named():
    with pytest.raises(ValueError, match="answer"):
        split.split_tabular("x.csv", b"problem\nonly one column\n")


def test_unparseable_splitter_raises_for_manual_fallback():
    with pytest.raises(ValueError, match="manually"):
        split.split_text("doc", llm=lambda s, t: "I could not find any problems, sorry!")


# --- run + grade ------------------------------------------------------------

def test_run_and_grade_round_trip():
    set_id, problem_id = _set_with_problem()
    llm = ScriptedLLM([_response([_tool_block("list_datasets", {})], "tool_use"),
                       _response([_text_block("Share erosion of ~6%.")])])
    grader = ScriptedGrader([PASS_JSON])
    result = runner.run_set(set_id, llm=llm, grader=grader)
    assert result["status"] == "done" and result["attempted"] == 1

    attempt = gym.get_attempt(
        gym.miss_queue(set_id)[0]["attempt_id"] if gym.miss_queue(set_id)
        else _only_attempt_id(result["run_id"])
    )
    assert attempt["status"] == "done"
    transcript = json.loads(attempt["transcript_json"])
    assert transcript[0]["type"] == "tool_use"
    assert attempt["tokens_out"] > 0
    tenant_id = gym.get_meta("practice_tenant_id")
    assert control.analyses_this_month(tenant_id) == 1


def _only_attempt_id(run_id):
    with gym.connect() as conn:
        return conn.execute("SELECT attempt_id FROM attempts WHERE run_id = ?", (run_id,)).fetchone()[0]


def test_answer_key_never_reaches_analyst():
    set_id, _ = _set_with_problem()
    llm = ScriptedLLM([_response([_text_block("done")])])
    grader = ScriptedGrader([PASS_JSON])
    runner.run_set(set_id, llm=llm, grader=grader)

    key_text = "flat volume in a growing market is share erosion"
    assert key_text in grader.calls[0]["user_text"]
    for call in llm.calls:
        assert key_text not in call["system"]
        assert key_text not in str(call["messages"])
        assert "Normalize by market growth" not in call["system"]
        assert "Normalize by market growth" not in str(call["messages"])


def test_graded_fail_lands_in_miss_queue_and_override_clears():
    set_id, _ = _set_with_problem()
    runner.run_set(set_id, llm=ScriptedLLM([_response([_text_block("stable")])]),
                   grader=ScriptedGrader([FAIL_JSON]))
    misses = gym.miss_queue(set_id)
    assert len(misses) == 1 and misses[0]["verdict"] == "fail"
    gym.override_grade(misses[0]["grade_id"], "grader_wrong", "actually fine")
    assert gym.miss_queue(set_id) == []


def test_grader_unparseable_fails_closed():
    set_id, _ = _set_with_problem()
    runner.run_set(set_id, llm=ScriptedLLM([_response([_text_block("x")])]),
                   grader=ScriptedGrader(["not json at all"]))
    misses = gym.miss_queue(set_id)
    assert misses[0]["verdict"] == "error" and misses[0]["score"] == 0.0


def test_failed_attempt_graded_without_llm_call():
    set_id, _ = _set_with_problem()
    grader = ScriptedGrader([])  # would raise if called
    runner.run_set(set_id, llm=ScriptedLLM([_response([], "refusal")]), grader=grader)
    misses = gym.miss_queue(set_id)
    assert misses[0]["verdict"] == "fail"
    assert "did not complete" in misses[0]["gaps_json"]
    assert grader.calls == []


def test_confirmed_problem_immutable_once_attempted():
    set_id, problem_id = _set_with_problem()
    gym.update_problem(problem_id, prompt_md="edited before attempts")  # allowed
    runner.run_set(set_id, llm=ScriptedLLM([_response([_text_block("a")])]),
                   grader=ScriptedGrader([PASS_JSON]))
    with pytest.raises(ValueError, match="immutable"):
        gym.update_problem(problem_id, prompt_md="tampering after the fact")


def test_resume_skips_attempted():
    set_id, _ = _set_with_problem()
    p2 = gym.add_problem(set_id, "Second question?", "Second answer.")
    gym.confirm_problem(p2)

    first = runner.run_set(set_id, limit=1,
                           llm=ScriptedLLM([_response([_text_block("a1")])]),
                           grader=ScriptedGrader([PASS_JSON]))
    assert first["status"] == "running" and first["remaining"] == 1

    second = runner.run_set(set_id, run_id=first["run_id"],
                            llm=ScriptedLLM([_response([_text_block("a2")])]),
                            grader=ScriptedGrader([PASS_JSON]))
    assert second["status"] == "done" and second["attempted"] == 1
    with gym.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM attempts WHERE run_id = ?", (first["run_id"],)).fetchone()[0]
    assert n == 2


def test_data_problem_places_dataset_idempotently():
    set_id = gym.create_problem_set("Data drills", "brand_analytics", "test")
    problem_id = gym.add_problem(set_id, "Crunch the numbers.", "42.", kind="data")
    with pytest.raises(ValueError, match="dataset"):
        gym.confirm_problem(problem_id)  # data problems need a dataset first

    src_dir = config.gym_files_root() / set_id
    src_dir.mkdir(parents=True)
    (src_dir / "mini.csv").write_bytes(b"a,b\n1,2\n")
    gym.attach_dataset(problem_id, "mini.csv", "hash")
    gym.confirm_problem(problem_id)

    for _ in range(2):  # two separate runs -> still one dataset row
        runner.run_set(set_id, llm=ScriptedLLM([_response([_text_block("42")])]),
                       grader=ScriptedGrader([PASS_JSON]))
    tenant_id = gym.get_meta("practice_tenant_id")
    matches = [d for d in tdb.list_datasets(tenant_id) if d["filename"] == f"{problem_id}_mini.csv"]
    assert len(matches) == 1


def test_gym_correction_unretrievable_until_approved():
    set_id, _ = _set_with_problem()
    runner.run_set(set_id, llm=ScriptedLLM([_response([_text_block("stable")])]),
                   grader=ScriptedGrader([FAIL_JSON]))
    grade_id = gym.miss_queue(set_id)[0]["grade_id"]

    method_id = knowledge.create_draft(
        "playbook", "Always normalize by market growth",
        "Flat TRx in a growing market is share erosion; normalize first.",
        "brand_analytics", "gym_correction", None,
    )
    gym.set_grade_correction(grade_id, method_id)
    assert gym.miss_queue(set_id) == []  # resolved

    # Draft is invisible everywhere...
    assert knowledge.search_methods("normalize market growth") == []
    real_tenant = control.create_tenant("Real Client Co")
    from app.agent import analyst

    llm = ScriptedLLM([_response([_text_block("ok")])])
    analyst.run_analysis(real_tenant, "normalize market growth", llm=llm)
    assert "share erosion; normalize first" not in llm.calls[0]["system"]

    # ...until it walks the gate.
    knowledge.mark_anonymized(method_id, True)
    knowledge.approve(method_id, "trainer-1")
    assert [m["method_id"] for m in knowledge.search_methods("normalize market growth")] == [method_id]


def test_gym_learnings_stay_in_practice_tenant():
    set_id, _ = _set_with_problem()
    leak = "GYM-ONLY-INSIGHT: always sandbag the forecast"
    llm = ScriptedLLM([
        _response([_tool_block("note_learning", {"text": leak})], "tool_use"),
        _response([_text_block("noted and answered")]),
    ])
    runner.run_set(set_id, llm=llm, grader=ScriptedGrader([PASS_JSON]))

    tenant_id = gym.get_meta("practice_tenant_id")
    assert any(leak in l["text"] for l in tdb.recent_learnings(tenant_id))

    real_tenant = control.create_tenant("Other Client Co")
    from app.agent import analyst

    real_llm = ScriptedLLM([_response([_text_block("ok")])])
    analyst.run_analysis(real_tenant, "forecast question", llm=real_llm)
    assert leak not in real_llm.calls[0]["system"]
