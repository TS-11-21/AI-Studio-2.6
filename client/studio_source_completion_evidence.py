"""Readonly same-job lineage checks for completion after a known budget stop.

No models, native Blender, billing approval, state mutation or claimed packing
proof lives here. The caller separately verifies the real SDK budget-stop/known
fee, formal-tool absence, saved packs and original scene/settings before launch.
"""
from __future__ import annotations

import json
from pathlib import Path

from studio_source_repair_evidence import (FRAME_NAMES, MAX_AUDIT_BYTES,
    MAX_SOURCE_BYTES, _hash, _mapping, _no_duplicates, _plain, _sha,
    frame_hashes, game_code_hashes)


def _read(path):
    path = _plain(path)
    if path.stat().st_size > MAX_AUDIT_BYTES:
        raise ValueError("Completion lineage metadata exceeds its bounded size.")
    return _mapping(json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicates))


def validate_completion_evidence(t, audit_file, record):
    """Return the original unchanged audit; no native packing claim is made."""
    job = _plain(t["job"], directory=True)
    if "root" in t:
        root = _plain(t["root"], directory=True)
        if job.parent != root / "jobs":
            raise ValueError("Completion must belong to this application's direct job directory.")
    paths = {name: _plain(t[name], directory=name != "source") for name in ("source", "art", "canonical", "game")}
    if any(not path.is_relative_to(job) for path in paths.values()) or paths["source"] != paths["art"] / "source.blend":
        raise ValueError("Completion must use this original member's same-job art/source.blend.")
    history = _plain(t["history"], directory=True)
    if history.parent != job / ".source-repair/history":
        raise ValueError("Completion history must be the original server-owned same-job operation directory.")
    authoritative = _read(history / "authorized-operation.json")
    if not isinstance(record, dict) or record != authoritative:
        raise ValueError("Client-provided or changed completion records are refused.")
    if record.get("schema") != "ai-studio-source-persistence-repair/v1" or record.get("job_id") != job.name:
        raise ValueError("The original source-correction operation identity is invalid.")
    old_sha = _sha(record.get("source_sha256"))
    if _hash(history / "source-before.blend", MAX_SOURCE_BYTES) != old_sha:
        raise ValueError("The archived original source differs from the authorized correction.")
    file = _plain(audit_file)
    if not file.is_relative_to(job) or _hash(file, MAX_AUDIT_BYTES) != _sha(record.get("audit_sha256")):
        raise ValueError("The original readonly failure receipt changed or belongs to another job.")
    audit = _read(file)
    if audit.get("schema") != "ai-studio-formal-claude-art-audit/v1" or audit.get("job_id") != job.name or audit.get("audit_status") != "PARTIAL_PACK_PERSISTENCE_FAIL":
        raise ValueError("Completion requires the original actual packing-failure receipt.")
    if type(audit.get("frame_count")) is not int or audit["frame_count"] != 18 or audit.get("asset_check_status") != "PASS" or audit.get("delivery_status") != "PASS":
        raise ValueError("Completion cannot repair other art/gameplay failures.")
    source = _mapping(audit.get("source"))
    native = _mapping(source.get("native_read"))
    packed = _mapping(source.get("packed_image_byte_check"))
    images = native.get("images")
    if _sha(source.get("sha256")) != old_sha or source.get("pack_persistence_status") != "FAIL" or packed.get("embedded_pngs_found") != 0 or packed.get("expected_frame_hashes_found") != 0:
        raise ValueError("The original audit does not prove the authorized zero-packed failure.")
    if not isinstance(images, list) or any(not isinstance(image, dict) or image.get("packed") for image in images):
        raise ValueError("Original zero-packed native image inventory is invalid.")
    if native.get("source_unchanged") is not True or native.get("new_background_process") is not True or native.get("autoexec_disabled") is not True or native.get("isolated_marker") is not True or not native.get("job_marker"):
        raise ValueError("Original receipt lacks independent readonly isolated disk evidence.")
    audited_source = Path(source.get("path", "")).absolute()
    if audited_source not in (paths["source"], paths["canonical"] / "source.blend") or Path(native.get("loaded_source", "")).absolute() != audited_source:
        raise ValueError("Original audited source is not this original member/canonical source.")
    expected = _mapping(record.get("png_sha256"))
    if set(expected) != set(FRAME_NAMES) or any(_sha(value) != value for value in expected.values()):
        raise ValueError("Original correction must protect exactly the 18 template PNGs.")
    rows = audit.get("frames")
    if not isinstance(rows, list) or len(rows) != 18:
        raise ValueError("Original audit frame list is incomplete.")
    receipt_frames = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("relative_path") not in FRAME_NAMES or row["relative_path"] in receipt_frames:
            raise ValueError("Original audit frame paths are unsafe or duplicated.")
        receipt_frames[row["relative_path"]] = _sha(row.get("file_sha256"))
    if receipt_frames != expected or any(frame_hashes(folder) != expected for folder in (paths["art"], paths["canonical"], paths["game"] / "assets/fan_wave_001")):
        raise ValueError("Original/candidate/canonical/game PNG bytes changed before completion.")
    code = _mapping(record.get("game_code_sha256"))
    if _mapping(audit.get("repair_guard")).get("game_code_sha256") != code or game_code_hashes(paths["game"]) != code or _mapping(audit.get("game")).get("all_18_handoff_hashes_match") is not True:
        raise ValueError("Protected gameplay evidence changed before completion.")
    if _hash(paths["source"], MAX_SOURCE_BYTES) == old_sha:
        raise ValueError("Completion requires the actually changed source from the preceding worker.")
    return audit
