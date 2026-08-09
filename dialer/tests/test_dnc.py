"""DNC sources: file parsing, last-10 matching, and the fail-closed missing case."""

from __future__ import annotations

from pathlib import Path

from conftest import make_settings

from orchestrator.compliance.dnc import FileDncChecker, MissingDncChecker, get_dnc_checker

DNC_FILE = """\
# Federal DNC extract — test fixture
6145550100
+16145550101

16145550102   # trailing comment is ignored
(614) 555-0103
   6145550104
not-a-number-line
"""


def write_dnc(tmp_path: Path, content: str = DNC_FILE) -> Path:
    path = tmp_path / "dnc.txt"
    path.write_text(content)
    return path


def test_file_checker_parses_all_line_shapes(tmp_path):
    checker = FileDncChecker(write_dnc(tmp_path))
    assert checker.available()
    for listed in (
        "+16145550100",  # bare 10-digit line
        "+16145550101",  # E.164 line
        "+16145550102",  # 11-digit line + trailing comment
        "+16145550103",  # punctuated line
        "+16145550104",  # leading whitespace
    ):
        assert checker.is_listed(listed), listed
    assert not checker.is_listed("+16145550199")


def test_matching_is_on_last_ten_digits(tmp_path):
    checker = FileDncChecker(write_dnc(tmp_path))
    # However the query is formatted, the national number is what matches.
    assert checker.is_listed("6145550100")
    assert checker.is_listed("1-614-555-0100")


def test_missing_file_is_unavailable(tmp_path):
    checker = FileDncChecker(tmp_path / "does-not-exist.txt")
    assert not checker.available()
    # If consulted anyway, fail closed.
    assert checker.is_listed("+16145550199")


def test_get_dnc_checker_unconfigured_is_missing():
    checker = get_dnc_checker(make_settings(dnc_list_path=None))
    assert isinstance(checker, MissingDncChecker)
    assert not checker.available()
    assert checker.is_listed("+16145550100")  # fail closed if consulted


def test_get_dnc_checker_with_path(tmp_path):
    path = write_dnc(tmp_path)
    checker = get_dnc_checker(make_settings(dnc_list_path=str(path)))
    assert isinstance(checker, FileDncChecker)
    assert checker.available()
    assert checker.is_listed("+16145550100")


def test_comment_only_file_is_available_but_empty(tmp_path):
    checker = FileDncChecker(write_dnc(tmp_path, "# nothing here\n\n"))
    assert checker.available()  # a present-but-empty list is a valid source
    assert not checker.is_listed("+16145550100")
