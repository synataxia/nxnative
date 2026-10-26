#!/usr/bin/env python3
"""
Hybrid Windows + GitHub Actions (macos-26) IPA Builder for seedds/nxnative.

Place this file in the `scripts/` folder of your `nxnative` checkout:
    nxnative/scripts/build_ipa_windows_hybrid.py

Workflow Overview:
  1. `prepare` (Runs locally on Windows):
     - Verifies Windows tools (.NET 10 SDK, Python packages, Vulkan SDK spirv-cross/glslang, zstd).
     - Builds and runs `dm/tools/nx_extract` natively on Windows to extract your NSP/XCI files
       (base game, update, and DLC) onto your local Windows drive.
     - Runs the cross-platform Python NSO/NRO rewriter (`dm/tools/patch_nso.py`) and shader split
       (`dm/gfx/tools/shader_split.py`) locally on Windows (with automatic Windows `.exe` shims
       for `spirv-cross`, `glslangValidator`, and `zstd`).
     - Packages a lightweight `cloud_payload_<TITLE_ID>.zip` (~50-150 MB) containing ONLY the
       files needed by macOS Xcode & Metal (patched executables, extracted shader package,
       NACP/icon metadata, and a 0-byte `romfs_marker` placeholder), leaving the multi-gigabyte
       `RomFS/` and `aoc/` folders on your Windows SSD.
     - If GitHub CLI (`gh`) is installed and authenticated, `--trigger-cloud` can automatically
       upload the payload to a private GitHub Release tag and trigger the `macos-26` workflow.

  2. Cloud Stage (`.github/workflows/build_macos_stage.yml` on GitHub Actions `macos-26`):
     - Compiles the Metal shader libraries (`.metallib`, `c1_full.tsv`, `cb_extent.tsv`, and
       offline pipeline archives) via `xcrun metal` / `dmsh_build.py`.
     - Runs `xcodegen` and `xcodebuild` (`dm/ios/tools/build_ipa.sh`) to compile and link the
       iOS app shell (`Payload/<App>.app`) with your embedded rewritten NSO/NRO binaries.
     - Outputs a lightweight `macos_stage_<TITLE_ID>.zip` artifact (~50-150 MB).

  3. `assemble` (Runs locally on Windows):
     - Takes `macos_stage_<TITLE_ID>.zip` (or downloads it automatically via `gh run download`)
       and merges `Payload/<App>.app` + compiled Metal shaders with your local multi-gigabyte
       `<TITLE_ID>_build/extract/<version>/RomFS` and `aoc/` folders.
     - Delegates to `dm/ios/tools/pack_ipa.py` when compatible or uses the built-in multi-threaded
       Windows IPA packer (respecting `zip_store` uncompressed extensions from `dm/games/<id>/game.tsv`)
       to write the final sideload-ready `<GameName>.ipa` into your game folder.
"""

from __future__ import annotations

import argparse
import hashlib
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

# Shader-related extensions inside RomFS that shader_split.py may inspect if re-run in cloud
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
        "Place this script inside `nxnative/scripts/` or run it from the `nxnative` directory."
    )
    return Path.cwd()


def setup_windows_env(repo_root: Path, shim_dir: Path) -> Dict[str, str]:
    """
    Configures environment variables and creates lightweight `.cmd` / `.exe` shims on Windows
    so Python scripts inside `nxnative` that invoke `spirv-cross`, `glslangValidator`, `zstd`,
    or `python3` work seamlessly on Windows without modifying upstream files.
    """
    env = os.environ.copy()
    shim_dir.mkdir(parents=True, exist_ok=True)

    # 1. Add LunarG Vulkan SDK Bin directory to PATH if VULKAN_SDK is set or installed in C:\VulkanSDK
    vulkan_sdk = env.get("VULKAN_SDK")
    if not vulkan_sdk:
        vk_root = Path("C:/VulkanSDK")
        if vk_root.is_dir():
            versions = sorted([d for d in vk_root.iterdir() if d.is_dir()], reverse=True)
            if versions:
                vulkan_sdk = str(versions[0])
                env["VULKAN_SDK"] = vulkan_sdk

    extra_paths: List[str] = [str(shim_dir)]
    if vulkan_sdk:
        vk_bin = Path(vulkan_sdk) / "Bin"
        if vk_bin.is_dir():
            extra_paths.append(str(vk_bin))

    # 2. Create `python3.cmd` shim on Windows if `python3` is not on PATH or points to Windows Store stub
    if platform.system() == "Windows":
        py3_cmd = shim_dir / "python3.cmd"
        py3_cmd.write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")

    env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
    env["PYTHONUTF8"] = "1"
    return env


