# -*- coding: utf-8 -*-
"""打包 audiobook_srt：确定性 zip + 形状校验。

用法：
    python scripts/build.py                 # 清理 → 校验 → 打包 → 打印 sha256
    python scripts/build.py --out dist      # 指定输出目录
    python scripts/build.py --no-validate   # 跳过源码校验（仍做产物形状校验）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(REPO_ROOT, "dist")

ROOT_FILES = ("manifest.json", "icon.png", "LICENSE", "NOTICE.md")
TREE_DIRS = ("backend", "frontend")
REQUIRED_ENTRIES = (
    "manifest.json",
    "icon.png",
    "backend/__init__.py",
    "backend/tool.py",
    "backend/worker.py",
    "backend/requirements.lock",
    "frontend/index.html",
    "frontend/app.js",
    "frontend/lib/theme.css",
    "frontend/lib/i18n.js",
    "frontend/locales/zh.json",
    "frontend/locales/en.json",
    "frontend/locales/zh-TW.json",
)
FORBIDDEN_PARTS = ("__pycache__", ".pyc", ".pyo", ".DS_Store", ".pytest_cache", ".git")


def load_manifest():
    with open(os.path.join(REPO_ROOT, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def fixed_date_time(manifest):
    raw = str(manifest.get("publish_date") or "").strip()
    parts = raw.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return (int(parts[0]), int(parts[1]), int(parts[2]), 0, 0, 0)
    return (1980, 1, 1, 0, 0, 0)


def clean_bytecode():
    removed = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        if ".git" in dirpath or os.sep + "dist" in dirpath:
            continue
        for name in list(dirnames):
            if name in ("__pycache__", ".pytest_cache"):
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    print("清理字节码/缓存：移除 %d 项" % removed)


def collect_entries():
    entries = []
    for name in ROOT_FILES:
        if os.path.isfile(os.path.join(REPO_ROOT, name)):
            entries.append(name)
    for top in TREE_DIRS:
        base = os.path.join(REPO_ROOT, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".pytest_cache"))
            for name in sorted(filenames):
                if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                    continue
                full = os.path.join(dirpath, name)
                entries.append(os.path.relpath(full, REPO_ROOT).replace(os.sep, "/"))
    return sorted(set(entries))


def validate_source():
    errors = []
    try:
        manifest = load_manifest()
    except (OSError, ValueError) as err:
        return ["manifest.json 读取失败：%s" % err]

    for key in ("tool_id", "name", "description", "revision", "author",
                "core_api_version", "entry_backend", "entry_frontend", "repo_url"):
        if not manifest.get(key):
            errors.append("manifest.json 缺字段：%s" % key)
    if not re.match(r"^[a-z0-9_]+$", str(manifest.get("tool_id") or "")):
        errors.append("tool_id 只能是小写字母/数字/下划线")

    for relative in REQUIRED_ENTRIES:
        if not os.path.exists(os.path.join(REPO_ROOT, relative)):
            errors.append("缺文件：%s" % relative)

    entry = str(manifest.get("entry_backend") or "")
    if "." in entry:
        module_name, class_name = entry.rsplit(".", 1)
        module_path = os.path.join(REPO_ROOT, "backend", module_name + ".py")
        if not os.path.exists(module_path):
            errors.append("entry_backend 指向不存在的模块：%s" % module_path)
        elif not re.search(r"^class\s+%s\b" % re.escape(class_name),
                           open(module_path, encoding="utf-8").read(), re.M):
            errors.append("entry_backend 的类不存在：%s" % class_name)
    else:
        errors.append("entry_backend 格式应为 <module>.<ClassName>")

    tool_source_path = os.path.join(REPO_ROOT, "backend", "tool.py")
    if os.path.exists(tool_source_path):
        tool_source = open(tool_source_path, encoding="utf-8").read()
        for route in manifest.get("api_routes") or []:
            handler = str(route.get("handler") or "")
            if "." not in handler:
                errors.append("api_routes.handler 格式不合法：%s" % handler)
                continue
            class_name = handler.rsplit(".", 1)[1]
            if not re.search(r"^class\s+%s\b" % re.escape(class_name), tool_source, re.M):
                errors.append("api_routes 指向不存在的 handler：%s" % class_name)

    frontend = str(manifest.get("entry_frontend") or "")
    if frontend and not os.path.exists(os.path.join(REPO_ROOT, "frontend", frontend)):
        errors.append("entry_frontend 指向不存在的文件：%s" % frontend)
    return errors


def validate_zip(entries):
    errors = []
    names = set(entries)
    for required in REQUIRED_ENTRIES:
        if required not in names:
            errors.append("产物缺文件：%s" % required)
    for name in entries:
        if any(part in name for part in FORBIDDEN_PARTS):
            errors.append("产物包含禁用路径：%s" % name)
        if name.startswith("/") or ".." in name.split("/"):
            errors.append("产物包含非法路径：%s" % name)
    return errors


def frontend_build_id(entries):
    digest = hashlib.sha256()
    for relative in entries:
        if not relative.startswith("frontend/"):
            continue
        digest.update(relative.encode("utf-8"))
        with open(os.path.join(REPO_ROOT, relative), "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()[:12]


def build_zip(out_dir, manifest=None):
    manifest = manifest or load_manifest()
    entries = collect_entries()
    errors = validate_zip(entries)
    if errors:
        raise SystemExit("产物校验失败：\n- " + "\n- ".join(errors))

    os.makedirs(out_dir, exist_ok=True)
    name = "%s-%s.zip" % (manifest["tool_id"], manifest["revision"])
    out_path = os.path.join(out_dir, name)
    date_time = fixed_date_time(manifest)
    build_id = frontend_build_id(entries)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in entries:
            full = os.path.join(REPO_ROOT, relative)
            info = zipfile.ZipInfo(relative, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            info.create_system = 3
            with open(full, "rb") as handle:
                data = handle.read()
            if relative.startswith("frontend/"):
                data = data.replace(b"__BUILD_ID__", build_id.encode("ascii"))
            archive.writestr(info, data)
    digest = hashlib.sha256(open(out_path, "rb").read()).hexdigest()
    return out_path, digest, entries


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DIST_DIR)
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args(argv)

    clean_bytecode()
    if not args.no_validate:
        errors = validate_source()
        if errors:
            print("源码校验失败：")
            for error in errors:
                print(" - %s" % error)
            return 1
        print("源码校验通过")
    out_path, digest, entries = build_zip(args.out)
    print("写出 %s" % out_path)
    print("条目 %d 个，大小 %.1f KB" % (len(entries), os.path.getsize(out_path) / 1024.0))
    print("sha256 %s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
