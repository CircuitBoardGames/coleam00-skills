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
    proj = cfg / "projects" / "-tmp-ablate-run-x-repo"
    proj.mkdir(parents=True)
    tag = '<cross-session-message from="uds:/tmp/x.sock">hi</cross-session-message>'
    idle = {"type": "user", "message": {"role": "user", "content": tag}}
    busy = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": tag + tag}}
    (proj / "sid-dirty.jsonl").write_text("\n".join(json.dumps(e) for e in (idle, busy)) + "\n")
    # The shapes a CLEAN run carries the tag in (hub#1618): the agent reading run_ablation.py, its
    # tool call quoting it, the queue's bookkeeping, and the SendMessage docs.
    read = {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "tool_result", "content": tag}]}}
    call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"q": tag}}]}}
    queue = {"type": "queue-operation", "operation": "enqueue", "content": tag}
    docs = {"type": "attachment", "attachment": {"type": "deferred_tools_record", "text": tag}}
    (proj / "sid-clean.jsonl").write_text(
        "\n".join(json.dumps(e) for e in (read, call, queue, docs)) + "\n")
    assert ra.cross_session_messages("sid-dirty") == 2   # one per DELIVERY, not per substring
    assert ra.cross_session_messages("sid-clean") == 0   # the tag present, nothing delivered
    assert ra.cross_session_messages("sid-missing") is None  # not found is not 0
    assert ra.cross_session_messages(None) is None


def _repo_with_hooks(tmp_path: Path, commands: list[str]) -> Path:
    root = _repo(tmp_path)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": c} for c in commands]}]}}))
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, env=env)
    return root


def test_cache_hooks_are_snapshotted_once_and_only_the_matching_ones(tmp_path):
    root = _repo_with_hooks(tmp_path, ["echo HOT-PAGE # cache-inject", "echo unrelated-guard"])
    snaps = ra.snapshot_cache_hooks(root, "cache-inject", tmp_path / "snaps")
    assert [s.read_text() for s in snaps] == ["HOT-PAGE\n"]


def test_no_matching_cache_hook_is_an_error_not_an_empty_strip(tmp_path):
    root = _repo_with_hooks(tmp_path, ["echo unrelated-guard"])
    with pytest.raises(RuntimeError, match="nothing to strip"):
        ra.snapshot_cache_hooks(root, "cache-inject", tmp_path / "snaps")


def test_a_cache_hook_that_prints_nothing_is_an_error(tmp_path):
    root = _repo_with_hooks(tmp_path, ["true # cache-inject"])
    with pytest.raises(RuntimeError, match="gave no output"):
        ra.snapshot_cache_hooks(root, "cache-inject", tmp_path / "snaps")


def test_each_arm_gets_its_own_project_settings_and_the_rewrite_stays_out_of_the_diff(tmp_path):
    root = _repo_with_hooks(tmp_path, ["echo HOT-PAGE # cache-inject", "echo side-effect"])
    snaps = ra.snapshot_cache_hooks(root, "cache-inject", tmp_path / "snaps")
    sha = ra.head_sha(root)
    seen = {}
    for arm in (ra.CONTROL, ra.STRIPPED):
        rec = ra.run_one(root, sha, arm, 1, "x", [], None, "cp .claude/settings.json seen.json", 60,
                         False, strip_surfaces=("caches",), snapshots=snaps)
        assert rec["files_changed"] == ["seen.json"], rec  # the rewrite itself is hidden
        added = [l[1:] for l in rec["diff"].splitlines() if l.startswith("+") and not l.startswith("+++")]
        seen[arm] = json.loads("".join(added))
    hooks = [h["command"] for g in seen[ra.CONTROL]["hooks"]["SessionStart"] for h in g["hooks"]]
    assert hooks == [f"cat {snaps[0]}"]          # control: only the cache replay
    assert seen[ra.STRIPPED] == {}                # stripped: no hooks at all
    assert "side-effect" not in json.dumps(seen)  # the project's other hooks are gone from both


