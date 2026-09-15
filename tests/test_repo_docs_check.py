"""Documentation validation rejects broken public archives without network use."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _checker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_repo_docs.py"
    spec = importlib.util.spec_from_file_location("repo_docs_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(tmp_path):
    checker = _checker()
    for directory in checker.MAIN_DIRS:
        target = tmp_path / directory
        target.mkdir(parents=True, exist_ok=True)
        for name in ("AGENTS.md", "CLAUDE.md"):
            (target / name).write_text("# Guide\n\nSynthetic routing instructions.\n")
    return checker


def test_valid_archive_resolves_links_and_ignores_external_and_code_examples(tmp_path):
    checker = _archive(tmp_path)
    (tmp_path / "docs" / "setup.md").write_text("# Setup\n\n## Run server\n")
    (tmp_path / "README.md").write_text(
        "[Setup](docs/setup.md#run-server)\n[External](https://example.invalid/not-local)\n"
        "[Guide][setup]\n\n[setup]: docs/setup.md\n"
        "```markdown\n[Illustrative link](missing-example.md)\n```\n"
    )
    assert checker.check_repo_docs(tmp_path) == []


@pytest.mark.parametrize("target,reason", [
    ("docs/missing.md", "missing local link"),
    ("AGENTS.md#missing-heading", "missing heading"),
    ("../outside.md", "escapes repository"),
])
def test_invalid_local_links_fail_validation(tmp_path, target, reason):
    checker = _archive(tmp_path)
    (tmp_path / "README.md").write_text(f"[Broken]({target})\n")
    errors = checker.check_repo_docs(tmp_path)
    assert any(reason in error and "README.md" in error for error in errors)


@pytest.mark.parametrize("defect,reason", [
    ("missing", "missing regular guide file"),
    ("different", "guide pair differs"),
    ("long", "exceeds 60 lines"),
])
def test_invalid_guide_pairs_fail_validation(tmp_path, defect, reason):
    checker = _archive(tmp_path)
    guide = tmp_path / "tests" / "CLAUDE.md"
    if defect == "missing":
        guide.unlink()
    elif defect == "different":
        guide.write_text("# A different guide\n")
    else:
        guide.write_text("line\n" * 61)
    assert any(reason in error for error in checker.check_repo_docs(tmp_path))


def test_source_package_needs_its_own_guides(tmp_path):
    checker = _archive(tmp_path)
    package = tmp_path / "src" / "epicor_mcp" / "example"
    package.mkdir()
    (package / "__init__.py").write_text("")
    errors = checker.check_repo_docs(tmp_path)
    assert any("example/AGENTS.md" in error for error in errors)
    assert any("example/CLAUDE.md" in error for error in errors)


def test_retired_citation_in_python_prose_fails_validation(tmp_path):
    checker = _archive(tmp_path)
    (tmp_path / "scripts" / "example.py").write_text("# " + "pro" + "be " + "12\n")
    errors = checker.check_repo_docs(tmp_path)
    assert any("retired private-reference" in error for error in errors)


@pytest.mark.parametrize("source", [
    "# See `tests/harness/absent_fixture.json` for the column list.\n",
    '"""See `tests/harness/absent_fixture.json` for the column list."""\n',
])
def test_broken_repository_reference_in_python_prose_fails_validation(tmp_path, source):
    checker = _archive(tmp_path)
    (tmp_path / "scripts" / "example.py").write_text(source)
    errors = checker.check_repo_docs(tmp_path)
    assert any("missing local reference" in error and "absent_fixture.json" in error for error in errors)


def test_valid_python_reference_and_ordinary_example_string_are_distinguished(tmp_path):
    checker = _archive(tmp_path)
    (tmp_path / "tests" / "fixture.json").write_text("{}")
    (tmp_path / "scripts" / "example.py").write_text(
        "# See `tests/fixture.json`.\n"
        'example = "# See `tests/harness/absent_fixture.json`."\n'
    )
    assert checker.check_repo_docs(tmp_path) == []


def test_broken_backticked_markdown_reference_fails_outside_fenced_examples(tmp_path):
    checker = _archive(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("```python\n# See `tests/absent.json`.\n```\n")
    assert checker.check_repo_docs(tmp_path) == []
    readme.write_text("See `tests/absent.json`.\n")
    assert any("missing local reference" in error for error in checker.check_repo_docs(tmp_path))
