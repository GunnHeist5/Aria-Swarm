"""Live activity view: background tasks, event feed, polling endpoints —
offline with scripted stubs; tasks run inline via monkeypatched spawn."""

import json
from types import SimpleNamespace

import pytest

from app import tasks
from app.db import control, gym, tenant as tdb


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


PASS_JSON = '{"verdict": "pass", "score": 0.9, "gaps": []}'


@pytest.fixture
def inline_tasks(monkeypatch):
    """Route-spawned background work runs synchronously in tests."""
    monkeypatch.setattr(tasks, "spawn", lambda fn, *args: fn(*args))
    yield
    tasks.llm_factory = None
    tasks.grader_factory = None


def _auth(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


# --- phrase mapping ---------------------------------------------------------

def test_friendly_phrase_mapping():
    assert tasks.friendly_phrase({"type": "tool_use", "name": "run_python", "input": {}}) == \
        ("step", "Running analysis code")
    assert tasks.friendly_phrase({"type": "tool_use", "name": "list_datasets", "input": {}}) == \
        ("step", "Looking at available datasets")
    assert tasks.friendly_phrase(
        {"type": "tool_use", "name": "save_deliverable", "input": {"title": "TRx trend"}}
    ) == ("step", "Saving deliverable: TRx trend")
    assert tasks.friendly_phrase({"type": "tool_result", "name": "run_python", "is_error": True}) == \
        ("error", "Hit an error, adjusting approach")
    assert tasks.friendly_phrase({"type": "tool_result", "name": "run_python", "is_error": False}) is None


# --- task writes the feed ---------------------------------------------------

def test_run_analysis_task_writes_feed(monkeypatch):
    tenant_id = control.create_tenant("Feed Co")
    monkeypatch.setattr(tasks, "llm_factory", lambda: ScriptedLLM([
        _response([_tool_block("list_datasets", {})], "tool_use"),
        _response([_text_block("The answer.")]),
    ]))
    analysis_id = tdb.create_analysis(tenant_id, "q")
    tasks.run_analysis_task(tenant_id, "q", None, analysis_id, "k_test")

    events = tdb.list_analysis_events(tenant_id, analysis_id)
    texts = [e["text"] for e in events]
    assert texts[0] == "Analysis started"
    assert "Looking at available datasets" in texts
    assert texts[-1] == "Analysis complete"
    assert tdb.get_analysis(tenant_id, analysis_id)["status"] == "done"


def test_run_analysis_task_marks_failed_on_crash(monkeypatch):
    tenant_id = control.create_tenant("Crash Co")

    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(tasks, "llm_factory", lambda: Boom())
    analysis_id = tdb.create_analysis(tenant_id, "q")
    tasks.run_analysis_task(tenant_id, "q", None, analysis_id, "k_test")
    row = tdb.get_analysis(tenant_id, analysis_id)
    assert row["status"] == "failed"
    assert tdb.list_analysis_events(tenant_id, analysis_id)[-1]["text"] == "Analysis failed"


# --- polling endpoint -------------------------------------------------------

def test_events_endpoint_cursor_and_answer(client, demo_tenant, monkeypatch, inline_tasks):
    tenant_id, raw_key = demo_tenant
    monkeypatch.setattr(tasks, "llm_factory", lambda: ScriptedLLM([
        _response([_tool_block("list_datasets", {})], "tool_use"),
        _response([_text_block("Final answer text.")]),
    ]))
    r = client.post("/chat", data={"question": "what happened?"},
                    headers=_auth(raw_key), follow_redirects=False)
    assert r.status_code == 303 and "watch=" in r.headers["location"]
    analysis_id = r.headers["location"].split("watch=")[1]

    r = client.get(f"/chat/analyses/{analysis_id}/events?after=0", headers=_auth(raw_key))
    data = r.json()
    assert data["status"] == "done"
    assert data["answer"] == "Final answer text."
    assert [e["text"] for e in data["events"]][0] == "Analysis started"

    last_seq = data["events"][-1]["seq"]
    r = client.get(f"/chat/analyses/{analysis_id}/events?after={last_seq}", headers=_auth(raw_key))
    assert r.json()["events"] == []


def test_tenant_cannot_poll_other_tenants_analysis(client, demo_tenant):
    tenant_a, key_a = demo_tenant
    tenant_b = control.create_tenant("B Watch Co")
    analysis_b = tdb.create_analysis(tenant_b, "secret question")
    r = client.get(f"/chat/analyses/{analysis_b}/events", headers=_auth(key_a))
    assert r.status_code == 404
    assert "secret" not in r.text


# --- double submit ----------------------------------------------------------

def test_double_submit_refused_while_running(client, demo_tenant, inline_tasks, monkeypatch):
    tenant_id, raw_key = demo_tenant
    conversation_id = tdb.create_conversation(tenant_id, "c")
    tdb.create_analysis(tenant_id, "first question", conversation_id)  # fresh 'running' row
    r = client.post("/chat", data={"question": "second", "conversation": conversation_id},
                    headers=_auth(raw_key), follow_redirects=False)
    assert r.status_code == 409


def test_stale_running_does_not_block(client, demo_tenant, inline_tasks, monkeypatch):
    tenant_id, raw_key = demo_tenant
    conversation_id = tdb.create_conversation(tenant_id, "c")
    old = tdb.create_analysis(tenant_id, "old question", conversation_id)
    with tdb.tenant_db(tenant_id) as conn:
        conn.execute("UPDATE analyses SET created_at = '2020-01-01T00:00:00+00:00'"
                     " WHERE analysis_id = ?", (old,))
    # the stale row reports 'stale' on its feed...
    r = client.get(f"/chat/analyses/{old}/events", headers=_auth(raw_key))
    assert r.json()["status"] == "stale"
    # ...and a new submission is accepted
    monkeypatch.setattr(tasks, "llm_factory", lambda: ScriptedLLM([_response([_text_block("ok")])]))
    r = client.post("/chat", data={"question": "new", "conversation": conversation_id},
                    headers=_auth(raw_key), follow_redirects=False)
    assert r.status_code == 303


# --- gym --------------------------------------------------------------------

def _confirmed_set():
    set_id = gym.create_problem_set("Live drills", "brand_analytics", "test")
    for i in range(2):
        problem_id = gym.add_problem(set_id, f"Question {i}?", f"Answer {i}.")
        gym.confirm_problem(problem_id)
    return set_id


def test_gym_run_status_endpoint(client, inline_tasks, monkeypatch):
    _, trainer_key = control.issue_key("trainer", None, "t")
    set_id = _confirmed_set()
    monkeypatch.setattr(tasks, "llm_factory",
                        lambda: ScriptedLLM([_response([_text_block("a1")]),
                                             _response([_text_block("a2")])]))
    monkeypatch.setattr(tasks, "grader_factory",
                        lambda: (lambda system, text: PASS_JSON))

    r = client.post(f"/trainer/gym/sets/{set_id}/run", data={},
                    headers=_auth(trainer_key), follow_redirects=False)
    assert r.status_code == 303 and "watch_run=" in r.headers["location"]
    run_id = r.headers["location"].split("watch_run=")[1]

    r = client.get(f"/trainer/gym/runs/{run_id}/status", headers=_auth(trainer_key))
    data = r.json()
    assert data["run_status"] == "done"
    assert data["attempted"] == 2 and data["total"] == 2
    assert all(p["verdict"] == "pass" for p in data["problems"])


def test_gym_status_locked_to_trainer_role(client, demo_tenant):
    _, client_key = demo_tenant
    set_id = _confirmed_set()
    run_id = gym.create_run(set_id)
    assert client.get(f"/trainer/gym/runs/{run_id}/status",
                      headers=_auth(client_key)).status_code == 403


def test_gym_status_unknown_run_404(client):
    _, trainer_key = control.issue_key("trainer", None, "t")
    assert client.get("/trainer/gym/runs/r_000000000000/status",
                      headers=_auth(trainer_key)).status_code == 404