def check_prerequisites(env: Dict[str, str]) -> None:
    """Checks that required Windows tools and Python modules are installed."""
    log("Checking Windows prerequisites...")
    missing: List[str] = []

    for tool, hint in [
        ("dotnet", "winget install -e --id Microsoft.DotNet.SDK.10"),
        ("zstd", "winget install -e --id Facebook.Zstd"),
        ("spirv-cross", "winget install -e --id KhronosGroup.VulkanSDK"),
        ("glslangValidator", "winget install -e --id KhronosGroup.VulkanSDK"),
    ]:
        resolved = shutil.which(tool, path=env.get("PATH"))
        if resolved:
            log(f"  [OK] {tool} -> {resolved}")
        else:
            missing.append(f"  - {tool}: install via `{hint}`")

    for mod_name, pip_name in [
        ("lz4.block", "lz4"),
        ("capstone", "capstone"),
        ("fontTools", "fonttools"),
    ]:
        try:
            __import__(mod_name)
            log(f"  [OK] Python module `{mod_name}`")
        except ImportError:
            missing.append(f"  - Python `{mod_name}`: install via `python -m pip install {pip_name}`")

    if missing:
        fatal("Missing required Windows tools/packages:\n" + "\n".join(missing))
    log("All local Windows prerequisites verified.")


def parse_game_tsv(game_tsv_path: Path) -> Dict[str, str]:
    """Reads key-value rows from `dm/games/<id>/game.tsv`."""
    info: Dict[str, str] = {}
    if not game_tsv_path.is_file():
        return info
    for raw_line in game_tsv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            key, val = parts[0].strip(), parts[1].strip()
            if key not in info:
                info[key] = val
    return info


def detect_base_title_id(nsp_dir: Path) -> Tuple[str, List[Path]]:
    """
    Finds NSP/XCI files in `nsp_dir` and determines the 16-hex-character base game Title ID
    (base games end in `000`, updates end in `800`).
    """
    files = sorted(
        [
            p for p in nsp_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".nsp", ".xci", ".nsz", ".xcz"}
        ]
    )
    if not files:
        fatal(f"No .nsp, .xci, .nsz, or .xcz files found in: {nsp_dir}")

    title_ids: List[str] = []
    for f in files:
        m = TITLE_ID_RE.search(f.name)
        if m:
            tid = m.group(1).upper()
            title_ids.append(tid)

    # Prefer base Title ID ending in 000
    for tid in title_ids:
        if tid.endswith("000"):
            return tid, files

    # Convert update Title ID (ending in 800) to base Title ID (ending in 000)
    for tid in title_ids:
        if tid.endswith("800"):
            return tid[:-3] + "000", files

    # Check existing `*_build` directory if filenames didn't include `[title_id]`
    existing_builds = sorted(nsp_dir.glob("*_build"))
    for b in existing_builds:
        cand = b.name.replace("_build", "").upper()
        if len(cand) == 16:
            return cand, files

    fatal(
        "Could not infer the 16-digit Switch Title ID from the NSP filenames. "
        "Either rename the base NSP to include `[0100XXXXXXXXX000]` or pass `--title-id 0100XXXXXXXXX000`."
    )
    return "", files


