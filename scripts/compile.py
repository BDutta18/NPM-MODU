#!/usr/bin/env python3
"""
compile.py — Compile ModuEscrow to TEAL using puyapy.

This script wraps the puyapy compiler in a way that works regardless of
whether `algokit` is installed via pipx or whether puyapy is on PATH.

It tries three methods in order:
  1. algokit compile python ...   (requires algokit + pipx)
  2. puyapy ... / python -m puya  (if puyapy is on PATH)
  3. Falls back with a helpful error.

Usage:
  python scripts/compile.py
  python scripts/compile.py --out-dir my_artifacts/
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CONTRACT = "contracts/modu_escrow.py"
DEFAULT_OUT = "artifacts"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs, capture_output=True, text=True)


def try_algokit(out_dir: str) -> bool:
    algokit = shutil.which("algokit")
    if not algokit:
        return False
    print(f"  Using algokit: {algokit}")
    result = run([algokit, "compile", "python", CONTRACT, "--out-dir", out_dir])
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        return False
    return True


def try_puyapy(out_dir: str) -> bool:
    # Try direct executable
    for cmd_name in ["puyapy", "puya"]:
        exe = shutil.which(cmd_name)
        if exe:
            print(f"  Using {cmd_name}: {exe}")
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            result = run([exe, CONTRACT, "--out-dir", out_dir], env=env)
            print(result.stdout)
            if result.returncode != 0:
                print(result.stderr)
                return False
            return True
    # Try python -m puya
    result = run([sys.executable, "-m", "puya", CONTRACT, "--out-dir", out_dir],
                 env={**os.environ, "NO_COLOR": "1"})
    if result.returncode == 0:
        print(result.stdout)
        return True
    return False


def main() -> None:
    out_dir = DEFAULT_OUT
    if "--out-dir" in sys.argv:
        idx = sys.argv.index("--out-dir")
        out_dir = sys.argv[idx + 1]

    os.makedirs(out_dir, exist_ok=True)
    print(f"Compiling {CONTRACT} → {out_dir}/\n")

    if try_algokit(out_dir):
        pass
    elif try_puyapy(out_dir):
        pass
    else:
        sys.exit(
            "\n[ERROR] No working compiler found.\n\n"
            "Install one of:\n"
            "  pip install pipx && pipx install algokit\n"
            "  # then: algokit compile python contracts/modu_escrow.py --out-dir artifacts/\n\n"
            "Or for a version-matched standalone install:\n"
            "  pip install algorand-python==2.0.0 puya==0.6.0\n"
            "  puyapy contracts/modu_escrow.py --out-dir artifacts/\n"
        )

    # Report what was produced
    import glob
    teal_files = glob.glob(f"{out_dir}/*.teal") + glob.glob(f"{out_dir}/*.json")
    if teal_files:
        print("\nArtifacts produced:")
        for f in sorted(teal_files):
            size = os.path.getsize(f)
            print(f"  {f}  ({size} bytes)")
    else:
        print(f"\n[WARN] No .teal files found in {out_dir}/")


if __name__ == "__main__":
    main()
