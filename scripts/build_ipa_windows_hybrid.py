#!/usr/bin/env python3
"""
Hybrid Windows + GitHub Actions (macos-26) IPA Builder for seedds/nxnative.
Self-contained robust version for Windows local preparation.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TITLE_ID_RE = re.compile(r"\[([0-9A-Fa-f]{16})\]")
VERSION_RE = re.compile(r"\[v(\d+)\]", re.IGNORECASE)

KNOWN_ALIASES = {
    "arceus": "0100F2C0115B0000",
    "legendsarceus": "0100F2C0115B0000",
    "totk": "0100F2C008FBE000",
    "tearsofkingdom": "0100F2C008FBE000",
    "botw": "01007EF00011E000",
    "breathofthewild": "01007EF00011E000",
}

SHADER_EXTS = {
    ".bfsha", ".bnsh", ".sharc", ".sharcfb", ".spv", ".glsl",
    ".vert", ".frag", ".comp", ".shader", ".sbin", ".xsh",
}

def log(msg: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def fatal(msg: str) -> None:
    print(f"\n[ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)

def find_repo_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in [here.parent, here, Path.cwd()]:
        if (candidate / "dm" / "games").is_dir() and (candidate / "dm" / "tools").is_dir():
            return candidate
    fatal(
        "Could not locate the `nxnative` repository root (expected `dm/games` and `dm/tools`). "
        "Run this script from the `nxnative` directory."
    )
    return Path.cwd()

def setup_windows_env(repo_root: Path, shim_dir: Path) -> Dict[str, str]:
    env = os.environ.copy()
    shim_dir.mkdir(parents=True, exist_ok=True)
    extra_paths = [str(shim_dir)]

    vulkan_sdk = env.get("VULKAN_SDK")
    if not vulkan_sdk:
        vk_root = Path("C:/VulkanSDK")
        if vk_root.is_dir():
            versions = sorted([d for d in vk_root.iterdir() if d.is_dir()], reverse=True)
            if versions:
                vulkan_sdk = str(versions[0])
                env["VULKAN_SDK"] = vulkan_sdk

    if vulkan_sdk:
        vk_bin = Path(vulkan_sdk) / "Bin"
        if vk_bin.is_dir():
            extra_paths.append(str(vk_bin))

    winget_packages = Path(os.path.expanduser("~")) / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.is_dir():
        for zd in winget_packages.glob("Meta.Zstandard_*"):
            zstd_bins = list(zd.rglob("zstd.exe"))
            if zstd_bins:
                extra_paths.append(str(zstd_bins[0].parent))
                break

    if platform.system() == "Windows":
        py3_cmd = shim_dir / "python3.cmd"
        py3_cmd.write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")

    env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
    env["PYTHONUTF8"] = "1"
    return env

def check_prerequisites(env: Dict[str, str]) -> None:
    log("Checking Windows prerequisites...")
    missing = []
    for tool, hint in [
        ("zstd", "winget install -e --id Meta.Zstandard"),
        ("spirv-cross", "winget install -e --id KhronosGroup.VulkanSDK"),
        ("glslangValidator", "winget install -e --id KhronosGroup.VulkanSDK"),
    ]:
        if shutil.which(tool, path=env.get("PATH")):
            log(f"  [OK] {tool}")
        else:
            missing.append(f"  - {tool}: install via `{hint}`")

    for mod in ["lz4.block", "capstone", "fontTools"]:
        try:
            __import__(mod)
            log(f"  [OK] Python module `{mod}`")
        except ImportError:
            missing.append(f"  - Python `{mod}`")

    if missing:
        fatal("Missing prerequisites:\n" + "\n".join(missing))

def parse_game_tsv(game_tsv_path: Path) -> Dict[str, str]:
    info = {}
    if not game_tsv_path.is_file():
        return info
    for raw in game_tsv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            info[parts[0].strip()] = parts[1].strip()
    return info

def resolve_title_id(path: Path, override_id: Optional[str]) -> Tuple[str, Path]:
    if override_id:
        return override_id.upper(), path

    if path.is_file():
        m = TITLE_ID_RE.search(path.name)
        if m:
            return m.group(1).upper(), path
        fname_clean = re.sub(r"[^a-zA-Z]", "", path.name).lower()
        for alias, tid in KNOWN_ALIASES.items():
            if alias in fname_clean:
                log(f"Matched filename '{path.name}' to alias '{alias}' -> {tid}")
                return tid, path
    elif path.is_dir():
        for p in path.iterdir():
            m = TITLE_ID_RE.search(p.name)
            if m:
                return m.group(1).upper(), path

    fatal(f"Could not determine Title ID for '{path.name}'. Use option `--title-id <16-hex-id>`.")
    return "", path

def run_windows_stage1_prepare(
    repo_root: Path,
    rom_path: Path,
    prod_keys: Path,
    title_id: Optional[str],
    package_ver: Optional[str],
    language: str,
    app_name: Optional[str],
    bundle_id: Optional[str],
    show_fps: bool,
    trigger_cloud: bool,
    gh_repo: Optional[str],
) -> Path:
    if not prod_keys.is_file():
        fatal(f"prod.keys file not found at: {prod_keys}")

    tid, target_path = resolve_title_id(rom_path, title_id)
    work_dir = rom_path.parent if rom_path.is_file() else rom_path
    build_dir = work_dir / f"{tid}_build"
    shim_dir = build_dir / "win_shims"
    
    env = setup_windows_env(repo_root, shim_dir)
    check_prerequisites(env)

    profile_dir = repo_root / "dm" / "games" / tid
    profile_info = parse_game_tsv(profile_dir / "game.tsv")
    game_label = profile_info.get("name", tid)
    log(f"Target Game: {game_label} (Title ID: {tid})")

    extract_root = build_dir / "extract" / "v0"
    extract_root.mkdir(parents=True, exist_ok=True)
    (extract_root / "RomFS").mkdir(exist_ok=True)
    (extract_root / "exefs").mkdir(exist_ok=True)
    
    romfs_marker = profile_info.get("romfs_marker", ".keep")
    (extract_root / "RomFS" / romfs_marker.lstrip("/")).touch()
    
    payload_zip = work_dir / f"cloud_payload_{tid}.zip"
    log(f"Creating lightweight cloud handoff bundle: {payload_zip}")

    manifest = {
        "title_id": tid,
        "game_name": game_label,
        "version_dir": "v0",
        "language": language,
        "app_name": app_name or "",
        "bundle_id": bundle_id or "",
        "show_fps": show_fps,
        "romfs_marker": romfs_marker,
        "has_patched": False,
        "has_shader_pkg": False,
        "has_aoc": False,
    }

    with zipfile.ZipFile(payload_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("hybrid_manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"{tid}_build/extract/v0/RomFS/{romfs_marker.lstrip('/')}", b"")

    log(f"Successfully prepared payload archive: {payload_zip}")
    
    if trigger_cloud:
        if not shutil.which("gh"):
            fatal("GitHub CLI (`gh`) not found.")
        tag = f"hybrid-payload-{tid.lower()}"
        repo_flags = ["-R", gh_repo] if gh_repo else []
        subprocess.run(["gh", "release", "delete", tag, "--yes", "--cleanup-tag", *repo_flags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["gh", "release", "create", tag, str(payload_zip), "--title", f"Payload {tid}", "--prerelease", *repo_flags], check=True)
        subprocess.run(["gh", "workflow", "run", "build_macos_stage.yml", "-f", f"title_id={tid}", "-f", f"payload_source=release:{tag}", *repo_flags], check=True)
        log("GitHub Action cloud workflow triggered successfully!")

    return payload_zip

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid Windows IPA Builder")
    sub = parser.add_subparsers(dest="command", required=True)
    
    sub.add_parser("check")
    
    prep = sub.add_parser("prepare")
    prep.add_argument("rom_path", type=Path)
    prep.add_argument("-k", "--keys", type=Path, required=True)
    prep.add_argument("--title-id", type=str, default=None)
    prep.add_argument("-p", "--package", type=str, default=None)
    prep.add_argument("-l", "--language", type=str, default="en-US")
    prep.add_argument("--app-name", type=str, default=None)
    prep.add_argument("--bundle-id", type=str, default=None)
    prep.add_argument("--show-fps", action="store_true")
    prep.add_argument("--trigger-cloud", action="store_true")
    prep.add_argument("--gh-repo", type=str, default=None)
    return parser

def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    repo_root = find_repo_root()

    if args.command == "check":
        with tempfile.TemporaryDirectory() as tmp:
            env = setup_windows_env(repo_root, Path(tmp))
            check_prerequisites(env)
    elif args.command == "prepare":
        run_windows_stage1_prepare(
            repo_root=repo_root,
            rom_path=args.rom_path.resolve(),
            prod_keys=args.keys.resolve(),
            title_id=args.title_id,
            package_ver=args.package,
            language=args.language,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            show_fps=args.show_fps,
            trigger_cloud=args.trigger_cloud,
            gh_repo=args.gh_repo,
        )

if __name__ == "__main__":
    main()
