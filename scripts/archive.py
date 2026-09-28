#!/usr/bin/env python3
"""Scan, plan and create restartable split-ZIP sample-library backups."""

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MANIFEST = "sample_archive_manifest.json"
IGNORED = {".DS_Store"}
PRINT_LOCK = threading.Lock()


def die(message):
    raise ValueError(message)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def is_within(child, parent):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def roots(source, destination):
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_dir():
        die("Source is not a directory: " + str(source))
    destination = Path(destination).expanduser().resolve(strict=False)
    if is_within(destination, source) or is_within(source, destination):
        die("Source and destination must be separate, non-nested directories")
    return source, destination


def rel_path(value):
    if not isinstance(value, str) or not value or value.startswith("/"):
        die("Rule path must be a nonempty relative path")
    path = Path(value)
    if any(part in ("..", "") for part in path.parts):
        die("Rule path may not escape the source")
    return path


def safe_component(value):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    clean = (clean or "library")[:72].rstrip(" .")
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return clean + "__" + suffix


def scan_tree(source, depth):
    rows = []

    def visit(folder, level):
        entries = sorted(os.scandir(folder), key=lambda item: item.name.casefold())
        direct_files = [item.name for item in entries if item.is_file(follow_symlinks=False) and item.name not in IGNORED]
        links = [item.name for item in entries if item.is_symlink()]
        directories = [item for item in entries if item.is_dir(follow_symlinks=False)]
        rows.append({"path": str(folder.relative_to(source)) or ".", "level": level,
                     "directories": len(directories), "files": len(direct_files),
                     "file_examples": direct_files[:8], "symlinks": links[:8]})
        if level < depth:
            for item in directories:
                visit(Path(item.path), level + 1)

    visit(source, 0)
    return rows


def enumerate_units(source, rules):
    if not isinstance(rules, dict) or not isinstance(rules.get("rules"), list) or not rules["rules"]:
        die("Rules JSON needs a nonempty rules array")
    policy = rules.get("symlinks", "reject")
    if policy not in ("reject", "preserve"):
        die("symlinks must be reject or preserve")
    units = []
    for rule in rules["rules"]:
        prefix = rel_path(rule["path"])
        level = rule["unit_depth"]
        if type(level) is not int or level < 0 or level > 8:
            die("unit_depth must be an integer from 0 to 8")
        root = source if str(prefix) == "." else source / prefix
        if not root.is_dir() or root.is_symlink():
            die("Rule subtree is not an ordinary directory: " + str(root))
        selected = [root]
        for _ in range(level):
            selected = [Path(item.path) for folder in selected for item in os.scandir(folder)
                        if item.is_dir(follow_symlinks=False)]
        if not selected:
            die("Rule finds no library directories: " + str(prefix))
        units.extend(selected)
    keys = [str(path.relative_to(source)) for path in units]
    if len(set(keys)) != len(keys):
        die("Overlapping rules select a library twice")
    for index, path in enumerate(units):
        if any(index != other and is_within(path, candidate) for other, candidate in enumerate(units)):
            die("Overlapping rules select nested libraries")

    unit_set = set(units)
    uncovered = []

    def check(folder):
        if folder in unit_set:
            return
        entries = list(os.scandir(folder))
        if not entries and folder != source:
            uncovered.append(str(folder.relative_to(source)))
        for item in entries:
            if item.name in IGNORED:
                continue
            item_path = Path(item.path)
            if item.is_dir(follow_symlinks=False):
                check(item_path)
            else:
                uncovered.append(str(item_path.relative_to(source)))

    check(source)
    if uncovered:
        die("Rules leave source entries uncovered: " + json.dumps(uncovered[:20], ensure_ascii=False))
    return sorted(units, key=lambda path: str(path.relative_to(source)).casefold()), policy


