#!/usr/bin/env python3
"""构建前应用自主档最小改造。纯标准库，找不到预期字符串立即失败，不静默跳过。

用法: python3 apply_autonomous.py <上游源码目录> <owner/repo> [--with-data-extraction] [--telemetry-default-off] [--single-market-source] [--lazy-relay-login] [--forum-default-empty]

选项:
  --with-data-extraction    可选：补 Android 12+ 备份排除规则（默认不启用）。
  --telemetry-default-off   可选：把匿名统计开关默认值改为关闭（opt-in）。
  --single-market-source    可选：插件市场索引收敛为单一来源（默认多源并列）。
  --lazy-relay-login        可选：远程登录方式改为按需获取。
  --forum-default-empty     可选：论坛地址默认留空（需要时在设置里填，仍可用）。

依据（均为在上游 ccch1mneyyy/dsh-Go 源码逐行核实过的原文）:
  1 app/lib/update/update_config.dart   const updateRepo = 'ccch1mneyyy/dsh-go';
  2 tooling/release_assets.py           REPO = "ccch1mneyyy/dsh-go"
  3 app/android/app/src/main/AndroidManifest.xml  android:allowBackup="false"
  4 app/lib/usage/usage_stats.dart      this.enabled = true / prefs.getBool(enabledKey) ?? true
  5 app/lib/state/plugins/plugin_market.dart  fetchOfficialMarket() 签名行 + 首行（市场源收敛 patch）
  6 app/lib/state/hosts_state.dart     if (user == null) return base.copyWith(providers: await _loginProviders(backend));
  7 app/lib/forum/forum_config.dart    const forumDefaultBaseUrl = 'https://forum.dshtui.com';
第 3、4、5、6、7 项默认不启用。第 4、5、6、7 项是精确字符串替换，上游一改即 sys.exit 硬失败，不会静默。

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
        sys.exit("用法: apply_autonomous.py <src-dir> <owner/repo> [--with-data-extraction] [--telemetry-default-off] [--single-market-source] [--lazy-relay-login] [--forum-default-empty]")
    root = pathlib.Path(args[0])
    repo = args[1]
    opts = args[2:]
    with_dex = False
    telemetry_off = False
    single_market = False
    lazy_relay = False
    forum_empty = False
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

    # 2b 可选：匿名统计默认关闭（opt-in）。
    #     上游默认 opt-out：构造默认 enabled=true，且 _load 读取时 `?? true`。
    #     两处都要改成 false，否则：只改构造 → 老用户 prefs 无键时 _load 仍回 true；
    #     只改 _load → 首次运行 build() 的初始状态仍是 enabled=true。
    #     用户此前手动关过（prefs 里 enabled=false）不受影响，仍是关；
    #     从未设置过的用户从默认开变成默认关，需主动进设置打开才开始统计。
    #     两处均为精确字符串，上游一改即 sys.exit 硬失败，不会静默失效。
    if telemetry_off:
        replace(
            root / "app/lib/usage/usage_stats.dart",
            "const UsageStatsState({this.loaded = false, this.enabled = true, this.noticed = false});",
            "const UsageStatsState({this.loaded = false, this.enabled = false, this.noticed = false});",
        )
        replace(
            root / "app/lib/usage/usage_stats.dart",
            "final enabled = prefs.getBool(enabledKey) ?? true;",
            "final enabled = prefs.getBool(enabledKey) ?? false;",
        )

    # 2c 可选：插件市场索引统一为单一来源。
    #     上游默认并列多个索引，条目口径可能重叠；统一后列表更一致可复现。
    #     做法：在 fetchOfficialMarket() 首行插入提前 return 空目录；合并后即为
    #     单一来源列表。常量、其它 provider 与调用方都不动。锚点是函数签名 + 首行。
    if single_market:
        replace(
            root / "app/lib/state/plugins/plugin_market.dart",
            "Future<MarketCatalog> fetchOfficialMarket({MarketFileFetcher get = fetchMarketFile, bool refresh = false}) async {\n"
            "  final copy = await _readOfficialCopy();",
            "Future<MarketCatalog> fetchOfficialMarket({MarketFileFetcher get = fetchMarketFile, bool refresh = false}) async {\n"
            "  // 统一使用单一索引源。\n"
            "  return MarketCatalog(MarketIndex(url: Uri.parse(officialIndexUrl), title: LocalizedText.plain('插件市场')));\n"
            "  final copy = await _readOfficialCopy();",
        )

    # 2d 可选：远程登录方式改为按需获取（进入登录页时才取）。
    #     做法：RelayAccountController.build() 不再预取 providers，留空后登录页
    #     走通用登录按钮分支；各主动入口不依赖这份预取。锚点为单行，
    #     上游一改即 sys.exit 硬失败。
    if lazy_relay:
        replace(
            root / "app/lib/state/hosts_state.dart",
            "    if (user == null) return base.copyWith(providers: await _loginProviders(backend));",
            "    // 登录方式按需获取。\n"
            "    if (user == null) return base;",
        )

    # 2e 可选：论坛地址默认留空。
    #     上游 forumDefaultBaseUrl 是可调窗口：forum_state.dart 里 _load 会用
    #     用户自定义地址覆盖它，set() 写入自定义地址；默认值改成空串后，未设置的
    #     用户发不出请求，而在设置里填了自定义地址的用户照旧可用（逻辑不变）。
    #     锚点是单行，上游一改即 sys.exit 硬失败，不会静默失效。
    if forum_empty:
        replace(
            root / "app/lib/forum/forum_config.dart",
            "const forumDefaultBaseUrl = 'https://forum.dshtui.com';",
            "const forumDefaultBaseUrl = '';",
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

    print("自主档改造完成，目标仓库:", repo,
          "| 桌宠为整目录覆盖，无源码 patch",
          "| 遥测默认关:", "是" if telemetry_off else "否",
          "| 市场索引单源:", "是" if single_market else "否",
          "| 远程登录按需:", "是" if lazy_relay else "否",
          "| 论坛默认留空:", "是" if forum_empty else "否")


if __name__ == "__main__":
    main()