def test_the_user_instruction_file_is_excluded_in_the_stripped_arm_only(claude_on_path, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    cmd, _ = ra.build_command(None, None, extra_settings={"claudeMdExcludes": [str(tmp_path / "cfg" / "CLAUDE.md")]})
    assert json.loads(cmd[cmd.index("--settings") + 1])["claudeMdExcludes"] == [str(tmp_path / "cfg" / "CLAUDE.md")]
    plain, _ = ra.build_command(None, None)
    assert "claudeMdExcludes" not in json.loads(plain[plain.index("--settings") + 1])  # control


@pytest.mark.parametrize("extra, says", [
    (["--no-hooks"], "would silence it in the control arm"),
    (["--variant-patch", "x.patch"], "cannot combine with --hooks-only or --variant-patch"),
    (["--hooks-only", "h.json"], "cannot combine with --hooks-only or --variant-patch"),
])
def test_strip_surface_refuses_modes_that_would_silence_or_replace_it(tmp_path, extra, says):
    task = tmp_path / "task.md"
    task.write_text("do a thing")
    out = subprocess.run([sys.executable, str(SCRIPTS / "run_ablation.py"), str(tmp_path), "--task-file",
                          str(task), "--strip-surface", "caches", *extra, "--dry-run"],
                         capture_output=True, text=True)
    assert out.returncode == 2 and says in " ".join(out.stderr.split())  # argparse wraps lines


def test_the_final_message_is_kept_whole_for_grading(tmp_path):
    """A task graded on the final message was graded on a fragment: result_text was cut at 4000
    characters, and every routing answer of hub#1618's task A came after the cut."""
    root = _committed_repo(tmp_path)
    sha = ra.head_sha(root)
    long = "x" * 5000 + "END"
    runner = "printf '%%s' '%s'" % json.dumps({"result": long, "session_id": None})
    rec = ra.run_one(root, sha, ra.CONTROL, 1, "x", [], None, runner, 60, False)
    assert rec["result_text"] == long, len(rec["result_text"])
    rec = ra.run_one(root, sha, ra.CONTROL, 2, "x", [], None, "printf '%s'" % long, 60, False)
    assert rec["result_text"] == long, len(rec["result_text"])   # a plain-text runner too


def test_an_arm_cannot_read_the_stripped_layer_or_other_refs_from_git(tmp_path):
    """hub#1618 round 2: a stripped arm ran `git show HEAD:AGENTS.md` and read the layer it was
    stripped of, and a control arm cited a branch in flight -- a linked worktree shares the
    source repo's objects and refs. Each arm is now a one-commit repo with nothing else in it."""
    root = _committed_repo(tmp_path)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "second"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(root), "branch", "in-flight"], check=True)
    sha = ra.head_sha(root)
    targets = [{"path": "CLAUDE.md"}]
    probe = ("{ git show HEAD:CLAUDE.md 2>&1; git log --all --oneline | wc -l;"
             " git branch -a; git status --porcelain; } > probe.txt")
    seen = {}
    for arm in (ra.CONTROL, ra.STRIPPED):
        rec = ra.run_one(root, sha, arm, 1, "x", targets, None, probe, 60, False)
        seen[arm] = "\n".join(l[1:] for l in rec["diff"].splitlines()
                              if l.startswith("+") and not l.startswith("+++"))
        assert rec["files_changed"] == ["probe.txt"], rec
    assert "rules" in seen[ra.CONTROL]                # control: the layer is there
    assert "rules" not in seen[ra.STRIPPED], seen[ra.STRIPPED]
    for arm in seen:
        assert "in-flight" not in seen[arm], seen[arm]  # no foreign refs in either arm
        assert "\n1\n" in "\n" + seen[arm] + "\n", seen[arm]  # one commit, no history
    assert (root / "CLAUDE.md").exists() and "in-flight" in subprocess.run(
        ["git", "-C", str(root), "branch"], capture_output=True, text=True).stdout


def test_the_working_directory_does_not_name_the_arm(tmp_path):
    """Round 3 of the hub's AGENTS.md ablation: the temp dir was `ablate-<arm>-<n>-...`, so an
    agent that named its working directory in its final message (three of six did) told the
    blind grader which arm it was in. The path an arm runs in must say nothing about the arm."""
    root = _committed_repo(tmp_path)
    sha = ra.head_sha(root)
    for arm in (ra.CONTROL, ra.STRIPPED):
        rec = ra.run_one(root, sha, arm, 1, "x", [], None, "pwd -P > cwd.txt", 60, False)
        seen = [l[1:] for l in rec["diff"].splitlines() if l.startswith("+") and not l.startswith("+++")]
        assert len(seen) == 1 and seen[0].startswith("/"), rec   # the probe ran and wrote a path
        for name in (ra.CONTROL, ra.STRIPPED):
            assert name not in seen[0], "arm %s runs in %s" % (arm, seen[0])
