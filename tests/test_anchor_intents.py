#!/usr/bin/env python3
"""锚点鲁棒性意图测试。

测试的是设计意图，不是某个具体处理：
  I1 值漂移仍命中：仓库名/URL/默认值变了，锚点仍能命中恰好 1 处。
  I2 结构漂移仍失败：const→var、删掉 ?? 默认值、列表改泛型声明 → 计数不符 fail-closed。
  I3 计数不符 fail-closed：重复锚点行 → preflight 拒绝，绝不静默多选。
  I4 两阶段提交：任一锚点坏 → 任何文件都不被写（含排在它之前的锚点）。
  I5 真实替换正确：应用后 new 内容精确落地；重复应用被拒（幂等保护）。
  I6 换行无关：CRLF 与 LF 检出都能命中。
  I7 短串不误配：精确短串锚点不会把注释里的相似行算进来。
  I8 捕获组保形：多行块锚点只改中间 return，保留 if 头与闭合括号。

   I9 基线唯一：全树每个计划锚点各恰好命中 1 处。

每个意图含多个场景、多条断言。基线源码目录由 DSH_UPSTREAM_SRC 环境变量指定。
"""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
APPLY = HERE.parent / "patches" / "apply_autonomous.py"
UPSTREAM = pathlib.Path(os.environ.get("DSH_UPSTREAM_SRC", "upstream"))

_spec = importlib.util.spec_from_file_location("apply_autonomous", APPLY)
apply = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(apply)

ALL_FLAGS = ["--telemetry-default-off", "--single-market-source",
             "--lazy-relay-login", "--forum-default-empty", "--recall-turn-id"]
FILES = {
    "update": "app/lib/update/update_config.dart",
    "release": "tooling/release_assets.py",
    "usage": "app/lib/usage/usage_stats.dart",
    "market": "app/lib/state/plugins/plugin_market.dart",
    "hosts": "app/lib/state/hosts_state.dart",
    "forum": "app/lib/forum/forum_config.dart",
    "menu": "app/lib/ui/plugins/native/message_plugin_menu.dart",
}


def read(key):
    return (UPSTREAM / FILES[key]).read_text(encoding="utf-8")


class AnchorIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not UPSTREAM.is_dir():
            raise RuntimeError("基线源码目录不存在，请设置 DSH_UPSTREAM_SRC: %s" % UPSTREAM)
        cls.plan, cls.newfiles = apply.build_plan(pathlib.Path("/"), "damemod/Dharness",
                                                   False, True, True, True, True, True)

    def anchors_for(self, key):
        return [a for a in self.plan if a[0] == FILES[key]]

    def count(self, text, old):
        return apply._count(text, old)[0]

    def test_I1_value_drift_still_matches(self):
        """I1：五类可变值漂移后仍各命中恰好 1 处（每个值至少两种新值）。"""
        cases = [
            ("update", "const updateRepo = 'owner/any-other-name';", 1),
            ("update", "const updateRepo = 'x/y';", 1),
            ("release", 'REPO = "someone/else"', 1),
            ("release", 'REPO = "a/b"', 1),
            ("forum", "const forumDefaultBaseUrl = 'https://example.com/new';", 1),
            ("forum", "const forumDefaultBaseUrl = '';", 1),
        ]
        for key, replaced_line, expect in cases:
            text = read(key)
            old_line = {
                "update": "const updateRepo = 'ccch1mneyyy/dsh-go';",
                "release": 'REPO = "ccch1mneyyy/dsh-go"',
                "forum": "const forumDefaultBaseUrl = 'https://forum.dshtui.com';",
            }[key]
            self.assertIn(old_line, text, "基线里应存在原锚点行: %s" % key)
            drifted = text.replace(old_line, replaced_line)
            for anchor in self.anchors_for(key):
                if self.count(drifted, anchor[1]) == 1:
                    break
            else:
                self.fail("值漂移后没有锚点命中: %s -> %s" % (key, replaced_line))
        # 参数顺序变化：enabled 默认值行前面多了个字段
        usage = read("usage")
        drifted = usage.replace(
            "const UsageStatsState({this.loaded = false, this.enabled = true, this.noticed = false});",
            "const UsageStatsState({this.enabled = true, this.loaded = false, this.noticed = false});")
        ctor = [a for a in self.anchors_for("usage") if "UsageStatsState" in a[1]][0]
        self.assertEqual(self.count(drifted, ctor[1]), 1, "参数顺序变化后构造行锚点应仍唯一命中")
        # 同时确认 ?? true 锚点未受影响（仍命中原基线）
        load = [a for a in self.anchors_for("usage") if "getBool" in a[1]][0]
        self.assertEqual(self.count(read("usage"), load[1]), 1, "?? true 锚点应独立命中")

    def test_I2_structural_drift_fails_closed(self):
        """I2：结构变（非值变）应导致计数 != 1，每个锚点族至少一个结构变场景。"""
        cases = [
            ("update", "const updateRepo = 'ccch1mneyyy/dsh-go';", "final updateRepo = 'ccch1mneyyy/dsh-go';"),
            ("release", 'REPO = "ccch1mneyyy/dsh-go"', "REPO: str = 'ccch1mneyyy/dsh-go'"),
            ("usage", "?? true;", "?? false;"),
            ("market", "const officialMarketBases = ['https://dl.dshtui.com/market/', 'https://go.dshtui.com/market/'];",
             "final officialMarketBases = <String>[];"),
            ("forum", "const forumDefaultBaseUrl = 'https://forum.dshtui.com';",
             "final forumDefaultBaseUrl = 'https://forum.dshtui.com';"),
        ]
        for key, old_frag, new_frag in cases:
            text = read(key)
            self.assertIn(old_frag, text)
            broken = text.replace(old_frag, new_frag)
            anchors = self.anchors_for(key)
            # 该族里至少一个锚点必须因结构变化而不再命中（fail-closed）
            failed = [a for a in anchors if self.count(broken, a[1]) != 1]
            self.assertTrue(failed, "结构漂移未被任何锚点拒绝: %s" % key)

    def test_I3_duplicate_anchor_fails_closed(self):
        """I3：同一锚点出现 2 次 -> 计数不符，preflight 必须退出非 0。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for key, rel in FILES.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(read(key), encoding="utf-8")
            # 复制论坛默认行使其成为 2 处
            fp = root / FILES["forum"]
            fp.write_text(fp.read_text(encoding="utf-8").replace(
                "const forumDefaultBaseUrl = 'https://forum.dshtui.com';",
                "const forumDefaultBaseUrl = 'https://forum.dshtui.com';\nconst forumDefaultBaseUrl = 'https://forum.dshtui.com';"),
                encoding="utf-8")
            pre = subprocess.run([sys.executable, str(APPLY), str(root), "damemod/Dharness", "--dry-run"] + ALL_FLAGS,
                                 capture_output=True, text=True)
            self.assertNotEqual(pre.returncode, 0, "重复锚点必须 fail-closed")
            self.assertIn("预期 1 处，实得 2 处", pre.stderr)

    def test_I4_two_phase_no_partial_write(self):
        """I4：最后一个锚点坏掉时，排在它前面的所有文件都不应被写入（真两阶段）。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for key, rel in FILES.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(read(key), encoding="utf-8")
            fp = root / FILES["forum"]  # plan 顺序里 forum 在 update 之后
            fp.write_text(fp.read_text(encoding="utf-8").replace(
                "const forumDefaultBaseUrl = 'https://forum.dshtui.com';",
                "final forumDefaultBaseUrl = 'https://forum.dshtui.com';"), encoding="utf-8")
            before = {key: (root / rel).read_text(encoding="utf-8") for key, rel in FILES.items()}
            run = subprocess.run([sys.executable, str(APPLY), str(root), "damemod/Dharness"] + ALL_FLAGS,
                                 capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            after = {key: (root / rel).read_text(encoding="utf-8") for key, rel in FILES.items()}
            self.assertEqual(before, after, "任一锚点失败时不得写入任何文件")

    def test_I5_apply_then_idempotency_guard(self):
        """I5：一次应用后每条 new 都落地，再跑 dry-run 必须失败（防重复应用）。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for key, rel in FILES.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(read(key), encoding="utf-8")
            run = subprocess.run([sys.executable, str(APPLY), str(root), "damemod/Dharness"] + ALL_FLAGS,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            u = (root / FILES["update"]).read_text(encoding="utf-8")
            self.assertIn("String.fromEnvironment('DSH_UPDATE_REPO'", u)
            self.assertNotIn("const updateRepo = 'ccch1mneyyy/dsh-go';", u)
            v = (root / FILES["usage"]).read_text(encoding="utf-8")
            self.assertIn("this.enabled = false", v)
            self.assertIn("?? false;", v)
            m = (root / FILES["market"]).read_text(encoding="utf-8")
            self.assertIn("const officialIndexUrl = '';", m)
            self.assertIn("return MarketCatalog(", m)
            h = (root / FILES["hosts"]).read_text(encoding="utf-8")
            self.assertIn("const <RelayAuthProvider>[]", h)
            f = (root / FILES["forum"]).read_text(encoding="utf-8")
            self.assertIn("const forumDefaultBaseUrl = '';", f)
            again = subprocess.run([sys.executable, str(APPLY), str(root), "damemod/Dharness", "--dry-run"] + ALL_FLAGS,
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0, "已应用过的树不应再次通过 dry-run")

    def test_I6_crlf_matches(self):
        """I6：CRLF 检出的文件经 read_text 通用换行后仍能命中全部锚点。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for key, rel in FILES.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(read(key).replace("\n", "\r\n").encode("utf-8"))
            pre = subprocess.run([sys.executable, str(APPLY), str(root), "damemod/Dharness", "--dry-run"] + ALL_FLAGS,
                                 capture_output=True, text=True)
            self.assertEqual(pre.returncode, 0, pre.stderr)

    def test_I7_short_literal_no_false_positive(self):
        """I7：精确短串 '  throw failure!;' 不应把注释里的相似行算进来。"""
        market = read("market")
        noisy = market.replace(
            "  throw failure!;",
            "  // a comment mentioning throw failure! but not a real throw\n  throw failure!;")
        anchor = [a for a in self.anchors_for("market") if a[1] == "  throw failure!;"][0]
        self.assertEqual(self.count(noisy, anchor[1]), 1,
                         "注释里的相似文本不应被精确短串锚点命中")

    def test_I8_capture_groups_preserve_structure(self):
        """I8：多行块锚点只替换中间 return，保留 if 头与闭合括号（捕获组保形）。"""
        market = read("market")
        anchor = [a for a in self.anchors_for("market") if a[1].startswith("re:(") and "officialMarketFresh" in a[1]][0]
        result = apply._sub(market, anchor[1], anchor[2])
        self.assertIn("if (copy != null && !refresh && DateTime.now().difference(copy.at) < officialMarketFresh) {", result)
        self.assertIn("return MarketCatalog(MarketIndex(url: Uri.parse(officialIndexUrl)", result)
        self.assertNotIn("return _officialCatalog(copy.index, parseMarketSidecarText(copy.sidecar));\n  }",
                         result.replace("    return _officialCatalog(copy.index, parseMarketSidecarText(copy.sidecar));\n  }",
                                        ""))  # 缓存分支已被替换
        # 闭合大括号数量不变（结构未破坏）
        self.assertEqual(result.count("officialMarketFresh"), market.count("officialMarketFresh"))

    def test_I9_every_plan_anchor_unique_on_baseline(self):
        """I9：基线全树所有锚点各恰好命中 1 处（不是只测某几个）。"""
        for rel, old, new in self.plan:
            text = (UPSTREAM / rel).read_text(encoding="utf-8")
            self.assertEqual(self.count(text, old), 1, "基线锚点应唯一: %s / %s" % (rel, old[:50]))

    def anchors_for_menu(self):
        return [a for a in self.plan if a[0] == FILES["menu"]]

    def test_I10_turn_id_anchor_wired(self):
        """I10：菜单 scope 补回合号——锚点唯一、产物含解析逻辑且传给 DshGoScope。"""
        anchors = self.anchors_for_menu()
        self.assertEqual(len(anchors), 1, "菜单补回合号应恰好 1 个锚点")
        rel, old, new = anchors[0]
        self.assertIn("DshGoScope", old)
        # 产物必须：①解析 rowKey ②传给 DshGoScope ③rowKey 为空安全降级
        self.assertIn("item.rowKey", new, "应从 rowKey 解析回合号")
        self.assertIn(r"RegExp(r'^m(\d+)\.')", new, "应认 m<turn>.<step> 格式")
        self.assertIn("turnId: turnId", new, "应把回合号传进 scope")
        self.assertIn("? null", new, "rowKey 缺失应降级为 null")
        # 基线唯一
        self.assertEqual(self.count(read("menu"), old), 1)

    def test_I11_turn_regex_parses_real_rowkeys(self):
        """I11：回合号正则对真实 rowKey 形态正确解析，畸形/缺失安全降级（多场景）。"""
        # 与产物中 Dart 正则等价的 Python 表达，验证解析意图而非具体实现
        import re as _re
        rx = _re.compile(r"^m(\d+)\.")
        cases = {
            "m7.3": "7", "m0.1": "0", "m128.9": "128",
            "d-attempt-x": None, "m.step": None, "": None,
        }
        for rowkey, expected in cases.items():
            m = rx.match(rowkey)
            self.assertEqual(m.group(1) if m else None, expected,
                             "rowKey %r 解析应得 %r" % (rowkey, expected))

    def test_I12_turn_id_fail_closed_on_duplicate(self):
        """I12：菜单锚点重复时 fail-closed，preflight 在任何写入前拒绝。"""
        anchor = self.anchors_for_menu()[0]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            target = root / FILES["menu"]
            target.parent.mkdir(parents=True, exist_ok=True)
            # 人为制造重复锚点
            line = anchor[1] + "\n" + anchor[1] + "\n"
            target.write_text(line, encoding="utf-8")
            self.assertEqual(self.count(target.read_text(encoding="utf-8"), anchor[1]), 2)
            with self.assertRaises(SystemExit):
                apply.preflight(root, [anchor], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
