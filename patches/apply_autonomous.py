#!/usr/bin/env python3
"""构建前应用自主档最小改造。纯标准库，锚点不符立即失败，不静默跳过。

用法: python3 apply_autonomous.py <source-dir> <owner/repo> [--with-data-extraction] [--telemetry-default-off] [--single-market-source] [--lazy-relay-login] [--forum-default-empty] [--dry-run]

选项:
  --with-data-extraction    可选：补 Android 12+ 备份排除规则（默认不启用）。
  --telemetry-default-off   可选：把匿名统计开关默认值改为关闭（opt-in）。
  --single-market-source    可选：插件市场索引收敛为单一来源（默认多源并列）。
  --lazy-relay-login        可选：远程登录方式改为按需获取。
  --forum-default-empty     可选：论坛地址默认留空（需要时在设置里填，仍可用）。
  --recall-turn-id          可选：插件菜单 scope 补回合号，供消息尾注定位。
  --dry-run                 可选：只校验所有锚点，不写入任何文件。

锚点分两类，由 old 字符串的前缀区分：
  - 无前缀：精确字面量匹配，适合结构固定、全文唯一的短串或多行块。
  - "re:" 前缀：其后是正则表达式，匹配结构而非具体值。
new 同理：
  - 无前缀：纯文本替换（含反斜杠也不被当转义，走 lambda）。
  - "re:" 前缀：re.sub 替换模板，支持 \\g<1> 等捕获组引用。
两类匹配都要求"恰好 N 处"，计数不符立即 sys.exit。

两阶段提交：先在 preflight() 里只读校验全部锚点，全部通过后才逐个写入。
任一锚点不符即在写入任何文件前退出，不留半改状态；--dry-run 跑完校验后停止。

read_text() 用通用换行（CRLF→LF），write 统一写 LF。
"""
import pathlib
import re
import sys

RE_PREFIX = "re:"


def _compile(old):
    """返回 (is_regex, pattern_or_literal)。"""
    if old.startswith(RE_PREFIX):
        return True, old[len(RE_PREFIX):]
    return False, old


def _count(text, old):
    """统计锚点在文本中命中的次数。"""
    is_re, pat = _compile(old)
    if is_re:
        return len(re.findall(pat, text, flags=re.MULTILINE)), pat, is_re
    return text.count(old), old, is_re


def _sub(text, old, new):
    """执行替换；正则锚点下 new 可带 're:' 模板前缀。"""
    is_re, pat = _compile(old)
    if is_re:
        if new.startswith(RE_PREFIX):
            return re.sub(pat, new[len(RE_PREFIX):], text, flags=re.MULTILINE)
        return re.sub(pat, lambda _m: new, text, flags=re.MULTILINE)
    return text.replace(old, new)


def replace(path, old, new, count=1):
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    found, _pat, _is_re = _count(text, old)
    if found != count:
        sys.exit("{}: 预期 {} 处，实得 {} 处: {!r}".format(path, count, found, old[:70]))
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(_sub(text, old, new))
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


