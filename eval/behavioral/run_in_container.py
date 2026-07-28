#!/usr/bin/env python3
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""Run one skill's behavioral test inside a device-passthrough container.

This is the CI-only isolation layer. Locally you still run the suite bare
(``python -m pytest -c pytest.ini ...``); nothing here touches the harness.
On the self-hosted Strix Halo runners the workflow calls this launcher so the
tested skill -- which runs with ``--dangerously-skip-permissions`` and installs
Lemonade / pulls models -- executes in a throwaway container instead of on the
runner host.

Two entry points:

    python run_in_container.py --skill <name>      # build image + run the test
    python run_in_container.py --skill <name> --preflight   # device check only

The container gets the GPU/NPU passed through (so the skill can actually run
inference and produce out.png) but cannot modify the host outside the mounted
repo checkout. The host env that the model-pin depends on (CI / GITHUB_ACTIONS)
is forwarded explicitly, because ``docker run`` does not inherit host env.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BEHAVIORAL_DIR = Path(__file__).resolve().parent

# Secrets / config the harness (and the claude CLI it spawns) needs. Passed by
# name so ``docker run`` reads the value from this process's environment --
# this preserves multi-line values like ANTHROPIC_CUSTOM_HEADERS verbatim.
FORWARD_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "BEHAVIORAL_MODEL",
    "BEHAVIORAL_EFFORT",
    "CLAUDE_CODE_MAX_RETRIES",
)


def _linux_group_id(name: str) -> str | None:
    """Return the host GID for ``name`` so the container user can reach the
    GPU/NPU render nodes. Numeric GIDs are host-specific, hence resolved here
    rather than baked into the image."""
    try:
        import grp

        return str(grp.getgrnam(name).gr_gid)
    except (KeyError, ImportError):
        return None


def _linux_config() -> dict:
    devices = ["/dev/kfd", "/dev/dri"]
    # XDNA NPU node, only present when the amdxdna driver is loaded on the host.
    if Path("/dev/accel/accel0").exists():
        devices.append("/dev/accel/accel0")

    device_args: list[str] = []
    for dev in devices:
        if Path(dev).exists():
            device_args += ["--device", dev]
    for group in ("video", "render"):
        gid = _linux_group_id(group)
        if gid is not None:
            device_args += ["--group-add", gid]

    return {
        "dockerfile": str(BEHAVIORAL_DIR / "Dockerfile.linux"),
        "image": "behavioral-linux",
        "mount": [f"{REPO_ROOT}:/work"],
        "workdir": "/work/eval/behavioral",
        "device_args": device_args,
        # A cheap "can we see the accelerator?" probe used by --preflight.
        "device_check": [
            "bash",
            "-lc",
            "ls -l /dev/kfd /dev/dri 2>/dev/null; "
            "rocminfo 2>/dev/null | grep -m1 -i 'Marketing Name' || "
            "{ echo 'no ROCm device visible in container' >&2; exit 1; }",
        ],
    }


def _windows_config() -> dict:
    # DirectX GPU device class for Windows process-isolated containers.
    gpu_class = "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599"
    return {
        "dockerfile": str(BEHAVIORAL_DIR / "Dockerfile.windows"),
        "image": "behavioral-windows",
        "mount": [f"{REPO_ROOT}:C:\\work"],
        "workdir": "C:/work/eval/behavioral",
        # Process isolation is required for device passthrough on Windows.
        "device_args": ["--isolation=process", "--device", gpu_class],
        "device_check": [
            "powershell",
            "-NoProfile",
            "-Command",
            "$g = Get-CimInstance Win32_VideoController; "
            "if (-not $g) { Write-Error 'no GPU visible in container'; exit 1 }; "
            "$g | Select-Object -First 1 -ExpandProperty Name",
        ],
    }


def _os_config(os_name: str) -> dict:
    if os_name == "linux":
        return _linux_config()
    if os_name == "windows":
        return _windows_config()
    raise SystemExit(f"error: unsupported OS '{os_name}' (expected linux/windows)")


def _detect_os() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "linux":
        return "linux"
    raise SystemExit(f"error: containerized behavioral runs are only wired for "
                     f"Linux/Windows runners, not '{system}'")


def _image_tag(cfg: dict) -> str:
    """Content-hash the Dockerfile + requirements so cache hits are automatic
    and a changed definition gets a fresh tag."""
    h = hashlib.sha256()
    for path in (Path(cfg["dockerfile"]), BEHAVIORAL_DIR / "requirements.txt"):
        h.update(path.read_bytes())
    return f"{cfg['image']}:{h.hexdigest()[:12]}"


def _run(cmd: list[str]) -> int:
    printable = " ".join(cmd)
    print(f"[run_in_container] $ {printable}", flush=True)
    return subprocess.run(cmd).returncode


def _build(cfg: dict, tag: str) -> int:
    cmd = [
        "docker", "build",
        "-f", cfg["dockerfile"],
        "-t", tag,
        str(REPO_ROOT),
    ]
    return _run(cmd)


def _docker_run_prefix(cfg: dict, skill: str) -> list[str]:
    cmd = [
        "docker", "run", "--rm",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "1024",
        "-w", cfg["workdir"],
    ]
    for mount in cfg["mount"]:
        cmd += ["-v", mount]
    cmd += cfg["device_args"]

    for name in FORWARD_ENV:
        if os.environ.get(name) is not None:
            cmd += ["-e", name]
    # The tested skill selection + the CI markers the model-pin keys off of.
    cmd += ["-e", f"BEHAVIORAL_SKILL={skill}"]
    cmd += ["-e", "CI=true", "-e", "GITHUB_ACTIONS=true"]
    return cmd


def _test_command(skill: str) -> list[str]:
    test_file = f"../../skills/{skill}/evals/evals.py"
    return ["python", "-m", "pytest", "-c", "pytest.ini", "-p", "conftest", test_file]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Skill name under skills/ to test.")
    parser.add_argument(
        "--os",
        dest="os_name",
        choices=["linux", "windows"],
        default=_detect_os(),
        help="Container platform (defaults to the host OS).",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Only build the image and verify the GPU/NPU is visible inside "
             "the container; do not run the test.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Reuse an existing image tag instead of building.",
    )
    args = parser.parse_args(argv)

    cfg = _os_config(args.os_name)
    tag = _image_tag(cfg)

    if not args.no_build:
        rc = _build(cfg, tag)
        if rc != 0:
            print(f"[run_in_container] image build failed (exit {rc})", file=sys.stderr)
            return rc

    if args.preflight:
        cmd = _docker_run_prefix(cfg, args.skill) + [tag] + cfg["device_check"]
        rc = _run(cmd)
        if rc != 0:
            print(
                "[run_in_container] device preflight FAILED: the accelerator is "
                "not visible inside the container. The skill cannot run local "
                "inference here.",
                file=sys.stderr,
            )
        return rc

    cmd = _docker_run_prefix(cfg, args.skill) + [tag] + _test_command(args.skill)
    return _run(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
