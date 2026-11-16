#!/usr/bin/env python3
"""路线 B 自持发布脚本：只消费已签名 APK，生成 dist/ 发布资产。

设计约束（与上游 tooling/release_assets.py 的关键区别）：
  - 不 import 任何上游模块（apk_delta / check_apk_jni / build_number）；
  - 不执行任何上游代码；只读一个已签名 APK + 少量环境变量；
  - 只用 Python 标准库 + 系统 aapt2（Android SDK 的标准工具，非上游代码）。

产物与上游 release_assets.py --delta-from 0 完全同构，App 端
app/lib/update/release.dart parseUpdateManifest 可直接解析：
  dist/dsh-go-<versionName>-arm64.apk
  dist/SHA256SUMS          ("<sha256>  <asset>" 两空格)
  dist/update.json         (schema 1)

用法（在签名 job 内）：
  SELF_REPO=damemod/Dharness TAG=v1.1.0 \
  python3 scripts/self_signed_release.py \
      --apk signed.apk --out dist
可选：
  --version-name / --version-code  签名 job 从构建 artifact 的 version.json 传入；
                                  同时传这两个值时完全跳过 aapt2（可信区无需 Android SDK）
  --aapt2 /path/to/aapt2           仅当未同时给出版本号时才需要，默认从 PATH/ANDROID_HOME 找
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

BADGING_RE = re.compile(
    r"^package: .*?\bversionCode='(\d*)' versionName='([^']*)'", re.MULTILINE
)
ASSET_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")


def find_aapt2(explicit: str | None) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(f"aapt2 不存在: {explicit}")
        return explicit
    found = shutil.which("aapt2")
    if found:
        return found
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(key)
        if not sdk:
            continue
        tools = sorted(
            pathlib.Path(sdk).glob("build-tools/*/aapt2"),
            key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.parent.name)),
        )
        if tools:
            return str(tools[-1])
    sys.exit("找不到 aapt2：请用 --aapt2 指定，或把 build-tools 加入 PATH / 设置 ANDROID_HOME")


def apk_version(aapt2: str, apk: pathlib.Path) -> tuple[str, int]:
    out = subprocess.run(
        [aapt2, "dump", "badging", str(apk)],
        capture_output=True, text=True,
    ).stdout
    match = BADGING_RE.search(out)
    if not match:
        sys.exit(f"aapt2 无法从 {apk} 读取 versionCode/versionName；确认已签名且文件完整")
    return match.group(2), int(match.group(1) or 0)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apk", required=True, help="已签名的 arm64 APK 路径")
    p.add_argument("--out", required=True, help="dist 输出目录")
    p.add_argument("--aapt2", default=None)
    p.add_argument("--version-name", default=None, help="覆盖 APK 读出的 versionName")
    p.add_argument("--version-code", type=int, default=None, help="覆盖 APK 读出的 versionCode")
    p.add_argument("--notes", default="", help="update.json 的 notes 文本")
    args = p.parse_args()

    repo = os.environ.get("SELF_REPO", "")
    tag = os.environ.get("TAG", "")
    if not repo or "/" not in repo:
        sys.exit("必须设置 SELF_REPO=<owner>/<repo>")
    if not tag or not tag.startswith("v"):
        sys.exit("必须设置 TAG=vX.Y.Z（上游版本 tag）")

    apk = pathlib.Path(args.apk).resolve()
    if not apk.is_file():
        sys.exit(f"APK 不存在: {apk}")
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # 同时传入 version-name 与 version-code 时，不读 APK、不需要 aapt2。
    # 这样签名 job 完全不依赖 Android SDK / 上游任何工具，只需标准库。
    if args.version_name and args.version_code is not None:
        name, build = args.version_name, args.version_code
    else:
        aapt2 = find_aapt2(args.aapt2)
        apk_name, apk_code = apk_version(aapt2, apk)
        name = args.version_name or apk_name
        build = args.version_code if args.version_code is not None else apk_code
    if not re.match(r"^\d+\.\d+\.\d+", name):
        sys.exit(f"versionName 格式异常: {name!r}")

    asset = f"dsh-go-{name}-arm64.apk"
    if not ASSET_NAME_RE.match(asset):
        sys.exit(f"资产文件名不合法: {asset}")
    target = out / asset
    shutil.copyfile(apk, target)

    digest = sha256(target)
    size = target.stat().st_size
    entry = {"name": asset, "size": size, "sha256": digest}

    sums = f"{digest}  {asset}\n"
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")

    manifest = {
        "schema": 1,
        "version": f"{name}+{build}",
        "tag": tag,
        "title": f"Dharness {name}",
        "publishedAt": datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "notes": args.notes.strip(),
        "page": f"https://github.com/{repo}/releases/tag/{tag}",
        # url 省略：App 端 update_config.dart githubReleaseAsset(tag, name) 会从
        # 已 patch 成 SELF_REPO 的 updateRepo 拼出本仓库下载地址，无需自托管。
        "assets": {"arm64": entry},
    }
    (out / "update.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )

    print(f"自持发布资产已生成: {out}")
    print(f"  {asset}  size={size}  sha256={digest}")
    print(f"  version={manifest['version']}  tag={tag}  page={manifest['page']}")
    print("  （未执行任何上游代码；未请求网络）")


if __name__ == "__main__":
    main()
