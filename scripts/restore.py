#!/usr/bin/env python3
"""Portable recovery helper copied beside a sample-library backup manifest."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

MANIFEST = "sample_archive_manifest.json"


def fail(message):
    raise ValueError(message)


def relative_parts(value, allow_dot=False):
    if not isinstance(value, str) or not value or "\\" in value:
        fail("Invalid manifest path: " + repr(value))
    if value == "." and allow_dot:
        return ()
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts) or value == ".":
        fail("Unsafe manifest path: " + value)
    return path.parts


def basename(value):
    parts = relative_parts(value)
    if len(parts) != 1 or parts[0] in (".", ".."):
        fail("Expected a single filename: " + repr(value))
    return parts[0]


def inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_entries(backup_root, only):
    with open(backup_root / MANIFEST, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema") != 1 or not isinstance(manifest.get("entries"), dict):
        fail("Unsupported or missing backup manifest")
    wanted = set(only or manifest["entries"])
    missing = wanted - set(manifest["entries"])
    if missing:
        fail("Unknown library path(s): " + ", ".join(sorted(missing)))
    selected = []
    for relpath in sorted(wanted):
        parts = relative_parts(relpath, allow_dot=True)
        versions = [item for item in manifest["entries"][relpath] if item.get("verified")]
        if not versions:
            fail("No verified version: " + relpath)
        entry = max(enumerate(versions), key=lambda pair: (pair[1]["verified_at"], pair[0]))[1]
        package = backup_root.joinpath(*relative_parts(entry["package_relpath"], allow_dot=True))
        if not inside(package, backup_root):
            fail("Package escapes backup directory: " + relpath)
        names = [basename(name) for name in entry["parts"]]
        zip_entry = basename(entry["zip_entry"])
        if zip_entry not in names or len(set(names)) != len(names):
            fail("Invalid ZIP entry or duplicate parts: " + relpath)
        selected.append({"relpath": relpath, "parts": parts, "entry": entry,
                         "package": package, "names": names, "zip": package / zip_entry})
    return manifest, selected


def sevenzip_path(value):
    found = shutil.which(value)
    if found:
        return found
    if value == "7zz":
        found = shutil.which("7z")
        if found:
            return found
    fail("7-Zip executable not found; install 7zz/7z or pass --sevenzip PATH")


def check_archive_paths(sevenzip, zip_file, expected_root):
    result = subprocess.run([sevenzip, "l", "-slt", str(zip_file)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode or "\n----------\n" not in result.stdout:
        fail("Cannot list ZIP entries: " + str(zip_file) + " " + result.stderr[-500:])
    found = False
    for block in result.stdout.split("\n----------\n", 1)[1].split("\n\n"):
        name = next((line[7:] for line in block.splitlines() if line.startswith("Path = ")), None)
        if name is None:
            continue
        found = True
        if "\\" in name:
            fail("ZIP contains a backslash path: " + name)
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected_root:
            fail("ZIP contains a path outside its library folder: " + name)
    if not found:
        fail("ZIP has no listed entries: " + str(zip_file))


def check_extracted(folder, entry):
    files = 0
    size = 0
    for current, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            if path.is_symlink():
                fail("Restored library contains a symlink; inspect manually: " + str(path))
            if path.is_file():
                files += 1
                size += path.stat().st_size
    if files != entry["source_files"] or size != entry["source_bytes"]:
        fail("Restored file count or byte count differs from manifest")


def restore_one(item, target_root, source_name, sevenzip):
    relpath = item["relpath"]
    expected_root = item["parts"][-1] if item["parts"] else source_name
    target = target_root.joinpath(*item["parts"]) if item["parts"] else target_root
    if target.exists() or target.is_symlink():
        fail("Target library already exists; refusing overwrite: " + str(target))
    for name in item["names"]:
        if not (item["package"] / name).is_file():
            fail("Missing ZIP part: " + str(item["package"] / name))
    check_archive_paths(sevenzip, item["zip"], expected_root)
    tested = subprocess.run([sevenzip, "t", "-bd", "-bb0", str(item["zip"])],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if tested.returncode:
        fail("7-Zip integrity test failed: " + relpath + " " + tested.stderr[-500:])
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".sample-restore-", dir=str(parent)))
    try:
        extracted = subprocess.run([sevenzip, "x", "-y", "-o" + str(stage), str(item["zip"])],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if extracted.returncode:
            fail("7-Zip extraction failed: " + relpath + " " + extracted.stderr[-500:])
        root = stage / expected_root
        if not root.is_dir() or root.is_symlink() or [child.name for child in stage.iterdir()] != [expected_root]:
            fail("Extracted structure does not match expected library root: " + relpath)
        check_extracted(root, item["entry"])
        if target.exists() or target.is_symlink():
            fail("Target appeared during restore; refusing overwrite: " + str(target))
        os.rename(root, target)
        stage.rmdir()
    except Exception as error:
        fail("Restore stopped (" + str(error) + "); inspect retained staging directory: " + str(stage))
    print("RESTORED", relpath, "->", target, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list latest verified archive for each library")
    restore = sub.add_parser("restore", help="verify and restore libraries under a new root")
    restore.add_argument("--target-root", required=True, help="replacement for the original source root")
    restore.add_argument("--only", action="append", help="restore one relative library path; repeatable")
    restore.add_argument("--sevenzip", default="7zz", help="7zz/7z executable or absolute path")
    args = parser.parse_args()
    backup_root = Path(__file__).resolve().parent
    manifest, items = load_entries(backup_root, getattr(args, "only", None))
    if args.command == "list":
        print(json.dumps({"original_source_root": manifest.get("source_root"),
                          "libraries": [{"source_relpath": item["relpath"],
                                         "archive": str(item["zip"].relative_to(backup_root)),
                                         "parts": item["names"],
                                         "verified_at": item["entry"]["verified_at"]}
                                        for item in items]}, ensure_ascii=False, indent=2))
        return
    target_root = Path(args.target_root).expanduser().resolve(strict=False)
    if inside(target_root, backup_root) or inside(backup_root, target_root):
        fail("Restore target must be separate from the backup directory")
    source_name = Path(manifest["source_root"]).name
    targets = [target_root.joinpath(*item["parts"]) for item in items]
    for path in targets:
        if not inside(path, target_root):
            fail("Library path escapes restore root: " + str(path))
    if len({str(path).casefold() for path in targets}) != len(targets):
        fail("Library paths collide on a case-insensitive target filesystem")
    for index, path in enumerate(targets):
        if any(index != other and (inside(path, candidate) or inside(candidate, path))
               for other, candidate in enumerate(targets)):
            fail("Selected library paths overlap; restore individually after inspection")
    for path in targets:
        if path.exists() or path.is_symlink():
            fail("Target library already exists; refusing overwrite: " + str(path))
    sevenzip = sevenzip_path(args.sevenzip)
    for item in items:
        restore_one(item, target_root, source_name, sevenzip)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