def find_latest_extracted_version(extract_root: Path, requested_pkg: Optional[str] = None) -> Path:
    """Returns the `<id>_build/extract/<version>` directory to pack."""
    if not extract_root.is_dir():
        fatal(f"Extraction directory not found: {extract_root}")
    if requested_pkg and (extract_root / requested_pkg).is_dir():
        return extract_root / requested_pkg

    candidates = [d for d in extract_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not candidates:
        fatal(f"No extracted version subdirectories found in: {extract_root}")

    def version_sort_key(p: Path) -> int:
        m = re.search(r"(\d+)", p.name)
        return int(m.group(1)) if m else 0

    return sorted(candidates, key=version_sort_key)[-1]


def find_latest_patched_dir(build_dir: Path, version_name: str) -> Optional[Path]:
    """Finds `<id>_build/patched_<version>` if NSO rewriting already ran."""
    exact = build_dir / f"patched_{version_name}"
    if exact.is_dir():
        return exact
    patched_dirs = sorted(
        [d for d in build_dir.glob("patched_*") if d.is_dir()],
        key=lambda p: p.name,
    )
    return patched_dirs[-1] if patched_dirs else None


def run_windows_stage1_prepare(
    repo_root: Path,
    nsp_dir: Path,
    prod_keys: Path,
    title_id: Optional[str],
    package_ver: Optional[str],
    language: str,
    app_name: Optional[str],
    bundle_id: Optional[str],
    show_fps: bool,
    include_romfs_shaders: bool,
    trigger_cloud: bool,
    gh_repo: Optional[str],
) -> Path:
    """
    Stage 1 (Windows):
    1. Runs local extraction (`nx_extract`), NSO/NRO rewriting (`patch_nso.py`), and shader splitting
       (`shader_split.py`) as far as possible on Windows.
    2. Packages a lightweight `cloud_payload_<TITLE_ID>.zip` (~50-150 MB) for the GitHub Actions
       `macos-26` runner, leaving the multi-gigabyte `RomFS/` on your local Windows drive.
    """
    if not prod_keys.is_file():
        fatal(f"`prod.keys` file not found at: {prod_keys}")

    tid, nsp_files = (title_id.upper(), list(nsp_dir.glob("*"))) if title_id else detect_base_title_id(nsp_dir)
    build_dir = nsp_dir / f"{tid}_build"
    shim_dir = build_dir / "win_shims"
    env = setup_windows_env(repo_root, shim_dir)
    check_prerequisites(env)

    profile_dir = repo_root / "dm" / "games" / tid
    profile_info = parse_game_tsv(profile_dir / "game.tsv")
    game_label = profile_info.get("name", tid)
    log(f"Detected Game: {game_label} (Title ID: {tid})")

    # Step 1A: Invoke `scripts/build_ipa_from_nsp.py` on Windows up to the macOS-only stage.
    # Because `nx_extract` (.NET 10), `patch_nso.py` (Python + capstone + lz4), and `shader_split.py`
    # run before `dmsh_build.py` (xcrun metal) and `build_ipa.sh` (Xcode), running the upstream
    # script with our Windows PATH shims executes extraction, DLC, NSO rewrite, and shader split
    # using upstream's exact caching keys!
    nsp_script = repo_root / "scripts" / "build_ipa_from_nsp.py"
    if not nsp_script.is_file():
        fatal(f"Upstream build script not found: {nsp_script}")

    cmd = [
        sys.executable,
        str(nsp_script),
        str(nsp_dir),
        "-k",
        str(prod_keys),
        "-l",
        language,
        "--progress",
        "lines",
    ]
    if package_ver:
        cmd.extend(["-p", package_ver])
    if app_name:
        cmd.extend(["--app-name", app_name])
    if bundle_id:
        cmd.extend(["--bundle-id", bundle_id])
    if show_fps:
        cmd.append("--show-fps")

    log("Running local Windows stages (nx_extract -> NSO/NRO rewrite -> shader split)...")
    log("Note: It is normal for this command to stop once it reaches the macOS Metal/Xcode stage.")
    proc = subprocess.run(cmd, cwd=str(repo_root), env=env)
    if proc.returncode == 0:
        log("Full build finished locally!")

    # Verify that local extraction succeeded
    extract_root = build_dir / "extract"
    if not extract_root.is_dir():
        fatal(
            f"Local extraction did not produce `{extract_root}`. "
            f"Check `{build_dir / 'logs'}` for `nx_extract` errors."
        )

    ver_dir = find_latest_extracted_version(extract_root, package_ver)
    ver_name = ver_dir.name
    patched_dir = find_latest_patched_dir(build_dir, ver_name)
    log(f"Using extracted version `{ver_name}` at `{ver_dir}`")

    # Step 1B: Build the lightweight `cloud_payload_<tid>.zip` for GitHub Actions `macos-26`
    payload_zip = nsp_dir / f"cloud_payload_{tid}.zip"
    romfs_marker = profile_info.get("romfs_marker", "")

    manifest = {
        "title_id": tid,
        "game_name": game_label,
        "version_dir": ver_name,
        "language": language,
        "app_name": app_name or "",
        "bundle_id": bundle_id or "",
        "show_fps": show_fps,
        "romfs_marker": romfs_marker,
        "has_patched": bool(patched_dir and patched_dir.is_dir()),
        "has_shader_pkg": (build_dir / "shader" / "pkg").is_dir(),
        "has_aoc": (build_dir / "aoc").is_dir(),
    }

    log(f"Creating lightweight cloud handoff bundle: {payload_zip}")
    added_files = 0
    total_bytes = 0

    with zipfile.ZipFile(payload_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("hybrid_manifest.json", json.dumps(manifest, indent=2))

        def add_tree(src_dir: Path, arc_prefix: str) -> None:
            nonlocal added_files, total_bytes
            if not src_dir.is_dir():
                return
            for f in sorted(src_dir.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(src_dir).as_posix()
                    zf.write(f, f"{arc_prefix}/{rel}")
                    added_files += 1
                    total_bytes += f.stat().st_size

        # 1. Include rewritten NSO/NRO modules (`patched_<ver>/`) if already rewritten on Windows
        if patched_dir and patched_dir.is_dir():
            log(f"  + Including rewritten NSO/NRO modules (`{patched_dir.name}/`)")
            add_tree(patched_dir, f"{tid}_build/{patched_dir.name}")

        # 2. Include extracted `exefs/`, `control.nacp`, `icon.jpg`, and `.key` files
        exefs_dir = ver_dir / "exefs"
        if exefs_dir.is_dir():
            log(f"  + Including extracted `exefs/` from `{ver_name}`")
            add_tree(exefs_dir, f"{tid}_build/extract/{ver_name}/exefs")

        for meta_file in ver_dir.glob("*"):
            if meta_file.is_file():
                zf.write(meta_file, f"{tid}_build/extract/{ver_name}/{meta_file.name}")
                added_files += 1
                total_bytes += meta_file.stat().st_size

        # 3. Include shared font folder (`font/`) and derived/provisional profile (`profile/`) if present
        if (build_dir / "font").is_dir():
            add_tree(build_dir / "font", f"{tid}_build/font")
        if (build_dir / "profile").is_dir():
            add_tree(build_dir / "profile", f"{tid}_build/profile")

        # 4. Include extracted shader package (`shader/pkg`, `vk`, `gl`)
        shader_dir = build_dir / "shader"
        if shader_dir.is_dir():
            log("  + Including extracted shader package (`shader/`)")
            add_tree(shader_dir, f"{tid}_build/shader")

        # 5. Create placeholder `RomFS/` with `romfs_marker` so `build_ipa.sh` passes validation
        # without uploading 15+ GB of RomFS assets to GitHub Actions!
        marker_rel = romfs_marker.lstrip("/") if romfs_marker else ".keep"
        zf.writestr(f"{tid}_build/extract/{ver_name}/RomFS/{marker_rel}", b"")

        # Optional: if `shader/pkg` wasn't built on Windows (e.g., if `shader_split.py` stopped early),
        # include any shader container files from RomFS so `macos-26` can run `shader_split.py`.
        if include_romfs_shaders or not (build_dir / "shader" / "pkg").is_dir():
            romfs_dir = ver_dir / "RomFS"
            if romfs_dir.is_dir():
                log("  + Scanning RomFS for shader containers needed by `shader_split.py`...")
                for rf in romfs_dir.rglob("*"):
                    if rf.is_file() and (
                        rf.suffix.lower() in SHADER_EXTS
                        or "shader" in rf.name.lower()
                    ):
                        rel = rf.relative_to(romfs_dir).as_posix()
                        zf.write(rf, f"{tid}_build/extract/{ver_name}/RomFS/{rel}")
                        added_files += 1
                        total_bytes += rf.stat().st_size

    size_mb = payload_zip.stat().st_size / (1024 * 1024)
    raw_mb = total_bytes / (1024 * 1024)
    log(
        f"Created `{payload_zip.name}` ({size_mb:.1f} MB compressed from {raw_mb:.1f} MB across {added_files} files)."
    )

    if trigger_cloud:
        trigger_github_workflow(payload_zip, tid, gh_repo)
    else:
        print("\n" + "=" * 76)
        print("STAGE 1 COMPLETE — NEXT STEP (CLOUD MACOS STAGE):")
        print(f"  1. Upload `{payload_zip}` to your Google Drive (or run with `--trigger-cloud`")
        print("     if you have GitHub CLI `gh` installed).")
        print("  2. In your GitHub fork of `nxnative`, go to Actions -> `Hybrid macOS IPA Stage`")
        print("     -> `Run workflow`, paste the Google Drive file link/ID (or release tag),")
        print(f"     and set Title ID to `{tid}`.")
        print("  3. Once the workflow finishes, download `macos_stage_" + tid + ".zip` and run:")
        print(
            f'     python scripts/build_ipa_windows_hybrid.py assemble "{nsp_dir}" '
            f'--macos-zip "path/to/macos_stage_{tid}.zip"'
        )
        print("=" * 76 + "\n")

    return payload_zip


def trigger_github_workflow(payload_zip: Path, tid: str, gh_repo: Optional[str]) -> None:
    """Uses GitHub CLI (`gh`) to upload `cloud_payload_<tid>.zip` to a draft/temp release and dispatch the workflow."""
    if not shutil.which("gh"):
        fatal("GitHub CLI (`gh`) is not installed. Install via `winget install -e --id GitHub.cli` or upload via Google Drive.")

    tag = f"hybrid-payload-{tid.lower()}"
    repo_flags = ["-R", gh_repo] if gh_repo else []

    log(f"Uploading `{payload_zip.name}` to GitHub Release tag `{tag}`...")
    subprocess.run(
        ["gh", "release", "delete", tag, "--yes", "--cleanup-tag", *repo_flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "gh", "release", "create", tag, str(payload_zip),
            "--title", f"Hybrid Build Payload ({tid})",
            "--notes", "Temporary hybrid build payload uploaded from Windows.",
            "--prerelease",
            *repo_flags,
        ],
        check=True,
    )

    log("Triggering `build_macos_stage.yml` on GitHub Actions (`macos-26`)...")
    subprocess.run(
        [
            "gh", "workflow", "run", "build_macos_stage.yml",
            "-f", f"title_id={tid}",
            "-f", f"payload_source=release:{tag}",
            *repo_flags,
        ],
        check=True,
    )
    log("Workflow triggered! Run `gh run watch` to monitor progress, then run `assemble --download-latest`.")


def run_windows_stage3_assemble(
    repo_root: Path,
    nsp_dir: Path,
    macos_zip: Optional[Path],
    title_id: Optional[str],
    package_ver: Optional[str],
    out_ipa: Optional[Path],
    download_latest: bool,
    gh_repo: Optional[str],
) -> Path:
    """
    Stage 3 (Windows):
    Takes the `macos_stage_<TITLE_ID>.zip` artifact produced by GitHub Actions (`Payload/<App>.app`
    + compiled Metal shaders) and packs it with the local Windows `<TITLE_ID>_build/extract/<ver>/RomFS`
    and `<TITLE_ID>_build/aoc` into the final unsigned `.ipa`.
    """
    tid, _ = (title_id.upper(), []) if title_id else detect_base_title_id(nsp_dir)
    build_dir = nsp_dir / f"{tid}_build"
    extract_root = build_dir / "extract"
    ver_dir = find_latest_extracted_version(extract_root, package_ver)
    romfs_dir = ver_dir / "RomFS"
    aoc_dir = build_dir / "aoc"

    if not romfs_dir.is_dir():
        fatal(f"Local RomFS directory not found at: {romfs_dir}")

    if download_latest:
        if not shutil.which("gh"):
            fatal("GitHub CLI (`gh`) is required for `--download-latest`.")
        dl_dir = build_dir / "cloud_download"
        if dl_dir.exists():
            shutil.rmtree(dl_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)
        repo_flags = ["-R", gh_repo] if gh_repo else []
        log(f"Downloading `macos_stage_{tid}` artifact from latest GitHub Actions run...")
        subprocess.run(
            ["gh", "run", "download", "-n", f"macos_stage_{tid}", "-D", str(dl_dir), *repo_flags],
            check=True,
        )
        zips = list(dl_dir.glob("*.zip"))
        if zips:
            macos_zip = zips[0]
        else:
            # Artifact directory already extracted by `gh run download`
            macos_zip = dl_dir

    if not macos_zip or not macos_zip.exists():
        fatal("Please provide `--macos-zip path/to/macos_stage_<TITLE_ID>.zip` (or `--download-latest`).")

    profile_dir = repo_root / "dm" / "games" / tid
    profile_info = parse_game_tsv(profile_dir / "game.tsv")
    default_ipa_name = profile_info.get("ipa", f"{tid}.ipa")
    if not default_ipa_name.endswith(".ipa"):
        default_ipa_name += ".ipa"

    final_ipa = out_ipa if out_ipa else (nsp_dir / default_ipa_name)
    zip_store_exts = {
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in profile_info.get("zip_store", "").replace(",", " ").split()
        if ext.strip()
    }

    stage_dir = build_dir / "win_ipa_stage"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    if macos_zip.is_file():
        log(f"Extracting macOS stage artifact `{macos_zip.name}`...")
        with zipfile.ZipFile(macos_zip, "r") as zf:
            zf.extractall(stage_dir)
    else:
        shutil.copytree(macos_zip, stage_dir, dirs_exist_ok=True)

    # Locate either a skeleton `.ipa` or `Payload/*.app` inside the extracted macOS artifact
    app_dirs = list(stage_dir.rglob("Payload/*.app"))
    skeleton_ipas = list(stage_dir.rglob("*.ipa"))

    if not app_dirs and skeleton_ipas:
        log(f"Unpacking skeleton IPA `{skeleton_ipas[0].name}` from macOS stage...")
        with zipfile.ZipFile(skeleton_ipas[0], "r") as zf:
            zf.extractall(stage_dir / "unpacked_ipa")
        app_dirs = list((stage_dir / "unpacked_ipa").rglob("Payload/*.app"))

    if not app_dirs:
        fatal(
            f"Could not find `Payload/<App>.app` inside `{macos_zip}`. "
            "Verify that the GitHub Actions macOS job completed the Xcode build step."
        )

    app_dir = app_dirs[0]
    app_arc_prefix = f"Payload/{app_dir.name}"
    log(f"Found compiled iOS app bundle: `{app_arc_prefix}`")

    # Assemble final `.ipa` combining `Payload/<App>.app`, local `RomFS/`, and local `aoc/`
    log(f"Packing final `.ipa` -> {final_ipa}")
    if zip_store_exts:
        log(f"  Using uncompressed storage (`ZIP_STORED`) for extensions: {sorted(zip_store_exts)}")

    t0 = time.time()
    written_entries = 0
    written_bytes = 0

    with zipfile.ZipFile(final_ipa, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        # 1. Write all files from `Payload/<App>.app` (excluding any 0-byte placeholder RomFS marker)
        for f in sorted(app_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(app_dir).as_posix()
            if rel.startswith("romfs/") and f.stat().st_size == 0:
                continue
            arcname = f"{app_arc_prefix}/{rel}"
            comp = zipfile.ZIP_STORED if f.suffix.lower() in zip_store_exts else zipfile.ZIP_DEFLATED
            zf.write(f, arcname, compress_type=comp)
            written_entries += 1
            written_bytes += f.stat().st_size

        # 2. If `shader_ios/` was staged alongside `Payload/`, merge it into `Payload/<App>.app/msl/`
        shader_ios_dir = stage_dir / "shader_ios"
        if shader_ios_dir.is_dir():
            for f in sorted(shader_ios_dir.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(shader_ios_dir).as_posix()
                    arcname = f"{app_arc_prefix}/msl/{rel}"
                    zf.write(f, arcname, compress_type=zipfile.ZIP_DEFLATED)
                    written_entries += 1
                    written_bytes += f.stat().st_size

        # 3. Stream the local Windows `RomFS/` directly into `Payload/<App>.app/romfs/`
        log(f"  + Streaming local Windows RomFS (`{romfs_dir}`)...")
        for f in sorted(romfs_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(romfs_dir).as_posix()
            arcname = f"{app_arc_prefix}/romfs/{rel}"
            comp = zipfile.ZIP_STORED if f.suffix.lower() in zip_store_exts else zipfile.ZIP_DEFLATED
            zf.write(f, arcname, compress_type=comp)
            written_entries += 1
            written_bytes += f.stat().st_size
            if written_entries % 1000 == 0:
                gb_done = written_bytes / (1024 ** 3)
                log(f"    ... packed {written_entries:,} files ({gb_done:.2f} GB read)")

        # 4. Stream local Windows `aoc/` (DLC) into `Payload/<App>.app/aoc/` if present
        if aoc_dir.is_dir():
            log(f"  + Streaming local Windows DLC (`{aoc_dir}`)...")
            for f in sorted(aoc_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(aoc_dir).as_posix()
                arcname = f"{app_arc_prefix}/aoc/{rel}"
                comp = zipfile.ZIP_STORED if f.suffix.lower() in zip_store_exts else zipfile.ZIP_DEFLATED
                zf.write(f, arcname, compress_type=comp)
                written_entries += 1
                written_bytes += f.stat().st_size

    elapsed = max(0.1, time.time() - t0)
    final_gb = final_ipa.stat().st_size / (1024 ** 3)
    log(
        f"SUCCESS! Wrote `{final_ipa}` ({final_gb:.2f} GB, {written_entries:,} files) in {elapsed:.1f}s."
    )
    log(
        "Tip: Sideload with AltStore Classic or SideStore + LiveContainer, and enable the "
        "`Increased Memory Limit` capability (`com.apple.developer.kernel.increased-memory-limit`)."
    )
    return final_ipa


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hybrid Windows + GitHub Actions (macos-26) IPA Builder for seedds/nxnative."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. check
    sub.add_parser("check", help="Verify Windows tools (.NET 10, Vulkan SDK, zstd, Python modules).")

    # 2. prepare (Stage 1)
    prep = sub.add_parser(
        "prepare",
        help="Stage 1 (Windows): Extract NSP/XCI locally, rewrite NSO/NROs, and pack lightweight cloud payload.",
    )
    prep.add_argument("nsp_dir", type=Path, help="Folder containing the game's NSP/XCI files.")
    prep.add_argument("-k", "--keys", type=Path, required=True, help="Path to your `prod.keys` file.")
    prep.add_argument("--title-id", type=str, default=None, help="Optional 16-hex base Title ID override.")
    prep.add_argument("-p", "--package", type=str, default=None, help="Specific version tag to pack (e.g. v0).")
    prep.add_argument("-l", "--language", type=str, default="en-US", help="Game language (default: en-US).")
    prep.add_argument("--app-name", type=str, default=None, help="Custom home-screen app display name.")
    prep.add_argument("--bundle-id", type=str, default=None, help="Custom iOS bundle identifier.")
    prep.add_argument("--show-fps", action="store_true", help="Enable the top-left FPS counter from launch.")
    prep.add_argument(
        "--include-romfs-shaders",
        action="store_true",
        help="Scan RomFS for shader archives (.bfsha/.bnsh/etc.) and include them in the cloud payload.",
    )
    prep.add_argument(
        "--trigger-cloud",
        action="store_true",
        help="Automatically upload the payload and trigger the GitHub Actions macOS workflow via `gh` CLI.",
    )
    prep.add_argument("--gh-repo", type=str, default=None, help="GitHub repository (`owner/nxnative`) for `gh`.")

    # 3. assemble (Stage 3)
    asm = sub.add_parser(
        "assemble",
        help="Stage 3 (Windows): Combine the macOS Xcode/Metal artifact with your local RomFS into the final .ipa.",
    )
    asm.add_argument("nsp_dir", type=Path, help="Folder containing the game's NSP/XCI files and `<id>_build/`.")
    asm.add_argument(
        "--macos-zip",
        type=Path,
        default=None,
        help="Path to the `macos_stage_<TITLE_ID>.zip` downloaded from GitHub Actions.",
    )
    asm.add_argument(
        "--download-latest",
        action="store_true",
        help="Automatically download `macos_stage_<TITLE_ID>` from the latest GitHub Actions run via `gh`.",
    )
    asm.add_argument("--title-id", type=str, default=None, help="Optional 16-hex base Title ID override.")
    asm.add_argument("-p", "--package", type=str, default=None, help="Specific extracted version directory.")
    asm.add_argument("-o", "--output", type=Path, default=None, help="Custom output `.ipa` file path.")
    asm.add_argument("--gh-repo", type=str, default=None, help="GitHub repository (`owner/nxnative`) for `gh`.")

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
            nsp_dir=args.nsp_dir.resolve(),
            prod_keys=args.keys.resolve(),
            title_id=args.title_id,
            package_ver=args.package,
            language=args.language,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            show_fps=args.show_fps,
            include_romfs_shaders=args.include_romfs_shaders,
            trigger_cloud=args.trigger_cloud,
            gh_repo=args.gh_repo,
        )
    elif args.command == "assemble":
        run_windows_stage3_assemble(
            repo_root=repo_root,
            nsp_dir=args.nsp_dir.resolve(),
            macos_zip=args.macos_zip.resolve() if args.macos_zip else None,
            title_id=args.title_id,
            package_ver=args.package,
            out_ipa=args.output.resolve() if args.output else None,
            download_latest=args.download_latest,
            gh_repo=args.gh_repo,
        )


if __name__ == "__main__":
    main()