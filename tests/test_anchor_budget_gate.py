#!/usr/bin/env python3
"""意图驱动的锚点预算门禁回归测试。

每个意图覆盖多个变异场景、多个断言；所有变异只写入临时目录中的
apply_autonomous.py 副本，真实的 patches/apply_autonomous.py 不被修改。
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CHECKER = ROOT / "scripts" / "check_anchor_budget.py"
SCRIPT = ROOT / "patches" / "apply_autonomous.py"
GOLDEN = ROOT / "golden" / "anchor-budget.json"

MARKER = "    if recall_user_msg:"
SIG = "forum_empty, recall_turn_id, recall_footnote, plugin_dismiss, recall_user_msg)"


def run(script: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(CHECKER), "--script", str(script), "--golden", str(GOLDEN)],
        capture_output=True, text=True,
    )


def mutate(text: str, injected: str, add_arg: str | None = None) -> str:
    """在 recall_user_msg 块前插入变异代码；injected 每行须已含正确缩进。"""
    assert MARKER in text
    text = text.replace(MARKER, injected + MARKER, 1)
    if add_arg:
        assert SIG in text
        text = text.replace(SIG, SIG[:-1] + ", " + add_arg + ")", 1)
    return text


def tmp_script(directory: str, text: str) -> pathlib.Path:
    tmp = pathlib.Path(directory) / "apply_autonomous.py"
    tmp.write_text(text, encoding="utf-8")
    return tmp


class AnchorBudgetTests(unittest.TestCase):
    def test_intent_clean_passes(self):
        """正常路径：9 个 flag 桶 + 无条件桶与 golden 一致，rc=0。"""
        r = run(SCRIPT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("无功能专用锚点增量", r.stdout)
        for flag in ("telemetry_off", "single_market", "recall_user_msg", "with_dex", "无条件 plan 写入"):
            self.assertIn(flag, r.stdout)

    def test_intent_A1_unconditional_write_rejected(self):
        """A1：无条件 plan 写入必须增加无条件桶计数并拒绝。"""
        scenarios = {
            "append": "    plan.append(('app/lib/a.dart', 'old', 'new'))\n",
            "extend": "    plan.extend([('app/lib/a.dart', 'old', 'new')])\n",
            "insert": "    plan.insert(0, ('app/lib/a.dart', 'old', 'new'))\n",
            "plus_equal": "    plan += [('app/lib/a.dart', 'old', 'new')]\n",
            "reassign_add": "    plan = plan + [('app/lib/a.dart', 'old', 'new')]\n",
            "index_assign": "    plan[0:0] = [('app/lib/a.dart', 'old', 'new')]\n",
        }
        for name, line in scenarios.items():
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory(prefix="anchor-A1-") as d:
                    text = mutate(SCRIPT.read_text(encoding="utf-8"), line)
                    r = run(tmp_script(d, text))
                    self.assertNotEqual(r.returncode, 0, name + " 未被拒绝: " + r.stdout)
                    self.assertIn("无条件 plan 写入", r.stdout)
                    self.assertIn("锚点从 2 增到 3", r.stdout)

    def test_intent_A2_compound_condition_rejected(self):
        """A2：复合条件里的已知 flag 必须被识别并归因到该 flag 桶。"""
        scenarios = {
            "and_true": ("    if telemetry_off and True:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "增到"),
            "or_flag": ("    if telemetry_off or forum_empty:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "多个 flag"),
            "not_flag": ("    if not telemetry_off:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "增到"),
        }
        for name, (injected, expected) in scenarios.items():
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory(prefix="anchor-A2-") as d:
                    text = mutate(SCRIPT.read_text(encoding="utf-8"), injected)
                    r = run(tmp_script(d, text))
                    self.assertNotEqual(r.returncode, 0, name + " 未被拒绝: " + r.stdout)
                    self.assertIn("telemetry_off", r.stdout)
                    self.assertIn(expected, r.stdout)

    def test_intent_A2_multi_flag_condition_fail_closed(self):
        """A2b：单个条件含多个 flag 无法归一，必须 fail-closed 拒绝。"""
        injected = "    if telemetry_off and forum_empty:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n"
        with tempfile.TemporaryDirectory(prefix="anchor-A2b-") as d:
            text = mutate(SCRIPT.read_text(encoding="utf-8"), injected)
            r = run(tmp_script(d, text))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("多个 flag", r.stdout)
            self.assertIn("静态归因不可靠", r.stdout)

    def test_intent_A3_new_flag_in_compound_rejected(self):
        """A3：新 flag 藏在复合条件里也必须作为未知 flag 被拒。"""
        scenarios = {
            "and_true": ("    if new_feature and True:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "不在预算基线"),
            "or_flag": ("    if telemetry_off or new_feature:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "多个 flag"),
            "not_flag": ("    if not new_feature:\n        plan.append(('app/lib/a.dart', 'old', 'new'))\n", "不在预算基线"),
        }
        for name, (injected, expected) in scenarios.items():
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory(prefix="anchor-A3-") as d:
                    text = mutate(SCRIPT.read_text(encoding="utf-8"), injected, add_arg="new_feature")
                    r = run(tmp_script(d, text))
                    self.assertNotEqual(r.returncode, 0, name + " 未被拒绝: " + r.stdout)
                    self.assertIn("new_feature", r.stdout)
                    self.assertIn(expected, r.stdout)

    def test_intent_A4_non_append_methods_rejected(self):
        """A4：extend/insert/+= 等非 append 写入必须被计数并拒绝。"""
        scenarios = {
            "extend": (
                "    if new_feature:\n        plan.extend([('app/lib/a.dart', 'old', 'new'), ('app/lib/b.dart', 'old', 'new')])\n",
                "1 个锚点",
            ),
            "insert": (
                "    if telemetry_off:\n        plan.insert(0, ('app/lib/a.dart', 'old', 'new'))\n",
                "增到 3",
            ),
            "plus_equal": (
                "    if forum_empty:\n        plan += [('app/lib/a.dart', 'old', 'new')]\n",
                "增到 2",
            ),
            "reassign_add": (
                "    if lazy_relay:\n        plan = plan + [('app/lib/a.dart', 'old', 'new')]\n",
                "增到 3",
            ),
            "index_assign": (
                "    if recall_turn_id:\n        plan[0:0] = [('app/lib/a.dart', 'old', 'new')]\n",
                "增到 2",
            ),
        }
        for name, (injected, expected) in scenarios.items():
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory(prefix="anchor-A4-") as d:
                    arg = "new_feature" if "new_feature" in injected else None
                    text = mutate(SCRIPT.read_text(encoding="utf-8"), injected, add_arg=arg)
                    r = run(tmp_script(d, text))
                    self.assertNotEqual(r.returncode, 0, name + " 未被拒绝: " + r.stdout)
                    self.assertIn(expected, r.stdout)

    def test_intent_A5_helper_indirect_writes_rejected(self):
        """A5：同函数内 helper 的间接写入必须被静态展开并计数。"""
        scenarios = {
            "helper_five_appends": (
                "def _add_anchors(plan):\n"
                "    plan.append(('app/lib/a.dart', 'old', 'new'))\n"
                "    plan.append(('app/lib/b.dart', 'old', 'new'))\n"
                "    plan.append(('app/lib/c.dart', 'old', 'new'))\n"
                "    plan.append(('app/lib/d.dart', 'old', 'new'))\n"
                "    plan.append(('app/lib/e.dart', 'old', 'new'))\n\n\n",
                "    if new_feature:\n        _add_anchors(plan)\n",
                "5 个锚点",
            ),
            "helper_extend": (
                "def _add_anchors2(plan):\n"
                "    plan.extend([('app/lib/a.dart', 'old', 'new')])\n\n\n",
                "    if telemetry_off:\n        _add_anchors2(plan)\n",
                "增到 3",
            ),
        }
        for name, (helper, injected, expected) in scenarios.items():
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory(prefix="anchor-A5-") as d:
                    text = SCRIPT.read_text(encoding="utf-8")
                    marker = "def build_plan("
                    self.assertIn(marker, text)
                    text = text.replace(marker, helper + marker, 1)
                    arg = "new_feature" if "new_feature" in injected else None
                    text = mutate(text, injected, add_arg=arg)
                    r = run(tmp_script(d, text))
                    self.assertNotEqual(r.returncode, 0, name + " 未被拒绝: " + r.stdout)
                    self.assertIn(expected, r.stdout)

    def test_intent_A5_unknown_call_fail_closed(self):
        """A5b：调用外部/动态函数无法判定是否写 plan，必须 fail-closed。"""
        injected = "    if new_feature:\n        external_inject(plan)\n"
        with tempfile.TemporaryDirectory(prefix="anchor-A5b-") as d:
            text = mutate(SCRIPT.read_text(encoding="utf-8"), injected, add_arg="new_feature")
            r = run(tmp_script(d, text))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("静态归因不可靠", r.stdout)
            self.assertIn("external_inject", r.stdout)

    def test_intent_A5_plan_alias_fail_closed(self):
        """A5c：plan 别名赋值后无法追踪写入，必须 fail-closed。"""
        injected = "    p = plan\n    p.append(('app/lib/a.dart', 'old', 'new'))\n"
        with tempfile.TemporaryDirectory(prefix="anchor-A5c-") as d:
            r = run(tmp_script(d, mutate(SCRIPT.read_text(encoding="utf-8"), injected)))
            self.assertNotEqual(r.returncode, 0, "plan 别名写入未被拒绝: " + r.stdout)
            self.assertIn("别名", r.stdout)

    def test_intent_A6_nested_if_rejected(self):
        """A6：嵌套 if 里的写入必须归因到外层 flag。"""
        injected = (
            "    if new_feature:\n"
            "        if True:\n"
            "            plan.append(('app/lib/a.dart', 'old', 'new'))\n"
        )
        with tempfile.TemporaryDirectory(prefix="anchor-A6-") as d:
            text = mutate(SCRIPT.read_text(encoding="utf-8"), injected, add_arg="new_feature")
            r = run(tmp_script(d, text))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("new_feature", r.stdout)
            self.assertIn("不在预算基线", r.stdout)

    def test_intent_A7_insert_as_append_rejected(self):
        """A7：已有 flag 的 append 改成 insert 不得被误判为锚点减少。

        场景一：append→insert 原地替换（锚点数不变，应放行，证明不误判为毕业）。
        场景二：append→insert 的同时再补一个 insert（真实锚点增加，必须拒绝）。
        """
        with tempfile.TemporaryDirectory(prefix="anchor-A7a-") as d:
            text = SCRIPT.read_text(encoding="utf-8")
            idx = text.index("    if plugin_dismiss:")
            end = text.index("    if recall_user_msg:", idx)
            seg = text[idx:end].replace("plan.append((", "plan.insert(0, (", 1)
            r = run(tmp_script(d, text[:idx] + seg + text[end:]))
            self.assertEqual(r.returncode, 0, "append→insert 原地替换被误判: " + r.stdout)
            self.assertIn("plugin_dismiss(插件关闭弹层): 锚点 2", r.stdout)

        with tempfile.TemporaryDirectory(prefix="anchor-A7b-") as d:
            text = SCRIPT.read_text(encoding="utf-8")
            idx = text.index("    if plugin_dismiss:")
            end = text.index("    if recall_user_msg:", idx)
            seg = text[idx:end].replace("plan.append((", "plan.insert(0, (", 1)
            extra = "        plan.insert(0, ('app/lib/extra.dart', 'old', 'new'))\n"
            seg = seg.replace("        plan.insert(0, (", extra + "        plan.insert(0, (", 1)
            r = run(tmp_script(d, text[:idx] + seg + text[end:]))
            self.assertNotEqual(r.returncode, 0, "insert 增加锚点未被拒绝: " + r.stdout)
            self.assertIn("plugin_dismiss", r.stdout)
            self.assertIn("增到 3", r.stdout)

    def test_intent_A8_comprehension_append_rejected(self):
        """A8：推导式里的 append 必须被计数。"""
        injected = (
            "    if new_feature:\n"
            "        [plan.append(('app/lib/a.dart', 'old', 'new')) for _ in range(3)]\n"
        )
        with tempfile.TemporaryDirectory(prefix="anchor-A8-") as d:
            text = mutate(SCRIPT.read_text(encoding="utf-8"), injected, add_arg="new_feature")
            r = run(tmp_script(d, text))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("new_feature", r.stdout)
            self.assertIn("不在预算基线", r.stdout)

    def test_intent_existing_flag_growth_rejected(self):
        """基本回归：给已有 flag 加第 3 个 append 必须拒绝。"""
        with tempfile.TemporaryDirectory(prefix="anchor-growth-") as d:
            text = SCRIPT.read_text(encoding="utf-8")
            idx = text.index("    if plugin_dismiss:")
            end = text.index("    if recall_user_msg:", idx)
            seg = text[idx:end]
            extra = "        plan.append(('app/lib/extra.dart', 'old', 'new'))\n"
            seg = seg.replace("        plan.append((", extra + "        plan.append((", 1)
            r = run(tmp_script(d, text[:idx] + seg + text[end:]))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("plugin_dismiss", r.stdout)
            self.assertIn("增到 3", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