def build_plan(root, repo, with_dex, telemetry_off, single_market, lazy_relay, forum_empty, recall_turn_id):
    """返回补丁计划 [(rel, old, new)] 与新建文件计划 [(rel, content)]。"""
    plan = []
    newfiles = []

    # 更新源仓库改为编译期可注入（默认值指向本仓库，构建时由 dart-define 覆盖）。
    # 正则只认 `const updateRepo = '...';` 这一行结构，仓库名任意。
    plan.append((
        "app/lib/update/update_config.dart",
        r"re:^const updateRepo = '[^']+';",
        "const updateRepo = String.fromEnvironment('DSH_UPDATE_REPO', defaultValue: '{}');".format(repo),
    ))

    # 发布脚本的仓库常量改为环境变量（脚本已 import os）。正则只认 `REPO = "..."` 结构。
    plan.append((
        "tooling/release_assets.py",
        r're:^REPO = "[^"]+"',
        'REPO = os.environ.get("DSH_RELEASE_REPO", "{}")'.format(repo),
    ))

    if telemetry_off:
        # 匿名统计默认关闭：构造默认值与读取默认值两处都要改。
        # 正则只改 this.enabled 的默认值与 ?? 后的值，容忍参数顺序调整或中间新增参数。
        plan.append((
            "app/lib/usage/usage_stats.dart",
            r"re:(const UsageStatsState\([^)\n]*\bthis\.enabled = )true([\s,)])",
            r"re:\g<1>false\g<2>",
        ))
        plan.append((
            "app/lib/usage/usage_stats.dart",
            r"re:(final enabled = prefs\.getBool\(enabledKey\)\s*\?\?\s*)true(;)" ,
            r"re:\g<1>false\g<2>",
        ))

    if single_market:
        # 插件市场索引收敛：地址与基址置空，throw failure! 换成返回空目录，
        # 并堵住两处从缓存复活官方目录的分支。URL/列表按结构匹配，避免值变化即漂移。
        plan.append((
            "app/lib/state/plugins/plugin_market.dart",
            r"re:^const officialIndexUrl = '[^']*';",
            "const officialIndexUrl = '';",
        ))
        plan.append((
            "app/lib/state/plugins/plugin_market.dart",
            r"re:^const officialMarketBases = \[[^\]]*\];",
            "const officialMarketBases = <String>[];",
        ))
        # 全文唯一的短串，保留精确字面量：计数为 1 即硬失败，不会误配注释。
        plan.append((
            "app/lib/state/plugins/plugin_market.dart",
            "  throw failure!;",
            "  return MarketCatalog(MarketIndex(url: Uri.parse(officialIndexUrl), title: LocalizedText.plain('插件市场')));",
        ))
        # 缓存未过期分支：多行块按结构匹配，用捕获组保留 if 头与闭合括号，只替换中间 return 行。
        plan.append((
            "app/lib/state/plugins/plugin_market.dart",
            r"re:(^  if \(copy != null && !refresh && DateTime\.now\(\)\.difference\(copy\.at\) < officialMarketFresh\) \{\n)    return _officialCatalog\([^;]*\);(\n  \})",
            r"re:\g<1>    return MarketCatalog(MarketIndex(url: Uri.parse(officialIndexUrl), title: LocalizedText.plain('插件市场')));\g<2>",
        ))
        # 离线回落分支：整行按结构匹配。
        plan.append((
            "app/lib/state/plugins/plugin_market.dart",
            r"re:^  if \(copy != null\) return _officialCatalog\([^;]*\);",
            "  if (copy != null) return MarketCatalog(MarketIndex(url: Uri.parse(officialIndexUrl), title: LocalizedText.plain('插件市场')));",
        ))

    if lazy_relay:
        # 远程登录方式改为按需获取：去掉预取与登出后刷新两处。
        plan.append((
            "app/lib/state/hosts_state.dart",
            r"re:^    if \(user == null\) return base\.copyWith\(providers: await _loginProviders\([^)]*\)\);",
            "    // 登录方式按需获取。\n    if (user == null) return base;",
        ))
        plan.append((
            "app/lib/state/hosts_state.dart",
            r"re:^    final providers = await _loginProviders\([^)]*\);",
            "    final providers = const <RelayAuthProvider>[];",
        ))

    if forum_empty:
        plan.append((
            "app/lib/forum/forum_config.dart",
            r"re:^const forumDefaultBaseUrl = '[^']*';",
            "const forumDefaultBaseUrl = '';",
        ))

    if recall_turn_id:
        # 插件菜单 scope 补 turnId：从 AssistantChatItem.rowKey (m<turn>.<step>) 解析回合号，
        # 让撤回尾注能贴到对应回复。精确串在文件内唯一，计数不符即失败。
        plan.append((
            "app/lib/ui/plugins/native/message_plugin_menu.dart",
            "  final target = DshGoScope(sessionId: sessionId, messageId: messageId);",
            "  final turnId = item.rowKey == null\n"
            "      ? null\n"
            "      : RegExp(r'^m(\\d+)\\.').firstMatch(item.rowKey!)?.group(1);\n"
            "  final target = DshGoScope(sessionId: sessionId, messageId: messageId, turnId: turnId);",
        ))

    if with_dex:
        plan.append((
            "app/android/app/src/main/AndroidManifest.xml",
            'android:allowBackup="false"',
            'android:allowBackup="false"\n        android:dataExtractionRules="@xml/data_extraction_rules"',
        ))
        newfiles.append((
            "app/android/app/src/main/res/xml/data_extraction_rules.xml",
            DATA_EXTRACTION_RULES,
        ))

    return plan, newfiles


def preflight(root, plan, newfiles):
    """只读校验全部锚点，任一不符立即退出；在任何写入之前完成。"""
    for rel, old, _ in plan:
        p = root / rel
        if not p.is_file():
            sys.exit("{}: 文件不存在".format(p))
        found, _pat, _is_re = _count(p.read_text(encoding="utf-8"), old)
        if found != 1:
            sys.exit("{}: 预期 1 处，实得 {} 处: {!r}".format(p, found, old[:70]))
    for rel, _ in newfiles:
        if (root / rel).exists():
            sys.exit("{}: 已存在，拒绝覆盖".format(root / rel))


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("用法: apply_autonomous.py <src-dir> <owner/repo> [--with-data-extraction] [--telemetry-default-off] [--single-market-source] [--lazy-relay-login] [--forum-default-empty] [--recall-turn-id] [--dry-run]")
    root = pathlib.Path(args[0])
    repo = args[1]
    opts = args[2:]
    with_dex = False
    telemetry_off = False
    single_market = False
    lazy_relay = False
    forum_empty = False
    recall_turn_id = False
    dry_run = False
    for o in opts:
        if o == "--with-data-extraction":
            with_dex = True
        elif o == "--telemetry-default-off":
            telemetry_off = True
        elif o == "--single-market-source":
            single_market = True
        elif o == "--lazy-relay-login":
            lazy_relay = True
        elif o == "--forum-default-empty":
            forum_empty = True
        elif o == "--recall-turn-id":
            recall_turn_id = True
        elif o == "--dry-run":
            dry_run = True
        else:
            sys.exit("未知参数: {!r}".format(o))

    plan, newfiles = build_plan(root, repo, with_dex, telemetry_off, single_market, lazy_relay, forum_empty, recall_turn_id)
    preflight(root, plan, newfiles)
    if dry_run:
        print("dry-run: 全部 {} 项补丁锚点 + {} 个新文件校验通过，未写入。".format(len(plan), len(newfiles)))
        return

    for rel, old, new in plan:
        replace(root / rel, old, new)
    for rel, content in newfiles:
        write_new(root / rel, content)

    print("自主档改造完成，目标仓库:", repo,
          "| 桌宠为整目录覆盖，无源码 patch",
          "| 遥测默认关:", "是" if telemetry_off else "否",
          "| 市场索引单源:", "是" if single_market else "否",
          "| 远程登录按需:", "是" if lazy_relay else "否",
          "| 论坛默认留空:", "是" if forum_empty else "否",
          "| 菜单补回合号:", "是" if recall_turn_id else "否")


if __name__ == "__main__":
    main()
