#!/usr/bin/env python3
"""
Hybrid Windows + GitHub Actions (macos-14) IPA Builder for nxnative.
Handles local validation, smart ROM discovery, multi-part >2GB release chunking,
and automatic GitHub Actions cloud compilation triggering.
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
CHUNK_SIZE_BYTES = 1900 * 1024 * 1024  # 1.9 GB safe limit for GitHub Releases (< 2 GiB max)

KNOWN_ALIASES = {
    "arceus": "0100F2C0115B0000",
    "legendsarceus": "0100F2C0115B0000",
    "totk": "0100F2C008FBE000",
    "tearsofkingdom": "0100F2C008FBE000",
    "botw": "01007EF00011E000",
    "breathofthewild": "01007EF00011E000",
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
        "Run this script from `C:\\Users\\Diego\\nxnative`."
    )
    return Path.cwd()


def setup_windows_env(shim_dir: Path) -> Dict[str, str]:
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

    winget_packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
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


def locate_real_rom_and_workdir(input_path: Path, tid: str) -> Tuple[Optional[Path], Path]:
    """
    Resolves the actual ROM file (.xci/.nsp) and working directory, even if an empty
    folder named 'Arceus.xci' was accidentally created by earlier script runs.
    """
    # Check if input_path is an accidentally created folder with a .xci/.nsp extension
    if input_path.is_dir() and input_path.suffix.lower() in {".xci", ".nsp"}:
        roms_inside = [
            p for p in input_path.iterdir()
            if p.is_file() and p.suffix.lower() in {".xci", ".nsp"} and p.stat().st_size > 1024 * 1024
        ]
        if not roms_inside:
            log(f"Detected accidental directory at '{input_path}'. Removing it and searching parent folder '{input_path.parent}'...")
            shutil.rmtree(input_path, ignore_errors=True)
            input_path = input_path.parent

    if input_path.is_file():
        if input_path.stat().st_size < 1024 * 1024:
            fatal(f"ROM file '{input_path}' is too small ({input_path.stat().st_size} bytes). Verify your ROM file.")
        return input_path, input_path.parent

    # If input_path does not exist directly (e.g. user passed Arceus.xci when file is Arceus [0100F2C0115B0000].xci)
    search_dir = input_path if input_path.is_dir() else input_path.parent
    if not search_dir.is_dir():
        fatal(f"Directory not found: {search_dir}")

    candidates = [
        p for p in search_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".xci", ".nsp"} and p.stat().st_size > 1024 * 1024
    ]
    if candidates:
        # Prefer candidate matching Title ID if present
        for c in candidates:
            if tid.lower() in c.name.lower():
                log(f"Auto-discovered ROM file: {c}")
                return c, search_dir
        log(f"Auto-discovered ROM file: {candidates[0]}")
        return candidates[0], search_dir

    return None, search_dir


def resolve_title_id(path: Path, override_id: Optional[str]) -> str:
    if override_id:
        return override_id.strip().upper()

    search_targets = [path]
    if path.is_dir():
        search_targets.extend(path.iterdir())
    elif path.parent.is_dir():
        search_targets.extend(path.parent.iterdir())

    for p in search_targets:
        m = TITLE_ID_RE.search(p.name)
        if m:
            return m.group(1).upper()
        fname_clean = re.sub(r"[^a-zA-Z]", "", p.name).lower()
        for alias, tid in KNOWN_ALIASES.items():
            if alias in fname_clean:
                return tid

    fatal(f"Could not determine Title ID for '{path.name}'. Pass `--title-id 0100F2C0115B0000`.")
    return ""


def split_file_if_needed(file_path: Path) -> List[Path]:
    """Splits payload zip into <1.9GB parts if it exceeds GitHub Release's 2GB per-asset limit."""
    total_size = file_path.stat().st_size
    if total_size <= CHUNK_SIZE_BYTES:
        return [file_path]

    log(f"Payload archive is {total_size / (1024**3):.2f} GB (>1.9 GB limit). Splitting into parts for GitHub Release...")
    parts: List[Path] = []
    part_num = 1
    with file_path.open("rb") as src:
        while True:
            chunk = src.read(CHUNK_SIZE_BYTES)
            if not chunk:
                break
            part_path = file_path.with_name(f"{file_path.name}.part{part_num:03d}")
            with part_path.open("wb") as dst:
                dst.write(chunk)
            parts.append(part_path)
            log(f"  Created chunk {part_path.name} ({len(chunk) / (1024**2):.1f} MB)")
            part_num += 1
    return parts


def has_valid_extracted_build(build_dir: Path) -> bool:
    """Checks if <tid>_build/extract contains real extracted files (not just an empty .keep)."""
    extract_dir = build_dir / "extract"
    if not extract_dir.is_dir():
        return False
    real_files = [
        f for f in extract_dir.rglob("*")
        if f.is_file() and f.name != ".keep" and f.stat().st_size > 0
    ]
    return len(real_files) > 0


