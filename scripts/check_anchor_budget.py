#!/usr/bin/env python3
"""锚点预算门禁：拦截新功能专用的持久补丁增量。

依据：原生插件层架构设计 §3/§6/§7。L2 补丁只允许一次性补通用原语，
新增功能不得引入专用 flag 或其 App 补丁锚点；既有 flag 的锚点数只许减不许增。

实现：解析 patches/apply_autonomous.py 的 build_plan()，静态归因每一处对 plan
列表的写入。覆盖 append/extend/insert、下标/切片赋值、+=、plan = plan + rhs、
推导式内调用、复合条件中的 flag 提取、无条件写入与同模块 helper 间接调用。
无法可靠归一的形态（别名、外部/动态调用、多 flag 条件）一律 fail-closed。

用法: python3 check_anchor_budget.py [--script 路径] [--golden 路径]
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SCRIPT = HERE.parent / "patches" / "apply_autonomous.py"
DEFAULT_GOLDEN = HERE.parent / "golden" / "anchor-budget.json"

UNCONDITIONAL = "__unconditional__"
WRITE_METHODS = frozenset({"append", "extend", "insert"})
SAFE_BUILTINS = frozenset({
    "len", "list", "tuple", "set", "str", "int", "bool", "type", "range",
    "enumerate", "zip", "sorted", "reversed", "min", "max", "sum", "any", "all",
    "isinstance", "repr", "format", "print",
})

# flag 变量名 -> 语义标签；这些是已登记的「功能专用补丁税」，锚点数只许减不许增。
FEATURE_FLAGS = {
    "recall_turn_id": "菜单补回合号",
    "recall_footnote": "脚注保留尾注",
    "plugin_dismiss": "插件关闭弹层",
    "recall_user_msg": "用户消息反馈菜单",
}


class Risk(RuntimeError):
    """静态归因无法可靠完成，必须 fail-closed。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _is_plan(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "plan"


def _condition_flags(test: ast.AST) -> set[str]:
    """提取条件表达式中作为布尔开关使用的变量名。

    覆盖 ast.Name、ast.BoolOp（And/Or）与 ast.UnaryOp(Not)。比较、常量、
    属性访问、调用等不视为 flag。空集表示条件里无可归因的 flag。
    """
    found: set[str] = set()
    if isinstance(test, ast.Name):
        found.add(test.id)
    elif isinstance(test, ast.BoolOp):
        for value in test.values:
            found.update(_condition_flags(value))
    elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        found.update(_condition_flags(test.operand))
    return found


def _is_plan_write(node: ast.AST) -> bool:
    """判断单个语句节点是否构成一次对 plan 的写入。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in WRITE_METHODS and _is_plan(node.func.value)
    if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
        return _is_plan(node.target)
    if isinstance(node, (ast.Assign, ast.NamedExpr)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if isinstance(value, ast.List) and not value.elts:
            return False  # plan = [] 是初始化
        if any(_is_plan(t) or (isinstance(t, ast.Subscript) and _is_plan(t.value)) for t in targets):
            return True
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return any(_is_plan(side) for side in (value.left, value.right))
    return False


class _Analysis:
    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.funcs: dict[str, ast.AST] = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[n.name] = n
        self.build_plan = self.funcs.get("build_plan")
        if self.build_plan is None:
            raise Risk("找不到 build_plan() 函数")
        self.counts: dict[str, int] = {}
        self.seen_writes: set[int] = set()

    def analyze(self) -> dict[str, int]:
        self._check_aliases(self.build_plan)
        self._visit(self.build_plan, None)
        return self.counts

    def _check_aliases(self, node: ast.AST) -> None:
        """plan 被赋值给别名后无法静态追踪写入，fail-closed。"""
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.NamedExpr)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                if isinstance(sub.value, ast.Name) and sub.value.id == "plan":
                    for t in targets:
                        if not _is_plan(t):
                            raise Risk(
                                f"第 {sub.lineno} 行将 plan 赋给别名，无法静态追踪后续写入"
                            )

    def _add(self, flag: str | None, n: int = 1) -> None:
        if n <= 0:
            return
        key = flag if flag is not None else UNCONDITIONAL
        self.counts[key] = self.counts.get(key, 0) + n

    def _record(self, node: ast.AST, flag: str | None) -> None:
        """记录一个写入节点，重复节点只计一次（避免递归重复遍历）。"""
        if id(node) in self.seen_writes:
            return
        if _is_plan_write(node):
            self.seen_writes.add(id(node))
            self._add(flag)

    def _visit(self, node: ast.AST, flag: str | None) -> None:
        """遍历语句树；写入归到最近的外层 If 的 flag，未门控归到 flag。"""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If):
                cflags = _condition_flags(child.test)
                if len(cflags) > 1:
                    raise Risk(
                        f"第 {child.lineno} 行条件同时含多个 flag"
                        f"（{', '.join(sorted(cflags))}），无法静态归一到单一预算桶"
                    )
                child_flag = next(iter(cflags)) if cflags else flag
                self._record(child.test, child_flag)
                for sub in ast.walk(child):
                    if sub is not child and sub is not child.test:
                        self._record(sub, child_flag)
                for call in self._calls_in_subtree(child):
                    self._resolve_call(call, child_flag, call.lineno)
                self._visit(child, child_flag)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            else:
                self._record(child, flag)
                for sub in ast.walk(child):
                    if sub is not child:
                        self._record(sub, flag)
                for call in self._calls_in_subtree(child):
                    self._resolve_call(call, flag, call.lineno)
                self._visit(child, flag)

    def _calls_in_subtree(self, node: ast.AST) -> list[ast.Call]:
        return [s for s in ast.walk(node) if isinstance(s, ast.Call)]

    def _resolve_call(self, call: ast.Call, flag: str | None, lineno: int) -> None:
        """解析调用：同模块 helper 静态展开；未知外部调用 fail-closed。"""
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr in WRITE_METHODS:
                return  # plan.append/extend/insert 已由 _is_plan_write 计数
            return  # 其它方法调用（如 str.format）不写 plan
        if not isinstance(func, ast.Name):
            return
        name = func.id
        if name in SAFE_BUILTINS:
            return
        if name in self.funcs and self.funcs[name] is not self.build_plan:
            helper = self.funcs[name]
            if id(helper) in self.seen_writes:
                return
            self.seen_writes.add(id(helper))
            for sub in ast.walk(helper):
                self._record(sub, flag)
            return
        raise Risk(
            f"第 {lineno} 行调用外部/动态函数 {name}()，无法判定是否写入 plan"
        )


def count_anchors(script: pathlib.Path) -> dict[str, int]:
    """返回 {flag 名: 锚点数}；静态无法可靠判定时抛 Risk 以 fail-closed。"""
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    return _Analysis(tree).analyze()


def load_budget(golden: pathlib.Path) -> dict[str, int]:
    raw = json.loads(golden.read_text(encoding="utf-8"))
    return {
        k: v for k, v in raw.items()
        if (k == UNCONDITIONAL or not k.startswith("_")) and isinstance(v, int)
    }


def main() -> int:
    args = sys.argv[1:]
    script = DEFAULT_SCRIPT
    golden = DEFAULT_GOLDEN
    if "--script" in args:
        script = pathlib.Path(args[args.index("--script") + 1])
    if "--golden" in args:
        golden = pathlib.Path(args[args.index("--golden") + 1])
    if not script.is_file():
        sys.exit(f"找不到补丁脚本: {script}")
    if not golden.is_file():
        sys.exit(f"缺少锚点预算 golden: {golden}（先用当前脚本生成）")

    print("=== 锚点预算门禁（新增功能专用补丁即拦截）===")
    try:
        actual = count_anchors(script)
    except Risk as exc:
        print(f"[FAIL] 静态归因不可靠，拒绝放行: {exc.message}")
        print("\n结论：存在无法静态判定的 plan 写入，fail-closed 拒绝。")
        return 1

    budget = load_budget(golden)
    failed = 0

    # 1) 未知 flag：脚本里出现预算基线没有的锚点 flag —— 一律拦截。
    for flag in sorted(set(actual) - set(budget)):
        print(f"[FAIL] 新增锚点 flag: {flag}({actual[flag]} 个锚点) 不在预算基线；"
              f"原始标识符: {flag} —— 禁止新增功能专用补丁")
        failed += 1

    # 2) 已知 flag：锚点数只许减不许增。
    for flag, want in budget.items():
        got = actual.get(flag)
        label = FEATURE_FLAGS.get(flag, "")
        tag = f"{flag}({label})" if label else flag
        if flag == UNCONDITIONAL:
            tag = "无条件 plan 写入"
        if got is None:
            print(f"[OK] {tag}: 锚点已清零（原 {want}），已毕业")
        elif got > want:
            print(f"[FAIL] {tag}: 锚点从 {want} 增到 {got} —— 新增功能专用补丁，禁止")
            failed += 1
        elif flag == UNCONDITIONAL and got < want:
            print(f"[FAIL] {tag}: 锚点从 {want} 减到 {got} —— 通用锚点缩减需人工复核")
            failed += 1
        elif got < want:
            print(f"[OK] {tag}: 锚点从 {want} 减到 {got}，补丁税在偿还")
        else:
            print(f"[OK] {tag}: 锚点 {got}，与预算一致" + ("（待毕业补丁税）" if label else ""))

    if failed:
        print(f"\n结论：{failed} 项超预算，拒绝放行。新功能请用通用原语（L0 声明）实现。")
        return 1
    print("\n结论：无功能专用锚点增量，放行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
