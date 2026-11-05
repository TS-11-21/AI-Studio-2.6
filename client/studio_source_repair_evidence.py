"""Bounded readonly evidence checks for the narrow saved-image correction.

This module never executes Blender or a model, changes files, approves charges,
or authenticates an unsigned local receipt. The caller separately checks the
original member, lease, saved Blender identity, live packing state and budget.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_AUDIT_BYTES = 2_000_000
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_GAME_FILE_BYTES = 8 * 1024 * 1024
MAX_GAME_FILES = 4096
MAX_GAME_ENTRIES = 20_000
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FRAME_NAMES = tuple("frames/frame_%04d.png" % i for i in range(1, 19))


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError("Redirected source-correction paths are refused.")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse source-correction paths are refused.")
    info = path.stat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("The source-correction directory is missing.")
    elif not stat.S_ISREG(info.st_mode):
        raise ValueError("Source-correction evidence must be an existing regular file.")
    return path


def _hash(path, limit):
    path = _plain(path)
    before = path.stat()
    if before.st_size <= 0 or before.st_size > limit:
        raise ValueError("Source-correction evidence exceeds its bounded file size.")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Source-correction evidence is no longer a regular file.")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            if size > limit:
                raise ValueError("Source-correction evidence grew beyond its bounded size.")
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
        raise ValueError("Source-correction evidence changed during its readonly check.")
    return digest.hexdigest()


def frame_hashes(art):
    art = _plain(art, directory=True)
    result = {}
    for name in FRAME_NAMES:
        path = _plain(art / name)
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("A source-correction frame is not a PNG file.")
        result[name] = _hash(path, MAX_FRAME_BYTES)
    return result


def game_code_hashes(game):
    game = _plain(game, directory=True)
    result = {}
    pending = [(game, 0)]
    seen = 0
    while pending:
        folder, depth = pending.pop()
        if depth > 32:
            raise ValueError("Gameplay evidence exceeds its bounded directory depth.")
        for path in sorted(folder.iterdir()):
            seen += 1
            if seen > MAX_GAME_ENTRIES:
                raise ValueError("Gameplay evidence exceeds its bounded entry count.")
            if path.name.startswith("."):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Gameplay evidence contains a redirected path.")
            if stat.S_ISDIR(info.st_mode):
                pending.append((_plain(path, directory=True), depth + 1))
            elif path.suffix.lower() in (".gd", ".tscn", ".godot", ".gdshader", ".shader"):
                if len(result) >= MAX_GAME_FILES:
                    raise ValueError("Gameplay evidence exceeds its bounded code-file count.")
                result[path.relative_to(game).as_posix()] = _hash(path, MAX_GAME_FILE_BYTES)
    if "project.godot" not in result:
        raise ValueError("A real protected Godot project is required for this correction.")
    return result


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate audit JSON keys are refused.")
        result[key] = value
    return result


def _sha(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("The audit contains an invalid SHA256 value.")
    return value


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError("The audit evidence section must be a JSON object.")
    return value


def validate_evidence(t, audit_file):
    """Return the unchanged receipt only when all bounded disk evidence matches."""
    job = _plain(t["job"], directory=True)
    if "root" in t:
        root = _plain(t["root"], directory=True)
        if not job.is_relative_to(root / "jobs") or job.parent != root / "jobs":
            raise ValueError("The repair must use this application's direct job directory.")
    paths = {name: _plain(t[name], directory=name != "source") for name in ("source", "art", "canonical", "game")}
    if any(not path.is_relative_to(job) for path in paths.values()):
        raise ValueError("Source-correction evidence escapes the current job.")
    if paths["source"] != paths["art"] / "source.blend":
        raise ValueError("The correction source must be this member's exact art/source.blend.")
    file = _plain(audit_file)
    if not file.is_relative_to(job) or file.stat().st_size > MAX_AUDIT_BYTES:
        raise ValueError("The bounded readonly audit receipt must be inside this job.")
    audit = json.loads(file.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicates)
    if not isinstance(audit, dict) or audit.get("schema") != "ai-studio-formal-claude-art-audit/v1" or audit.get("job_id") != job.name:
        raise ValueError("Wrong actual source audit identity/schema.")
    if audit.get("audit_status") != "PARTIAL_PACK_PERSISTENCE_FAIL" or type(audit.get("frame_count")) is not int or audit["frame_count"] != 18:
        raise ValueError("A real 18-frame packing persistence failure is required.")
    if audit.get("asset_check_status") != "PASS" or audit.get("delivery_status") != "PASS":
        raise ValueError("This operation cannot repair other art or gameplay failures.")
    source = _mapping(audit.get("source"))
    packed = _mapping(source.get("packed_image_byte_check"))
    native = _mapping(source.get("native_read"))
    images = native.get("images")
    if not isinstance(images, list) or any(not isinstance(image, dict) for image in images):
        raise ValueError("The native saved-source image inventory is invalid.")
    if source.get("pack_persistence_status") != "FAIL" or packed.get("embedded_pngs_found") != 0 or packed.get("expected_frame_hashes_found") != 0 or any(image.get("packed") for image in images):
        raise ValueError("This operation requires the audited saved-source zero-packed failure.")
    if native.get("source_unchanged") is not True or native.get("new_background_process") is not True or native.get("autoexec_disabled") is not True or native.get("isolated_marker") is not True or not native.get("job_marker"):
        raise ValueError("The receipt must report an independent readonly isolated source reload.")
    if "path" in source:
        audited_source = _plain(source["path"])
        if audited_source not in (paths["source"], paths["canonical"] / "source.blend") or Path(native.get("loaded_source", "")).absolute() != audited_source:
            raise ValueError("The receipt's independently loaded source is not this member/canonical source.")
    original_sha = _sha(source.get("sha256"))
    if original_sha != _hash(paths["source"], MAX_SOURCE_BYTES) or original_sha != _hash(paths["canonical"] / "source.blend", MAX_SOURCE_BYTES):
        raise ValueError("The original/canonical source differs from the readonly failure audit.")
    rows = audit.get("frames")
    if not isinstance(rows, list) or len(rows) != 18:
        raise ValueError("The audit must list exactly 18 individual original PNG files.")
    expected = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("relative_path") not in FRAME_NAMES or row["relative_path"] in expected:
            raise ValueError("The audit contains duplicate or unsafe frame paths.")
        expected[row["relative_path"]] = _sha(row.get("file_sha256"))
    if expected != frame_hashes(paths["art"]) or expected != frame_hashes(paths["canonical"]):
        raise ValueError("Original/canonical PNG bytes differ from the readonly audit.")
    game = _mapping(audit.get("game"))
    if game.get("all_18_handoff_hashes_match") is not True:
        raise ValueError("The original game frame handoff was not audited as successful.")
    if expected != frame_hashes(paths["game"] / "assets/fan_wave_001"):
        raise ValueError("Actual game frame bytes differ from the original art PNGs.")
    protected = _mapping(audit.get("repair_guard")).get("game_code_sha256")
    if not isinstance(protected, dict) or not protected or any(not isinstance(name, str) or _sha(value) != value for name, value in protected.items()) or protected != game_code_hashes(paths["game"]):
        raise ValueError("Exact current gameplay hashes must accompany the readonly receipt.")
    return audit
