"""The fork's changes to ablate-ai-layer's runner: --no-hooks, --hooks-only and --variant-patch, and results kept out of
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
    assert json.loads(cmd[i + 1]) == {"disableAllHooks": True, "crossSessionInbound": "refuse"}


def test_hooks_stay_on_by_default__control(claude_on_path):
    """Upstream's default, which the fork keeps: without --no-hooks nothing disables hooks."""
    cmd, _ = ra.build_command(None, None)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert "disableAllHooks" not in settings and "--setting-sources" not in cmd


def test_every_claude_arm_refuses_peer_messages(claude_on_path):
    """Measured on 2.1.280 (hub#1614): a peer message reached `claude -p` with disableAllHooks on,
    and once replaced the run's final result; with this key, 0 of 2 got through."""
    for kw in ({}, {"no_hooks": True}):
        cmd, _ = ra.build_command(None, None, **kw)
        assert cmd.count("--settings") == 1
        assert json.loads(cmd[cmd.index("--settings") + 1])["crossSessionInbound"] == "refuse"


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


def test_allowed_tools_reach_claude_as_one_variadic_flag(claude_on_path):
    cmd, _ = ra.build_command("m", None, allowed_tools=["Bash(python3 -m pytest:*)", "Bash(sh -n:*)"])
    i = cmd.index("--allowedTools")
    assert cmd[i + 1:i + 3] == ["Bash(python3 -m pytest:*)", "Bash(sh -n:*)"]
    assert cmd[i + 3] == "--model"  # a flag ends the variadic list


def _committed_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "CLAUDE.md").write_text("rules\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, env=env)
    return root


def test_a_stripped_file_is_absent_from_status_and_from_the_captured_diff(tmp_path):
    root = _committed_repo(tmp_path)
    (root / "CLAUDE.md").unlink()
    ra.hide_stripped(root, ["CLAUDE.md"])
    (root / "new.py").write_text("x = 1\n")
    status = ra.git(["status", "--porcelain"], root)
    assert "CLAUDE.md" not in status and "new.py" in status
    ra.git(["add", "-A"], root)
    diff = ra.git(["diff", "--cached", "--name-only"], root).split()
    assert diff == ["new.py"]


def test_without_hiding_the_deletion_leaks_into_the_diff__control(tmp_path):
    """PASSES ON BASE: the leak the fix removes, shown so the test above cannot pass vacuously."""
    root = _committed_repo(tmp_path)
    (root / "CLAUDE.md").unlink()
    ra.git(["add", "-A"], root)
    assert "CLAUDE.md" in ra.git(["diff", "--cached", "--name-only"], root).split()


def test_hooks_only_drops_project_settings_and_hands_claude_the_file(claude_on_path):
    """Measured on 2.1.280: with `--setting-sources user`, a project SessionStart and PreToolUse
    hook and a project-enabled plugin's SessionStart all stayed silent; a hook from --settings fired."""
    f = Path(__file__).parent / "_hooks_only_fixture.json"
    f.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    try:
        cmd, _ = ra.build_command(None, None, hooks_only=str(f))
    finally:
        f.unlink()
    i = cmd.index("--setting-sources")
    assert cmd[i:i + 3] == ["--setting-sources", "user", "--settings"]
    settings = json.loads(cmd[i + 3])
    assert settings["hooks"]["SessionStart"] and settings["crossSessionInbound"] == "refuse"


def _patch_for(root: Path) -> Path:
    (root / "CLAUDE.md").write_text("short rules\n")
    patch = root.parent / "short.patch"
    patch.write_text(ra.git(["diff"], root))
    ra.git(["checkout", "--", "CLAUDE.md"], root)
    return patch


def test_the_variant_patch_is_applied_and_absent_from_the_captured_diff(tmp_path):
    root = _committed_repo(tmp_path)
    patch = _patch_for(root)
    assert ra.apply_variant(root, patch) == ["CLAUDE.md"]
    assert (root / "CLAUDE.md").read_text() == "short rules\n"
    (root / "new.py").write_text("x = 1\n")
    assert "CLAUDE.md" not in ra.git(["status", "--porcelain"], root)
    ra.git(["add", "-A"], root)
    assert ra.git(["diff", "--cached", "--name-only"], root).split() == ["new.py"]


def test_an_unhidden_patch_leaks_into_the_diff__control(tmp_path):
    """PASSES ON BASE: the leak apply_variant hides, so the test above cannot pass vacuously."""
    root = _committed_repo(tmp_path)
    patch = _patch_for(root)
    ra.git(["apply", str(patch)], root)
    ra.git(["add", "-A"], root)
    assert "CLAUDE.md" in ra.git(["diff", "--cached", "--name-only"], root).split()


def test_scratch_memory_copies_the_main_checkouts_memory_even_from_a_linked_worktree(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    root = _committed_repo(tmp_path)
    real = cfg / "projects" / ra.project_slug(root) / "memory"
    real.mkdir(parents=True)
    (real / "MEMORY.md").write_text("- [a fact](a.md)\n")
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    run_tmp = tmp_path / "run"
    run_tmp.mkdir()
    copy = ra.scratch_memory(wt, run_tmp)
    assert (copy / "MEMORY.md").read_text() == "- [a fact](a.md)\n"
    (copy / "MEMORY.md").write_text("an arm wrote this\n")
    assert (real / "MEMORY.md").read_text() == "- [a fact](a.md)\n"  # the real one is untouched
    cmd, _ = ra.build_command(None, None, memory_dir=copy)
    assert json.loads(cmd[cmd.index("--settings") + 1])["autoMemoryDirectory"] == str(copy)


def test_a_repo_with_no_memory_gets_an_empty_scratch_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = _committed_repo(tmp_path)
    run_tmp = tmp_path / "run"
    run_tmp.mkdir()
    copy = ra.scratch_memory(root, run_tmp)
    assert copy.is_dir() and not any(copy.iterdir())


def test_peer_messages_in_a_transcript_are_counted(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    proj = cfg / "projects" / "-tmp-ablate-stripped-1-x-repo"
    proj.mkdir(parents=True)
    (proj / "sid-dirty.jsonl").write_text('{"c":"<cross-session-message from=x>hi"}\n{"c":"<cross-session-message from=y>"}\n')
    (proj / "sid-clean.jsonl").write_text('{"c":"ordinary turn"}\n')
    assert ra.cross_session_messages("sid-dirty") == 2
    assert ra.cross_session_messages("sid-clean") == 0   # control: a transcript that exists, clean
    assert ra.cross_session_messages("sid-missing") is None  # not found is not 0
    assert ra.cross_session_messages(None) is None
