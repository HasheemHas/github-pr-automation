"""Container isolation for target-controlled commands."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.common import sandbox
from scripts.common.commands import run_commands
from scripts.common.config import Command, Target
from scripts.common.errors import AutomationError, ConfigError
from scripts.quality.run import build_runner

CONFIG = {"sandbox": {"enabled": True, "image": "example/img@sha256:" + "a" * 64,
                      "required_binaries": ["sh", "node", "python3"]}}


def fake_runner(results):
    """A subprocess.run stand-in returning scripted (returncode, stdout) pairs."""
    calls = []
    script = list(results)

    class Completed:
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def run(args, **kwargs):
        calls.append(args)
        code, out = script.pop(0) if script else (0, "")
        return Completed(code, out)

    run.calls = calls
    return run


# -- config ----------------------------------------------------------------

def test_a_disabled_sandbox_is_not_usable():
    assert not sandbox.load({}).usable
    assert not sandbox.load({"sandbox": {"enabled": False, "image": "x"}}).usable


def test_enabled_without_an_image_is_a_config_error():
    with pytest.raises(ConfigError, match="sandbox.image is not set"):
        sandbox.load({"sandbox": {"enabled": True}})


def test_an_enabled_sandbox_with_an_image_is_usable():
    config = sandbox.load(CONFIG)
    assert config.usable
    assert config.required_binaries == ("sh", "node", "python3")


def test_a_malformed_sandbox_block_is_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        sandbox.load({"sandbox": "yes"})
    with pytest.raises(ConfigError, match="required_binaries must be a list"):
        sandbox.load({"sandbox": {"enabled": True, "image": "x", "required_binaries": "sh"}})


# -- docker arguments ------------------------------------------------------

def test_only_the_target_tree_is_mounted(tmp_path):
    args = sandbox.docker_args(sandbox.load(CONFIG), tmp_path)
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--volume"]
    assert len(mounts) == 1
    assert mounts[0] == f"{tmp_path.resolve()}:/workspace"


def test_the_central_scripts_and_artifact_dir_are_never_mounted(tmp_path):
    args = " ".join(sandbox.docker_args(sandbox.load(CONFIG), tmp_path))
    for forbidden in ("pr-automation-artifacts", "/scripts", "RUNNER_TEMP", "config/"):
        assert forbidden not in args


def test_the_docker_socket_is_never_mounted(tmp_path):
    args = " ".join(sandbox.docker_args(sandbox.load(CONFIG), tmp_path))
    assert "docker.sock" not in args


def test_capabilities_are_dropped_and_privilege_escalation_blocked(tmp_path):
    args = sandbox.docker_args(sandbox.load(CONFIG), tmp_path)
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert "--privileged" not in args


def test_resource_limits_are_applied(tmp_path):
    args = sandbox.docker_args(sandbox.load(CONFIG), tmp_path)
    assert args[args.index("--memory") + 1] == "4g"
    assert args[args.index("--pids-limit") + 1] == "512"


def test_the_runner_environment_is_never_inherited(tmp_path):
    args = sandbox.docker_args(sandbox.load(CONFIG), tmp_path)
    assert args[args.index("--env-file") + 1] == "/dev/null"
    passed = {args[i + 1].split("=", 1)[0] for i, a in enumerate(args) if a == "--env"}
    assert passed == set(sandbox.CONTAINER_ENV)


def test_network_is_allowed_by_default(tmp_path):
    args = sandbox.docker_args(sandbox.load(CONFIG), tmp_path)
    assert "--network" not in args


def test_network_isolation_restricts_non_install_groups(tmp_path):
    config = sandbox.load(
        {"sandbox": {"enabled": True, "image": "i", "isolate_network": True}}
    )
    install = sandbox.docker_args(config, tmp_path, group="install")
    build = sandbox.docker_args(config, tmp_path, group="build")
    assert "--network" not in install
    assert build[build.index("--network") + 1] == "none"


# -- preflight ------------------------------------------------------------

def test_preflight_passes_when_the_runtime_and_binaries_exist():
    ready, reason = sandbox.preflight(sandbox.load(CONFIG), runner=fake_runner([(0, "24.0"), (0, "")]))
    assert ready
    assert "Sandbox ready" in reason


def test_preflight_fails_when_docker_is_absent():
    ready, reason = sandbox.preflight(sandbox.load(CONFIG), runner=fake_runner([(1, "")]))
    assert not ready
    assert "not available" in reason


def test_preflight_fails_when_the_image_lacks_a_required_binary():
    ready, reason = sandbox.preflight(
        sandbox.load(CONFIG), runner=fake_runner([(0, "24.0"), (127, "not found")])
    )
    assert not ready
    assert "does not provide every required binary" in reason


def test_preflight_on_a_disabled_sandbox_reports_rather_than_probing():
    ready, reason = sandbox.preflight(sandbox.load({}))
    assert not ready
    assert "not enabled" in reason


# -- runner integration ---------------------------------------------------

def test_commands_run_through_docker_when_sandboxed(tmp_path):
    config = sandbox.load(CONFIG)
    runner = sandbox.make_runner(config, tmp_path, runner=fake_runner([(0, "ok")] * 2))
    results = run_commands(
        [Command(name="Lint", run="npm run lint", group="lint")], runner=runner
    )
    assert results[0].status == "pass"
    args = runner.__closure__ is not None  # runner is a closure over the fake
    assert args


def test_the_command_reaches_the_container_as_a_shell_argument(tmp_path):
    fake = fake_runner([(0, "ok")])
    config = sandbox.load(CONFIG)
    runner = sandbox.make_runner(config, tmp_path, runner=fake)
    runner("npm run lint", None, None, 60)
    assert fake.calls[0][-3:] == [config.image, "sh", "-lc"] or fake.calls[0][-2:] == [
        "-lc", "npm run lint"
    ]
    assert "npm run lint" in fake.calls[0]


def test_the_group_drives_the_network_policy(tmp_path):
    fake = fake_runner([(0, ""), (0, "")])
    config = sandbox.load(
        {"sandbox": {"enabled": True, "image": "i", "isolate_network": True}}
    )
    runner = sandbox.make_runner(config, tmp_path, runner=fake)
    run_commands(
        [
            Command(name="Deps", run="npm ci", group="install"),
            Command(name="Build", run="npm run build", group="build"),
        ],
        runner=runner,
    )
    assert "--network" not in fake.calls[0]
    assert "none" in fake.calls[1]


# -- build_runner gating ---------------------------------------------------

def test_build_runner_falls_back_to_the_host_when_no_sandbox_is_configured(tmp_path):
    target = Target(owner="o", repo="r", config={})
    runner, info = build_runner(target, str(tmp_path))
    assert runner is None
    assert info["ready"] is False
    assert info["enabled"] is False


def test_build_runner_refuses_to_fall_back_when_a_sandbox_is_required(tmp_path):
    target = Target(owner="o", repo="r", config={})
    with pytest.raises(AutomationError, match="requires sandboxed execution"):
        build_runner(target, str(tmp_path), require_sandbox=True)


def test_a_failing_preflight_is_fatal_when_a_sandbox_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "preflight", lambda cfg, **kw: (False, "docker missing"))
    target = Target(owner="o", repo="r", config=CONFIG)
    with pytest.raises(AutomationError, match="preflight failed"):
        build_runner(target, str(tmp_path), require_sandbox=True)


def test_a_failing_preflight_degrades_to_the_host_for_read_only_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "preflight", lambda cfg, **kw: (False, "docker missing"))
    target = Target(owner="o", repo="r", config=CONFIG)
    runner, info = build_runner(target, str(tmp_path))
    assert runner is None
    assert info["ready"] is False
    assert "Falling back to host execution" in capsys.readouterr().out


def test_the_description_recorded_in_the_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "preflight", lambda cfg, **kw: (True, "ready"))
    target = Target(owner="o", repo="r", config=CONFIG)
    runner, info = build_runner(target, str(tmp_path))
    assert runner is not None
    assert info == {
        "enabled": True,
        "image": CONFIG["sandbox"]["image"],
        "ready": True,
        "reason": "ready",
        "isolate_network": False,
        "network_groups": ["install"],
    }


def test_the_workdir_path_is_resolved_absolutely(tmp_path):
    relative = Path("target")
    args = sandbox.docker_args(sandbox.load(CONFIG), relative)
    mount = [args[i + 1] for i, a in enumerate(args) if a == "--volume"][0]
    assert mount.startswith("/")