def fingerprint(folder, policy):
    digest = hashlib.sha256()
    byte_count = 0
    file_count = 0
    link_count = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        files.sort()
        base = Path(current)
        relative_dir = base.relative_to(folder)
        for name in dirs + files:
            if name in IGNORED:
                continue
            path = base / name
            relative = str(relative_dir / name)
            meta = path.lstat()
            if path.is_symlink():
                if policy == "reject":
                    die("Symlink needs a preserve/reject decision: " + str(path))
                entry = ["L", relative, os.readlink(path)]
                link_count += 1
            elif path.is_dir():
                entry = ["D", relative]
            elif path.is_file():
                entry = ["F", relative, meta.st_size, meta.st_mtime_ns]
                byte_count += meta.st_size
                file_count += 1
            else:
                die("Unsupported special file: " + str(path))
            digest.update(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
    return {"fingerprint": digest.hexdigest(), "source_bytes": byte_count,
            "files": file_count, "symlinks": link_count}


def part_names(folder):
    return sorted(item.name for item in folder.iterdir()
                  if item.is_file() and re.fullmatch(r"backup\.(?:zip|z\d{2,})", item.name))


def manifest_for(destination, source):
    path = destination / MANIFEST
    if path.exists():
        manifest = read_json(path)
        if manifest.get("schema") != 1 or manifest.get("source_root") != str(source):
            die("Existing manifest belongs to a different source or schema")
        return manifest
    legacy = (destination / "ARCHIVE_LOG.tsv").exists()
    return {"schema": 1, "source_root": str(source), "created_at": utc_now(),
            "legacy_import_complete": not legacy, "entries": {}}


def entry_status(manifest, unit, destination):
    versions = manifest["entries"].get(unit["source_relpath"], [])
    for entry in versions:
        if entry["fingerprint"] == unit["fingerprint"] and entry.get("verified"):
            folder = destination / entry["package_relpath"]
            names = entry.get("parts", [])
            if not names or any(not (folder / name).is_file() for name in names):
                return "missing"
            return "skip"
    return "changed" if versions else "new"


def build_plan(source, destination, rules):
    units, policy = enumerate_units(source, rules)
    manifest = manifest_for(destination, source)
    records = []
    for folder in units:
        relative = folder.relative_to(source)
        stats = fingerprint(folder, policy)
        record = {"source_relpath": str(relative), **stats}
        record["status"] = entry_status(manifest, record, destination)
        records.append(record)
    counts = {status: sum(item["status"] == status for item in records)
              for status in ("new", "changed", "skip", "missing")}
    pending_bytes = sum(item["source_bytes"] for item in records if item["status"] in ("new", "changed"))
    legacy = not manifest.get("legacy_import_complete", True)
    return {"schema": 1, "source_root": str(source), "destination_root": str(destination),
            "created_at": utc_now(), "rules": rules, "units": records, "counts": counts,
            "pending_source_bytes": pending_bytes, "legacy_archive_detected": legacy,
            "split_size": "8g", "default_jobs": 5, "fingerprint_kind": "metadata"}


def verify_archive(path):
    result = subprocess.run(["7zz", "t", "-bd", "-bb0", "-mmt=off", str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        die("7zz verification failed for " + str(path) + ": " + result.stderr[-1000:])


def snapshot_paths(destination, unit):
    relative = Path(unit["source_relpath"])
    parent = relative.parent
    group = Path(*(safe_component(part) for part in parent.parts)) if str(parent) != "." else Path()
    local_day = dt.datetime.now().strftime("%Y-%m-%d")
    key = hashlib.sha256(str(relative).encode("utf-8")).hexdigest()[:10]
    name = safe_component(relative.name) + "__" + key + "__" + local_day + "__" + unit["fingerprint"][:12]
    final = destination / "packages" / group / name
    stage = destination / ".staging" / name
    return stage, final


def worker(unit, source, destination, policy, retry_incomplete):
    source_folder = source / unit["source_relpath"]
    if fingerprint(source_folder, policy)["fingerprint"] != unit["fingerprint"]:
        die("Source changed since plan: " + unit["source_relpath"])
    stage, final = snapshot_paths(destination, unit)
    package_meta = {"source_relpath": unit["source_relpath"], "fingerprint": unit["fingerprint"]}
    if final.exists():
        if read_json(final / "unit.json") != package_meta:
            die("Existing package path conflicts with plan: " + str(final))
        verify_archive(final / "backup.zip")
    else:
        if stage.exists():
            if not (stage / "unit.json").is_file() or read_json(stage / "unit.json") != package_meta:
                die("Existing staging folder does not match plan: " + str(stage))
            try:
                verify_archive(stage / "backup.zip")
            except (ValueError, OSError):
                if not retry_incomplete:
                    die("Incomplete staging package needs inspection (then --retry-incomplete): " + str(stage))
                failed = destination / ".failed" / (stage.name + "__" + dt.datetime.now().strftime("%Y%m%d%H%M%S%f"))
                failed.parent.mkdir(parents=True, exist_ok=True)
                os.rename(stage, failed)
        if not stage.exists():
            stage.parent.mkdir(parents=True, exist_ok=True)
            stage.mkdir()
            atomic_json(stage / "unit.json", package_meta)
            archive = stage / "backup.zip"
            argument = "./" + source_folder.name
            command = ["zip", "-q", "-r", "-1", "-y", "-s", "8g", str(archive), argument,
                       "-x", "*/.DS_Store"]
            result = subprocess.run(command, cwd=str(source_folder.parent), stderr=subprocess.PIPE, text=True)
            if result.returncode:
                die("zip failed for " + unit["source_relpath"] + ": " + result.stderr[-1000:])
            verify_archive(archive)
        if fingerprint(source_folder, policy)["fingerprint"] != unit["fingerprint"]:
            die("Source changed during compression; staged package kept: " + unit["source_relpath"])
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(stage, final)
    names = part_names(final)
    if "backup.zip" not in names:
        die("Package has no ZIP entry: " + str(final))
    return {"fingerprint": unit["fingerprint"], "source_bytes": unit["source_bytes"],
            "source_files": unit["files"], "package_relpath": str(final.relative_to(destination)),
            "parts": names, "zip_entry": "backup.zip",
            "archive_bytes": sum((final / name).stat().st_size for name in names),
            "verified": True, "verified_at": utc_now()}


def archive_listing(zip_path):
    result = subprocess.run(["7zz", "l", "-slt", str(zip_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        die("Cannot list legacy archive: " + str(zip_path) + " " + result.stderr[-500:])
    marker = "\n----------\n"
    if marker not in result.stdout:
        die("Unexpected 7zz listing format: " + str(zip_path))
    entries = {}
    for block in result.stdout.split(marker, 1)[1].strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                fields[key] = value
        if "Path" in fields and "Folder" in fields:
            entries[fields["Path"]] = fields
    if not entries:
        die("Legacy archive has no listed entries: " + str(zip_path))
    return entries


def crc32_file(path):
    value = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            value = zlib.crc32(chunk, value)
    return "%08X" % (value & 0xffffffff)


def compare_legacy_archive(zip_path, source_folder, policy):
    if policy != "reject":
        die("Legacy import cannot prove symlink equivalence; use a fresh archive")
    before = fingerprint(source_folder, policy)
    verify_archive(zip_path)
    entries = archive_listing(zip_path)
    root = source_folder.name
    archived_files = {}
    archived_dirs = set()
    for name, fields in entries.items():
        if name == root or name == root + "/":
            continue
        if not name.startswith(root + "/"):
            die("Legacy ZIP contains an unexpected root: " + name)
        relative = name[len(root) + 1:].rstrip("/")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            die("Unsafe legacy ZIP path: " + name)
        if Path(relative).name in IGNORED:
            continue
        if fields["Folder"] == "+":
            archived_dirs.add(relative)
        else:
            archived_files[relative] = fields
    source_files = {}
    source_dirs = set()
    for current, dirs, files in os.walk(source_folder, followlinks=False):
        for name in dirs:
            if name not in IGNORED:
                source_dirs.add(str((Path(current) / name).relative_to(source_folder)))
        for name in files:
            if name not in IGNORED:
                path = Path(current) / name
                source_files[str(path.relative_to(source_folder))] = path
    if set(source_files) != set(archived_files) or source_dirs != archived_dirs:
        die("Legacy ZIP file/directory inventory differs from current source")
    for relative, path in source_files.items():
        fields = archived_files[relative]
        if path.stat().st_size != int(fields["Size"]) or crc32_file(path) != fields.get("CRC", "").upper():
            die("Legacy ZIP content differs from current source: " + relative)
    if fingerprint(source_folder, policy) != before:
        die("Source changed during legacy comparison")
    return before


def adopt_legacy(plan_path):
    plan = read_json(plan_path)
    source, destination = roots(plan["source_root"], plan["destination_root"])
    log_path = destination / "ARCHIVE_LOG.tsv"
    if not log_path.is_file():
        die("No legacy ARCHIVE_LOG.tsv in destination")
    if not destination.is_dir():
        die("Destination directory does not exist")
    with open(destination / ".sample_archive.lock", "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die("Another archive run holds the destination lock")
        manifest = manifest_for(destination, source)
        if manifest.get("legacy_import_complete"):
            die("Legacy import already completed")
        units, policy = enumerate_units(source, plan["rules"])
        planned = {item["source_relpath"]: item for item in plan["units"]}
        if {str(item.relative_to(source)) for item in units} != set(planned):
            die("Source layout changed since plan")
        rows = {}
        with open(log_path, "r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                relative = str(Path(row["category"]) / row["library"])
                if relative in rows:
                    die("Duplicate legacy log record: " + relative)
                rows[relative] = row
        unmatched = []
        imported = 0
        for relative, unit in planned.items():
            if relative not in rows:
                unmatched.append({"library": relative, "reason": "not in legacy log"})
                continue
            if entry_status(manifest, unit, destination) == "skip":
                continue
            row = rows[relative]
            try:
                zip_path = Path(row["archive_path"]).resolve(strict=True)
                if not is_within(zip_path, destination) or zip_path.suffix.lower() != ".zip":
                    die("Legacy ZIP path is outside destination or invalid")
                current = compare_legacy_archive(zip_path, source / relative, policy)
                if current != {key: unit[key] for key in current}:
                    die("Source changed since plan")
                sibling_parts = sorted(item.name for item in zip_path.parent.iterdir()
                                       if item.name == zip_path.name or
                                       re.fullmatch(re.escape(zip_path.stem) + r"\.z\d{2,}", item.name))
                entry = {"fingerprint": unit["fingerprint"], "source_bytes": unit["source_bytes"],
                         "source_files": unit["files"], "package_relpath": str(zip_path.parent.relative_to(destination)),
                         "zip_entry": zip_path.name, "parts": sibling_parts,
                         "archive_bytes": sum((zip_path.parent / name).stat().st_size for name in sibling_parts),
                         "verified": True, "verified_at": utc_now(), "legacy": True,
                         "original_archive_date": row.get("date")}
                manifest["entries"].setdefault(relative, []).append(entry)
                atomic_json(destination / MANIFEST, manifest)
                imported += 1
                print("IMPORTED", imported, relative, flush=True)
            except (ValueError, OSError, KeyError) as error:
                unmatched.append({"library": relative, "reason": str(error)})
                print("UNMATCHED", relative, str(error), file=sys.stderr, flush=True)
        for relative in rows.keys() - planned.keys():
            unmatched.append({"library": relative, "reason": "not in current source plan"})
        manifest["legacy_unmatched"] = unmatched
        manifest["legacy_import_complete"] = True
        manifest["legacy_import_finished_at"] = utc_now()
        atomic_json(destination / MANIFEST, manifest)
        print(json.dumps({"imported": imported, "unmatched": unmatched}, ensure_ascii=False, indent=2))
        return 0


def run_plan(plan_path, jobs, retry_incomplete):
    plan = read_json(plan_path)
    source, destination = roots(plan["source_root"], plan["destination_root"])
    if not destination.is_dir():
        die("Destination directory does not exist: " + str(destination))
    if plan.get("legacy_archive_detected") or ((destination / "ARCHIVE_LOG.tsv").exists() and not (destination / MANIFEST).exists()):
        die("Legacy archive detected without a compatible manifest; review/import it before a new run")
    lock_path = destination / ".sample_archive.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die("Another archive run holds the destination lock")
        manifest = manifest_for(destination, source)
        if not manifest.get("legacy_import_complete", True):
            die("Legacy import is incomplete")
        units, policy = enumerate_units(source, plan["rules"])
        if {str(item.relative_to(source)) for item in units} != {item["source_relpath"] for item in plan["units"]}:
            die("Library layout changed since plan; scan and plan again")
        pending = []
        skipped = 0
        for unit in plan["units"]:
            current = fingerprint(source / unit["source_relpath"], policy)
            if current != {key: unit[key] for key in ("fingerprint", "source_bytes", "files", "symlinks")}:
                die("Source changed since plan: " + unit["source_relpath"])
            state = entry_status(manifest, unit, destination)
            if state == "missing":
                die("Previously verified package is missing parts: " + unit["source_relpath"])
            if state == "skip":
                skipped += 1
            else:
                pending.append(unit)
        required = sum(unit["source_bytes"] for unit in pending)
        available = shutil.disk_usage(destination).free
        reserve = max(4 * 1024**3, required // 20)
        if required + reserve > available:
            die("Insufficient destination space for conservative uncompressed estimate: need " +
                str(required + reserve) + ", free " + str(available))
        completed = 0
        errors = []
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(worker, unit, source, destination, policy, retry_incomplete): unit
                       for unit in pending}
            for future in as_completed(futures):
                unit = futures[future]
                try:
                    entry = future.result()
                    versions = manifest["entries"].setdefault(unit["source_relpath"], [])
                    versions.append(entry)
                    atomic_json(destination / MANIFEST, manifest)
                    completed += 1
                    with PRINT_LOCK:
                        print("VERIFIED", completed, "/", len(pending), unit["source_relpath"], flush=True)
                except Exception as error:
                    errors.append({"library": unit["source_relpath"], "error": str(error)})
                    with PRINT_LOCK:
                        print("FAILED", unit["source_relpath"], str(error), file=sys.stderr, flush=True)
        print(json.dumps({"completed": completed, "skipped": skipped, "failed": errors,
                          "total": len(plan["units"])}, ensure_ascii=False, indent=2))
        if errors:
            return 2
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="read-only source structure inspection")
    scan.add_argument("--source", required=True)
    scan.add_argument("--depth", type=int, default=3)
    plan = sub.add_parser("plan", help="make a metadata-based backup plan")
    plan.add_argument("--source", required=True)
    plan.add_argument("--destination", required=True)
    plan.add_argument("--rules", required=True)
    plan.add_argument("--output", required=True)
    run = sub.add_parser("run", help="execute an approved plan")
    run.add_argument("--plan", required=True)
    run.add_argument("--jobs", type=int, default=5)
    run.add_argument("--retry-incomplete", action="store_true")
    adopt = sub.add_parser("adopt-legacy", help="verify old ZIP contents against source and import matching libraries")
    adopt.add_argument("--plan", required=True)
    status = sub.add_parser("status", help="show manifest counts without reading archive data")
    status.add_argument("--destination", required=True)
    args = parser.parse_args()
    if args.command == "scan":
        source = Path(args.source).expanduser().resolve(strict=True)
        if args.depth < 0 or args.depth > 5:
            die("Scan depth must be between 0 and 5")
        print(json.dumps({"source": str(source), "tree": scan_tree(source, args.depth)}, ensure_ascii=False, indent=2))
    elif args.command == "plan":
        source, destination = roots(args.source, args.destination)
        result = build_plan(source, destination, read_json(args.rules))
        output = Path(args.output).expanduser().resolve(strict=False)
        atomic_json(output, result)
        print(json.dumps({key: value for key, value in result.items() if key != "units"}, ensure_ascii=False, indent=2))
        print("Plan file:", output)
    elif args.command == "run":
        if args.jobs < 1 or args.jobs > 5:
            die("jobs must be between 1 and 5")
        return run_plan(args.plan, args.jobs, args.retry_incomplete)
    elif args.command == "adopt-legacy":
        return adopt_legacy(args.plan)
    else:
        destination = Path(args.destination).expanduser().resolve(strict=True)
        manifest = read_json(destination / MANIFEST)
        versions = [entry for group in manifest["entries"].values() for entry in group]
        missing = [entry["package_relpath"] for entry in versions
                   if any(not (destination / entry["package_relpath"] / name).is_file()
                          for name in entry["parts"])]
        print(json.dumps({"libraries": len(manifest["entries"]), "versions": len(versions),
                          "missing_packages": missing}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
