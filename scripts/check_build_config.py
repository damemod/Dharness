#!/usr/bin/env python3
"""构建配置检查器：只读确认上游源码里我们依赖的编译期配置点仍然存在。

背景
----
本仓库的构建依赖上游源码中的若干编译期配置点（dart-define 名、
编译期可注入的仓库常量）以及构建前补丁插入的若干标记。上游发版如果
改了这些名字或语义，我们的构建参数就不再生效，而构建与发布都会照常
成功、不会报错。本检查器专门盯这一类配置漂移。

检查对象分两类：
  (1) apply_autonomous.py 的精确字符串补丁：这类失效时 replace() 计数
      不符会 sys.exit，本来就会中断构建，这里只做一次复核。
  (2) 传给 flutter build 的 dart-define 名与空串语义：这类失效是静默的，
      需要本检查器显式发现。

设计约束
--------
  - 纯标准库；只读；不改文件；不联网；不 import 任何上游模块。
  - 在 checkout 之后运行，不影响拉取动作本身。
  - 默认告警模式：任一检查失败只打印 [WARN]，exit 0，发布照常。
    --strict：任一失败打印 [FAIL] 并 exit 1，中断构建。

用法
----
  python3 check_build_config.py <上游源码目录> [--strict]
"""
from __future__ import annotations

import pathlib
import sys

# (相对路径, 必须存在的子串, 说明)。只锚定 define 名与关键语义，
# 不锚定默认值字面量（默认值会变，define 名才是契约）。
GUARDS: list[tuple[str, str, str]] = [
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_MIRROR'",
     "更新镜像 define 仍被读取"),
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_MIRRORS'",
     "公共镜像 define 仍被读取"),
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_FEED'",
     "feed define 仍被读取"),
    ("app/lib/state/workshop_dependencies.dart",
     "String.fromEnvironment('DSH_WORKSHOP_ORIGIN'",
     "工坊地址 define 仍被读取"),
    ("app/lib/update/update_config.dart",
     "origin.isEmpty ? null : UpdateMirror.selfHosted(origin)",
     "空镜像地址的解析语义仍在"),
    ("app/lib/update/update_config.dart",
     "final public = accelerators == null",
     "空镜像列表的解析语义仍在"),
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_REPO'",
     "更新仓库常量为编译期可注入"),
    ("app/lib/state/plugins/plugin_market.dart",
     "LocalizedText.plain('插件市场')",
     "市场索引配置补丁仍在"),
    ("app/lib/state/hosts_state.dart",
     "登录方式按需获取",
     "远程登录配置补丁仍在"),
    ("app/lib/forum/forum_config.dart",
     "const forumDefaultBaseUrl = '';",
     "论坛默认地址配置补丁仍在"),
]


def main() -> None:
    args = sys.argv[1:]
    strict = "--strict" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        sys.exit("用法: check_build_config.py <上游源码目录> [--strict]")
    root = pathlib.Path(paths[0])

    failed = 0
    print("=== 构建配置检查（只读，不影响拉取）===")
    for rel, needle, desc in GUARDS:
        f = root / rel
        if not f.is_file():
            print(f"[{'FAIL' if strict else 'WARN'}] 缺少文件 {rel} —— {desc}")
            failed += 1
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        ok = needle in text
        tag = "OK  " if ok else ("FAIL" if strict else "WARN")
        print(f"[{tag}] {desc}")
        if not ok:
            print(f"       预期子串未命中（{rel}）: {needle!r}")
            failed += 1

    if failed:
        mode = "硬失败" if strict else "告警"
        print(f"\n结论：{failed} 项检查未通过（{mode}模式）。"
              + (" 已按 --strict 中断。" if strict else " 已告警，不阻断发布。"))
        sys.exit(1 if strict else 0)
    print("\n结论：构建配置检查项全部命中，未见配置漂移。")


if __name__ == "__main__":
    main()
