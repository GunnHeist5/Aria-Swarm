import time

from app import config
from app.agent.sandbox import run_python


def test_basic_execution(tmp_path):
    result = run_python("print('hello', 40 + 2)", tmp_path)
    assert result.ok and "hello 42" in result.stdout


def test_stderr_and_failure(tmp_path):
    result = run_python("raise ValueError('boom')", tmp_path)
    assert not result.ok and "boom" in result.stderr


def test_env_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PHARMA_SECRET_KEY", "cookie-secret")
    result = run_python(
        "import os; leaks=[k for k in os.environ if 'ANTHROPIC' in k or 'PHARMA' in k or 'PROXY' in k.upper()]; print(leaks)",
        tmp_path,
    )
    assert result.ok and result.stdout.strip() == "[]"


def test_wall_timeout_kills(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_WALL_TIMEOUT_S", 2)
    start = time.monotonic()
    result = run_python("import time\ntime.sleep(60)", tmp_path)
    assert not result.ok and "wall timeout" in result.stderr
    assert time.monotonic() - start < 30


def test_missing_workdir_refused(tmp_path):
    result = run_python("print('x')", tmp_path / "nope")
    assert not result.ok


def test_script_file_cleaned_up(tmp_path):
    run_python("print('x')", tmp_path)
    assert not list(tmp_path.glob("_analysis_*.py"))
