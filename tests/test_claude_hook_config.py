"""The live-launch hook must stay registered for both shell tools.

Removing or loosening the entry in .claude/settings.json would silently
switch the tripwire off, so these tests pin it.
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
HOOK_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "block_live_launch.py"


def _launch_hook_entries() -> list[dict]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if any("block_live_launch.py" in hook.get("command", "") for hook in entry["hooks"])
    ]


def test_hook_script_exists():
    assert HOOK_SCRIPT.is_file()


def test_pretooluse_hook_covers_bash_and_powershell():
    entries = _launch_hook_entries()
    assert entries, "block_live_launch.py is not registered as a PreToolUse hook"
    matcher = entries[0]["matcher"]
    assert re.fullmatch(matcher, "Bash")
    assert re.fullmatch(matcher, "PowerShell")


def test_hook_is_a_command_hook_pointing_at_the_project_script():
    (hook,) = [
        hook
        for entry in _launch_hook_entries()
        for hook in entry["hooks"]
        if "block_live_launch.py" in hook["command"]
    ]
    assert hook["type"] == "command"
    assert '"$CLAUDE_PROJECT_DIR/.claude/hooks/block_live_launch.py"' in hook["command"]


def test_hooks_are_not_disabled():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    assert settings.get("disableAllHooks") is not True
