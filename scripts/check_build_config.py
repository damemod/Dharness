#!/usr/bin/env python3
"""构建配置检查器：只读确认上游源码里我们依赖的编译期配置点与补丁标记仍在。

背景
----
本仓库的构建依赖上游源码中的若干编译期配置点（dart-define 名、
编译期可注入的仓库常量）以及构建前补丁插入/移除的若干标记。上游发版如果
改了这些名字或语义，我们的构建参数就不再生效，而构建与发布都会照常
成功、不会报错。本检查器专门盯这一类配置漂移。

检查对象分三类：
  (1) GUARDS    —— 必须存在的子串（define 名与关键语义、补丁后的实现逻辑）；
  (2) COUNTS    —— 必须恰好出现 N 次的子串（多处补丁要确认每一处都在）；
  (3) FORBIDDEN —— 必须已消失的子串（被替换掉的旧实现逻辑）。

每个补丁点尽量同时锚定"新逻辑在"与"旧逻辑不在"两侧，避免只查注释或单个
字面量造成的单点检测：注释可以留在而逻辑被改回，字面量也可能凑巧命中。
其中 dart-define 名与空串语义的失效是静默的，需要本检查器显式发现；
精确字符串补丁失效时 replace() 计数不符本来就会中断构建，这里只做复核。

设计约束
--------
  - 纯标准库；只读；不改文件；不联网；不 import 任何上游模块。
  - 在 checkout、打补丁之后运行，不影响拉取动作本身。
  - 默认告警模式：任一检查失败只打印 [WARN]，exit 0，发布照常。
    --strict：任一失败打印 [FAIL] 并 exit 1，中断构建。

用法
----
  python3 check_build_config.py <上游源码目录> [--strict]
"""
from __future__ import annotations

import pathlib
import sys

# (相对路径, 必须存在的子串, 说明)。锚定 define 名与补丁后的实现逻辑，
# 不锚定默认值字面量（默认值会变，define 名与逻辑才是契约），也不锚定注释。
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
    ("tooling/release_assets.py",
     'os.environ.get("DSH_RELEASE_REPO"',
     "发布仓库常量为环境变量可注入"),
    ("app/lib/usage/usage_stats.dart",
     "this.enabled = false",
     "遥测构造默认值已改为关"),
    ("app/lib/usage/usage_stats.dart",
     "prefs.getBool(enabledKey) ?? false",
     "遥测读取默认值已改为关"),
    ("app/lib/state/plugins/plugin_market.dart",
     "const officialIndexUrl = '';",
     "市场索引地址补丁仍在"),
    ("app/lib/state/plugins/plugin_market.dart",
     "const officialMarketBases = <String>[];",
     "市场索引基址补丁仍在"),
    ("app/lib/state/hosts_state.dart",
     "if (user == null) return base;",
     "登录方式改为按需获取（后台预取已移除）"),
    ("app/lib/state/hosts_state.dart",
     "final providers = const <RelayAuthProvider>[];",
     "登出后不再主动获取登录方式"),
    ("app/lib/forum/forum_config.dart",
     "const forumDefaultBaseUrl = '';",
     "论坛默认地址补丁仍在"),
    # 以下为上游安全默认值哨兵：这些文件未打补丁，仅监控默认值是否被改松。
    ("app/android/app/src/main/kotlin/io/github/ccch1mneyyy/dsh_app/phone/SafetyGate.kt",
     "val confirmMode: ConfirmMode = ConfirmMode.ASK",
     "高危操作默认每次询问（ask）"),
    ("app/android/app/src/main/kotlin/io/github/ccch1mneyyy/dsh_app/phone/SafetyGate.kt",
     "val unlocks: PhoneUnlocks = PhoneUnlocks()",
     "解除限制默认全关"),
    ("app/android/app/src/main/kotlin/io/github/ccch1mneyyy/dsh_app/phone/shell/AndroidShellController.kt",
     'preferences.getBoolean("enabled", false)',
     "Android Shell 默认关闭"),
    ("app/lib/phone/phone_channel.dart",
     "this.confirmMode = PhoneConfirmMode.ask",
     "Dart 侧确认模式默认 ask"),
]

# (相对路径, 子串, 期望次数, 说明)。确认多处补丁每一处都在。
COUNTS: list[tuple[str, str, int, str]] = [
    ("app/lib/state/plugins/plugin_market.dart",
     "title: LocalizedText.plain('插件市场')",
     3,
     "市场三处（异常出口+两处缓存分支）均返回中性空目录"),
]

# (相对路径, 禁止子串, 说明)。补丁后这些旧逻辑必须已消失：只查新逻辑
# 可能被留存的注释或凑巧的字面量蒙混，配合 GUARDS 构成双侧锚定。
FORBIDDEN: list[tuple[str, str, str]] = [
    ("app/lib/state/plugins/plugin_market.dart",
     "return _officialCatalog(copy.index",
     "市场旧缓存复活分支已全部堵住"),
    ("app/lib/state/plugins/plugin_market.dart",
     "officialIndexUrl = 'https://",
     "市场索引地址旧值未残留"),
    ("app/lib/state/hosts_state.dart",
     "return base.copyWith(providers: await _loginProviders(backend))",
     "后台预取登录方式的旧逻辑已移除"),
    ("app/lib/state/hosts_state.dart",
     "final providers = await _loginProviders(backend);",
     "登出后主动获取登录方式的旧逻辑已移除"),
    ("app/lib/usage/usage_stats.dart",
     "prefs.getBool(enabledKey) ?? true",
     "遥测读取默认值旧逻辑未残留"),
    ("app/lib/forum/forum_config.dart",
     "forumDefaultBaseUrl = 'https://",
     "论坛默认地址旧值未残留"),
]


def _read(root: pathlib.Path, rel: str) -> str | None:
    f = root / rel
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None


def main() -> None:
    args = sys.argv[1:]
    strict = "--strict" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        sys.exit("用法: check_build_config.py <上游源码目录> [--strict]")
    root = pathlib.Path(paths[0])

    tag_fail = "FAIL" if strict else "WARN"
    failed = 0

    def report(ok: bool, desc: str, detail: str = "") -> None:
        nonlocal failed
        tag = "OK  " if ok else tag_fail
        print(f"[{tag}] {desc}")
        if not ok:
            if detail:
                print(f"       {detail}")
            failed += 1

    print("=== 构建配置检查（只读，不影响拉取）===")

    for rel, needle, desc in GUARDS:
        text = _read(root, rel)
        if text is None:
            report(False, desc, f"缺少文件 {rel}")
            continue
        report(needle in text, desc, f"预期子串未命中（{rel}）: {needle!r}")

    for rel, needle, want, desc in COUNTS:
        text = _read(root, rel)
        if text is None:
            report(False, desc, f"缺少文件 {rel}")
            continue
        got = text.count(needle)
        report(got == want, desc, f"{rel}: 预期 {want} 处，实得 {got} 处: {needle!r}")

    for rel, needle, desc in FORBIDDEN:
        text = _read(root, rel)
        if text is None:
            report(False, desc, f"缺少文件 {rel}")
            continue
        got = text.count(needle)
        report(got == 0, desc, f"{rel}: 残留 {got} 处不该存在的分支: {needle!r}")

    if failed:
        mode = "硬失败" if strict else "告警"
        print(f"\n结论：{failed} 项检查未通过（{mode}模式）。"
              + (" 已按 --strict 中断。" if strict else " 已告警，不阻断发布。"))
        sys.exit(1 if strict else 0)
    print("\n结论：构建配置检查项全部命中，未见配置漂移。")


if __name__ == "__main__":
    main()
