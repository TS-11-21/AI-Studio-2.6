"""Read real VFX deliverables and verify decoded PNG alpha/animation content.

No network, model calls, Blender execution, installs, or asset changes occur.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def artifact_path(art: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Artifact filename must be relative.")
    path = (art / relative).resolve()
    if not path.is_relative_to(art.resolve()):
        raise ValueError("Artifact filename escapes the art bundle.")
    if not path.is_file():
        raise ValueError(f"Missing real artifact: {relative}")
    return path


def decode_blend_header_bytes(header: bytes) -> dict:
    """Accept only the official 12-byte v0 or 17-byte v1 .blend header layouts.

    Format definition: Blender's BLO_core_blend_header.hh, linked from the
    Blender 5.0 Core release notes. Format v1 has fixed '17-01v' markers;
    its four-digit Blender version is distinct from the file-format version.
    """
    old = header[:12]
    if (len(old) == 12 and old[:7] == b"BLENDER" and
            old[7:8] in (b"_", b"-") and old[8:9] in (b"v", b"V") and old[9:12].isdigit()):
        return {"stored_blender_version": old[9:12].decode("ascii"),
                "file_format_version": 0, "header_bytes": 12,
                "pointer_size": 4 if old[7:8] == b"_" else 8,
                "endianness": "little" if old[8:9] == b"v" else "big"}
    new = header[:17]
    if (len(new) == 17 and new[:13] == b"BLENDER17-01v" and new[13:17].isdigit()):
        return {"stored_blender_version": new[13:17].decode("ascii"),
                "file_format_version": 1, "header_bytes": 17,
                "pointer_size": 8, "endianness": "little"}
    raise ValueError("Source is not a verified Blender v0/v1 file header.")


def blend_header(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(17)
    compression = "none"
    if header.startswith(b"\x1f\x8b"):
        compression = "gzip"
        with gzip.open(path, "rb") as stream:
            header = stream.read(17)
    elif header.startswith(b"\x28\xb5\x2f\xfd"):
        compression = "zstd"
        try:
            from compression import zstd
        except ImportError as exc:
            raise ValueError("Compressed Blender source needs an existing zstd reader; no software was installed.") from exc
        with zstd.open(path, "rb") as stream:
            header = stream.read(17)
    decoded = decode_blend_header_bytes(header)
    if path.stat().st_size <= 100:
        raise ValueError("Blender source is implausibly small.")
    return {"filename": path.name, "bytes": path.stat().st_size,
            **decoded, "compression": compression}


def inspect_png(path: Path, expected_size: tuple[int, int]) -> dict:
    # First verify the real PNG structure/CRC, then decode its actual pixels.
    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != "RGBA" or image.size != expected_size:
            raise ValueError(f"Expected a {expected_size[0]}x{expected_size[1]} RGBA PNG: {path.name}")
        image.verify()
    with Image.open(path) as image:
        image.load()
        rgba = image.tobytes()
        alpha = image.getchannel("A")
        histogram = alpha.histogram()
        width, height = image.size
        corners = [alpha.getpixel((x, y)) for x, y in
                   ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
        border = [alpha.getpixel((x, 0)) for x in range(width)]
        border += [alpha.getpixel((x, height - 1)) for x in range(width)]
        border += [alpha.getpixel((0, y)) for y in range(1, height - 1)]
        border += [alpha.getpixel((width - 1, y)) for y in range(1, height - 1)]
        total = width * height
        if histogram[0] == 0 or any(corners):
            raise ValueError(f"Transparent background/corners are absent: {path.name}")
        return {"filename": path.name, "mode": image.mode, "size": list(image.size),
                "visible_pixels": total - histogram[0], "transparent_pixels": histogram[0],
                "transparent_fraction": histogram[0] / total,
                "partially_transparent_pixels": sum(histogram[1:255]),
                "alpha_range": list(alpha.getextrema()), "content_bounds": alpha.getbbox(),
                "corner_alpha": corners, "transparent_border_fraction": border.count(0) / len(border),
                "decoded_rgba_sha256": hashlib.sha256(rgba).hexdigest(),
                "decoded_alpha_sha256": hashlib.sha256(alpha.tobytes()).hexdigest(),
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verify(job: Path) -> dict:
    job = job.resolve()
    art = job / "art"
    task = read_json(job / "task.json")
    manifest = read_json(art / "manifest.json")
    spec = task["vfx"]
    expected_keys = ("effect_id", "fps", "frame_count", "size", "pivot", "blend_mode", "source", "frames")
    for key in expected_keys:
        if manifest.get(key) != spec.get(key):
            raise ValueError(f"Manifest differs from task: {key}")
    if manifest["frame_count"] != 18 or manifest["size"] != [256, 256]:
        raise ValueError("This verifier expects the actual 18-frame 256x256 demo contract.")
    names = manifest["frames"]
    if not isinstance(names, list) or len(names) != 18 or len(set(names)) != 18:
        raise ValueError("The manifest must contain exactly 18 distinct frame filenames.")
    source = artifact_path(art, manifest["source"])
    source_info = blend_header(source)
    frames = []
    for relative in names:
        info = inspect_png(artifact_path(art, relative), (256, 256))
        info["relative_path"] = relative
        frames.append(info)
    unique_rgba = len({frame["decoded_rgba_sha256"] for frame in frames})
    unique_alpha = len({frame["decoded_alpha_sha256"] for frame in frames})
    if unique_rgba < 3:
        raise ValueError("Fewer than three distinct decoded frames: an animation was not established.")
    middle_indices = list(range(len(frames) // 3, 2 * len(frames) // 3))
    if any(frames[index]["visible_pixels"] == 0 for index in middle_indices):
        raise ValueError("At least one middle animation frame has no visible content.")
    return {"automated_pixel_checks": "pass", "job": str(job),
            "verified_scope": "Actual PNG decode/CRC, RGBA alpha/corners, middle-frame content, distinct decoded pixels, task/manifest equality and Blender source header.",
            "limits": "Does not establish aesthetic quality, motion quality, Blender scene parameters, or licensing.",
            "frame_count": len(frames), "size": [256, 256],
            "unique_decoded_rgba_frames": unique_rgba, "unique_decoded_alpha_frames": unique_alpha,
            "middle_visible_frame_numbers": [index + 1 for index in middle_indices],
            "source": source_info, "frames": frames}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("--report", type=Path, default=Path(__file__).parent / "vfx_pixel_report.json")
    args = parser.parse_args()
    try:
        result = verify(args.job)
        code = 0
    except Exception as exc:
        result = {"automated_pixel_checks": "fail", "error": str(exc), "job": str(args.job.resolve())}
        code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if code:
        print("REAL VFX PIXEL VERIFICATION FAILED: " + result["error"])
    else:
        print(f"REAL VFX PIXEL VERIFICATION PASS: {result['frame_count']} actual RGBA frames, "
              f"{result['unique_decoded_rgba_frames']} distinct decoded frames, transparent background and visible middle frames.")
    print("Report: " + str(args.report.resolve()))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
