#!/usr/bin/env python3
"""去三方链接哨兵：只读检查上游源码里「无声失效」的去三方机制是否仍然有效。

为什么需要它
------------
自主档去掉第三方链接靠两类机制：

  (1) apply_autonomous.py 的精确字符串 patch
      （update_config.dart 的 updateRepo、release_assets.py 的 REPO，
      以及市场索引单源模式下的 plugin_market.dart 提前返回）。
      这类失效时 replace() 计数不符会 sys.exit —— 是「响亮的硬失败」，
      本来就会中断构建提醒你，过去无需额外检测。

  (2) workflow 传给 flutter build 的 dart-define：
      DSH_UPDATE_REPO / DSH_UPDATE_MIRROR / DSH_UPDATE_MIRRORS /
      DSH_UPDATE_FEED / DSH_WORKSHOP_ORIGIN。
      这类是「无声失效」：上游若改了 define 名、改了默认值语义，
      我们传的空串不再生效，dl.dshtui.com / ghfast.top / gh-proxy.com /
      go.dshtui.com 又会回到产物里，而构建和发布都会照常成功。
      本脚本只盯这一类。

设计约束
--------
  - 纯标准库；只读；不改任何文件；不联网；不 import 任何上游模块。
  - 只读扫描 checkout 下来的上游源码，放在 checkout 之后运行，
    因此不会影响「拉取上游」这个动作本身。
  - 默认告警模式：任一检查失败只打印 [WARN]，exit 0 —— 更新照常发布。
    --strict：任一检查失败打印 [FAIL] 并 exit 1 —— 硬失败，中断本次构建。

用法
----
  python3 check_thirdparty_guards.py <上游源码目录> [--strict]
"""
from __future__ import annotations

import pathlib
import re
import sys

# (相对路径, 必须存在的子串, 说明)。子串取得尽量短且稳定，
# 只锚定 define 名与关键语义，不锚定默认值字面量（默认值会变，define 名才是契约）。
GUARDS: list[tuple[str, str, str]] = [
    # dart-define 名仍被上游读取——我们传的空串才有可能生效
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_MIRROR'",
     "更新镜像 define 仍有效（关掉 dl.dshtui.com 靠它）"),
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_MIRRORS'",
     "公共加速器 define 仍有效（关掉 ghfast/gh-proxy 靠它）"),
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_FEED'",
     "feed define 仍有效"),
    ("app/lib/state/workshop_dependencies.dart",
     "String.fromEnvironment('DSH_WORKSHOP_ORIGIN'",
     "工坊地址 define 仍有效（关掉 go.dshtui.com 工坊靠它）"),
    # 空串语义仍在：mirror='' -> 无自有镜像；accelerators='' -> 无公共加速器
    ("app/lib/update/update_config.dart",
     "origin.isEmpty ? null : UpdateMirror.selfHosted(origin)",
     "空 mirror 仍解析为「无自有镜像」"),
    ("app/lib/update/update_config.dart",
     "final public = accelerators == null",
     "空加速器列表语义仍在（传空串不再回落公共镜像）"),
    # 纵深防御：确认 apply_autonomous.py 的源码 patch 确实生效
    ("app/lib/update/update_config.dart",
     "String.fromEnvironment('DSH_UPDATE_REPO'",
     "updateRepo 已被改成编译期可注入（否则 dart-define 被忽略）"),
    ("app/lib/state/plugins/plugin_market.dart",
     "LocalizedText.plain('插件市场')",
     "市场索引已收敛为单一来源（fetchOfficialMarket 提前返回空目录）"),
    ("app/lib/state/hosts_state.dart",
     "登录方式按需获取",
     "远程登录方式为按需获取（不在后台无差别预检）"),
    ("app/lib/forum/forum_config.dart",
     "const forumDefaultBaseUrl = '';",
     "论坛地址默认留空（需要时可在设置里填）"),
]

# 已知允许出现的第三方主机（注释、文档或用户主动功能），仅作信息提示，不判失败。
KNOWN_THIRD_PARTY_RE = re.compile(r"https://[A-Za-z0-9.-]*(?:dshtui\.com|ghfast\.top|gh-proxy\.com)")


def main() -> None:
    args = sys.argv[1:]
    strict = "--strict" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        sys.exit("用法: check_thirdparty_guards.py <上游源码目录> [--strict]")
    root = pathlib.Path(paths[0])

    failed = 0
    print("=== 去三方链接哨兵（只读，不影响拉取）===")
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

    # 信息段：列出上游 app/lib 活跃代码里仍硬编码的第三方主机，供你知情。
    # 只做提示，不判失败——它们多是「用户主动才连」的功能或已知残留。
    app_lib = root / "app/lib"
    hits: dict[str, set[str]] = {}
    if app_lib.is_dir():
        for p in app_lib.rglob("*.dart"):
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                s = line.strip()
                if s.startswith("//") or s.startswith("*"):
                    continue  # 跳过纯注释行，减少噪声
                for m in KNOWN_THIRD_PARTY_RE.finditer(line):
                    hits.setdefault(m.group(0), set()).add(f"{p.relative_to(root)}:{i}")
    if hits:
        print("=== 上游活跃代码中仍存在的第三方主机（信息提示，未判失败）===")
        for host, locs in sorted(hits.items()):
            sample = ", ".join(sorted(locs)[:3])
            more = " ..." if len(locs) > 3 else ""
            print(f"  {host}  ({len(locs)} 处，如 {sample}{more})")

    if failed:
        mode = "硬失败" if strict else "告警"
        print(f"\n结论：{failed} 项检查未通过（{mode}模式）。"
              + (" 已按 --strict 中断。" if strict else " 已告警，不阻断发布。"))
        sys.exit(1 if strict else 0)
    print("\n结论：去三方链接机制全部命中，未见无声失效。")


if __name__ == "__main__":
    main()
