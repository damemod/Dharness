#!/usr/bin/env python3
"""构建前应用自主档最小改造。纯标准库，找不到预期字符串立即失败，不静默跳过。

用法: python3 apply_autonomous.py <上游源码目录> <owner/repo> [--with-data-extraction]

选项:
  --with-data-extraction  可选：补 Android 12+ 备份排除规则（默认不启用）。

依据（均为在上游 ccch1mneyyy/dsh-Go 源码逐行核实过的原文）:
  1 app/lib/update/update_config.dart   const updateRepo = 'ccch1mneyyy/dsh-go';
  2 tooling/release_assets.py           REPO = "ccch1mneyyy/dsh-go"
  3 app/android/app/src/main/AndroidManifest.xml  android:allowBackup="false"
第 3 项默认不启用，启用前需核对目标环境对该规则的接受情况。

设计取舍：
  「换自有美术素材」不改任何 Dart 源码，只在工作流里整目录覆盖 app/assets/。
  桌宠保留 deepy-whale / whale-girl 两个目录名，各自放一份自有合规 pack.json
  与 idle.png —— pubspec.yaml、pet_prefs.dart、pet_picker.dart、companion_chrome.dart
  以及全部 test/fixture 的引用全部原样工作，零源码 patch，不需要在此脚本里
  改动 builtInPets / defaultPetId / assets 清单。这样上游后续调整这些 .dart 时，
  本补丁也不依赖源码中的精确行匹配，无需随上游发版重做。
  运行时更新源与公共镜像由工作流里的 dart-define 统一配置
  （DSH_UPDATE_MIRROR= / DSH_UPDATE_MIRRORS= / DSH_UPDATE_FEED / DSH_UPDATE_REPO），
  这些是上游 update_config.dart 文件头自述的设计机制，不依赖本脚本改常量。

注意：read_text() 用通用换行（CRLF→LF），write 统一写 LF，故 Windows(CRLF) 与
Linux runner(LF) 检出都能命中同一份单行 old 字符串。
"""
import pathlib
import sys


def replace(path, old, new, count=1):
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        sys.exit("{}: 预期 {} 处，实得 {} 处: {!r}".format(path, count, found, old[:70]))
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace(old, new))
    print("patched", path)


def write_new(path, content):
    p = pathlib.Path(path)
    if p.exists():
        sys.exit("{}: 已存在，拒绝覆盖".format(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("created", path)


DATA_EXTRACTION_RULES = """<?xml version="1.0" encoding="utf-8"?>
<!-- 模板：Android 12+ 备份/转移排除项。语法请在目标环境核对后再启用。 -->
<data-extraction-rules>
  <cloud-backup>
    <exclude domain="file" path="runtime/rootfs" />
    <exclude domain="file" path="links" />
  </cloud-backup>
  <device-transfer>
    <exclude domain="file" path="runtime/rootfs" />
    <exclude domain="file" path="links" />
  </device-transfer>
</data-extraction-rules>
"""


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("用法: apply_autonomous.py <src-dir> <owner/repo> [--with-data-extraction]")
    root = pathlib.Path(args[0])
    repo = args[1]
    opts = args[2:]
    with_dex = False
    for o in opts:
        if o == "--with-data-extraction":
            with_dex = True
        else:
            sys.exit("未知参数: {!r}".format(o))

    # 1 更新源仓库改为编译期可注入（默认值指向本仓库，构建时由 dart-define 覆盖）
    #    这是唯一一处必须改的：上游把 updateRepo 写成 const 硬编码，dart-define 无法生效。
    replace(
        root / "app/lib/update/update_config.dart",
        "const updateRepo = 'ccch1mneyyy/dsh-go';",
        "const updateRepo = String.fromEnvironment('DSH_UPDATE_REPO', defaultValue: '{}');".format(repo),
    )

    # 2 发布脚本的仓库常量改为环境变量（脚本已 import os；release_assets.py 生成
    #    update.json 里的 GitHub 链接与 gh --repo 参数都用它）。
    #    兜底值也写成本库：万一 runner 漏传 DSH_RELEASE_REPO，
    #    发布链接也不会指向上游仓库。
    replace(
        root / "tooling/release_assets.py",
        'REPO = "ccch1mneyyy/dsh-go"',
        'REPO = os.environ.get("DSH_RELEASE_REPO", "{}")'.format(repo),
    )

    # 3 可选：补 dataExtractionRules（语法需核对，默认不启用）
    if with_dex:
        replace(
            root / "app/android/app/src/main/AndroidManifest.xml",
            'android:allowBackup="false"',
            'android:allowBackup="false"\n        android:dataExtractionRules="@xml/data_extraction_rules"',
        )
        write_new(
            root / "app/android/app/src/main/res/xml/data_extraction_rules.xml",
            DATA_EXTRACTION_RULES,
        )

    print("自主档改造完成，目标仓库:", repo, "（桌宠为整目录覆盖，无源码 patch）")


if __name__ == "__main__":
    main()
