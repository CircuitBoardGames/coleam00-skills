"""The fork's two changes to ablate-ai-layer's runner: --no-hooks, and results kept out of
`git status` through the repo-local exclude file instead of an edit to `.gitignore`."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/ablate-ai-layer/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_ablation as ra  # noqa: E402


@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)


def test_no_hooks_hands_claude_a_settings_flag_that_disables_every_hook(claude_on_path):
    cmd, shell = ra.build_command(None, None, no_hooks=True)
    assert not shell
    i = cmd.index("--settings")
    assert json.loads(cmd[i + 1]) == {"disableAllHooks": True}


def test_hooks_stay_on_by_default__control(claude_on_path):
    """PASSES ON BASE: upstream's default, which the fork keeps."""
    cmd, _ = ra.build_command(None, None)
    assert "--settings" not in cmd


def test_a_custom_runner_is_passed_through_untouched():
    """--no-hooks is a Claude Code flag, so a --runner command is left alone."""
    assert ra.build_command(None, "my-agent --stdin", no_hooks=True) == ("my-agent --stdin", True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("node_modules/\n")
    return root


def test_results_inside_the_repo_are_excluded_without_touching_gitignore(tmp_path):
    root = _repo(tmp_path)
    out = root / ".ablation" / "20260924-000000"
    out.mkdir(parents=True)
    (out / "results.json").write_text("{}")
    assert ra.exclude_results(root, out) == "/.ablation/"
    assert (root / ".gitignore").read_text() == "node_modules/\n"
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                            capture_output=True, text=True, check=True).stdout
    assert ".ablation" not in status and ".gitignore" in status  # .gitignore is untracked, not edited


def test_excluding_twice_writes_the_pattern_once(tmp_path):
    root = _repo(tmp_path)
    out = root / ".ablation" / "a"
    out.mkdir(parents=True)
    ra.exclude_results(root, out)
    assert ra.exclude_results(root, out) is None
    exclude = (root / ".git/info/exclude").read_text().splitlines()
    assert exclude.count("/.ablation/") == 1


def test_results_outside_the_repo_write_nothing(tmp_path):
    root = _repo(tmp_path)
    before = (root / ".git/info/exclude").read_text()
    assert ra.exclude_results(root, tmp_path / "elsewhere") is None
    assert (root / ".git/info/exclude").read_text() == before


def test_a_linked_worktree_excludes_through_the_common_git_dir(tmp_path):
    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                    "PATH": "/usr/bin:/bin"})
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    out = wt / ".ablation" / "a"
    out.mkdir(parents=True)
    assert ra.exclude_results(wt, out) == "/.ablation/"
    assert "/.ablation/" in (root / ".git/info/exclude").read_text().splitlines()