def run_windows_stage1_prepare(
    repo_root: Path,
    rom_input: Path,
    prod_keys: Path,
    title_id: Optional[str],
    language: str,
    app_name: Optional[str],
    bundle_id: Optional[str],
    show_fps: bool,
    trigger_cloud: bool,
    gh_repo: Optional[str],
) -> Path:
    if not prod_keys.is_file():
        fatal(f"prod.keys file not found at: {prod_keys}")

    tid = resolve_title_id(rom_input, title_id)
    rom_file, work_dir = locate_real_rom_and_workdir(rom_input, tid)
    build_dir = work_dir / f"{tid}_build"

    with tempfile.TemporaryDirectory() as tmp_shim:
        env = setup_windows_env(Path(tmp_shim))
        check_prerequisites(env)

    has_extracted = has_valid_extracted_build(build_dir)
    if rom_file is None and not has_extracted:
        existing = [p.name for p in work_dir.iterdir()] if work_dir.is_dir() else []
        fatal(
            f"No valid .xci/.nsp ROM file and no extracted build found in '{work_dir}'.\n"
            f"Files currently in '{work_dir}': {existing}"
        )

    # Clean up old partial chunks in work_dir
    for old_part in work_dir.glob(f"cloud_payload_{tid}.zip*"):
        old_part.unlink(missing_ok=True)

    payload_zip = work_dir / f"cloud_payload_{tid}.zip"
    rom_arcname = None
    if rom_file is not None:
        # Ensure filename in cloud archive includes [TitleID] so nxnative always recognizes it
        if f"[{tid.lower()}]" not in rom_file.name.lower():
            rom_arcname = f"{rom_file.stem} [{tid}][v0]{rom_file.suffix}"
        else:
            rom_arcname = rom_file.name

    manifest = {
        "title_id": tid,
        "rom_filename": rom_arcname or "",
        "language": language,
        "app_name": app_name or "",
        "bundle_id": bundle_id or "",
        "show_fps": show_fps,
        "has_extracted": has_extracted,
    }

    log(f"Building cloud payload archive: {payload_zip}")
    with zipfile.ZipFile(payload_zip, "w", allowZip64=True) as zf:
        zf.writestr("hybrid_manifest.json", json.dumps(manifest, indent=2))
        zf.write(prod_keys, arcname="prod.keys", compress_type=zipfile.ZIP_DEFLATED)

        if has_extracted:
            log(f"Packing pre-extracted build directory: {build_dir / 'extract'}")
            for file_p in (build_dir / "extract").rglob("*"):
                if file_p.is_file() and file_p.name != ".keep":
                    rel = file_p.relative_to(work_dir).as_posix()
                    zf.write(file_p, arcname=rel, compress_type=zipfile.ZIP_DEFLATED)
        elif rom_file is not None and rom_arcname is not None:
            size_gb = rom_file.stat().st_size / (1024 ** 3)
            log(f"Packing ROM '{rom_file.name}' ({size_gb:.2f} GB) as '{rom_arcname}' (using fast ZIP_STORED)...")
            zf.write(rom_file, arcname=rom_arcname, compress_type=zipfile.ZIP_STORED)

    zip_size_mb = payload_zip.stat().st_size / (1024 ** 2)
    log(f"Payload archive created: {payload_zip} ({zip_size_mb:.2f} MB)")
    if payload_zip.stat().st_size < 1024:
        fatal("Payload archive is unexpectedly tiny (<1 KB). Aborting so an empty build isn't triggered.")

    upload_files = split_file_if_needed(payload_zip)

    if trigger_cloud:
        if not shutil.which("gh"):
            fatal("GitHub CLI (`gh`) not found in PATH.")
        tag = f"hybrid-payload-{tid.lower()}"
        repo_flags = ["-R", gh_repo] if gh_repo else []

        log(f"Cleaning previous release tag '{tag}' (if any)...")
        subprocess.run(
            ["gh", "release", "delete", tag, "--yes", "--cleanup-tag", *repo_flags],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        log(f"Uploading {len(upload_files)} payload file(s) to GitHub Release '{tag}'...")
        subprocess.run(
            [
                "gh", "release", "create", tag,
                *[str(f) for f in upload_files],
                str(prod_keys),
                "--title", f"Payload {tid}",
                "--notes", f"Automated hybrid payload for {tid}",
                "--prerelease",
                *repo_flags,
            ],
            check=True,
        )

        log("Triggering GitHub Actions workflow 'build_macos_stage.yml'...")
        subprocess.run(
            [
                "gh", "workflow", "run", "build_macos_stage.yml",
                "-f", f"title_id={tid}",
                "-f", f"payload_source=release:{tag}",
                *repo_flags,
            ],
            check=True,
        )
        log("GitHub Actions cloud workflow triggered successfully!")

    return payload_zip


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid Windows IPA Builder for nxnative")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Check local Windows prerequisites")

    prep = sub.add_parser("prepare", help="Package ROM/assets and trigger macOS cloud IPA build")
    prep.add_argument("rom_path", type=Path, help="Path to ROM file (.xci/.nsp) or game folder")
    prep.add_argument("-k", "--keys", type=Path, required=True, help="Path to prod.keys")
    prep.add_argument("--title-id", type=str, default=None, help="16-char hex Title ID")
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
            env = setup_windows_env(Path(tmp))
            check_prerequisites(env)
    elif args.command == "prepare":
        run_windows_stage1_prepare(
            repo_root=repo_root,
            rom_input=args.rom_path.resolve(),
            prod_keys=args.keys.resolve(),
            title_id=args.title_id,
            language=args.language,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            show_fps=args.show_fps,
            trigger_cloud=args.trigger_cloud,
            gh_repo=args.gh_repo,
        )


if __name__ == "__main__":
    main()
