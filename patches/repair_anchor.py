#!/usr/bin/env python3
"""补丁漂移修复助手：解析 apply_autonomous.py 的失败日志 -> 定位锚点 -> 找新候选 -> 生成修复 diff -> 门禁校验。

纯标准库。三个子命令：

  diagnose <源码目录> <失败日志文件或 ->   # 解析失败日志，在新源码树里找候选新锚点，输出 JSON
  propose  <源码目录> --index N --old <新的old字面量> [--new <新的new字面量>]
                                           # 改写 apply_autonomous.py 第 N 个锚点，输出 unified diff
  verify   <源码目录> [--flags ...]        # 跑 apply_autonomous.py --dry-run，作为修复门禁

失败文案与 apply_autonomous.py 的 sys.exit 对应：
  - replace():  sys.exit("{path}: 预期 {count} 处，实得 {found} 处: {old[:70]!r}")
  - preflight(): sys.exit("{path}: 预期 1 处，实得 {found} 处: {old[:70]!r}")
                  sys.exit("{path}: 文件不存在") / "{path}: 已存在，拒绝覆盖"
  - 锚点在 build_plan() 里以 plan.append((rel, old, new)) / newfiles.append((rel, content)) 声明。
  - 工作流实际启用的开关：
        --telemetry-default-off --single-market-source --lazy-relay-login --forum-default-empty
    即 2 个必改锚点 + 2 + 5 + 2 + 1 = 12 个 plan 锚点，0 个新文件。
"""
import argparse
import ast
import difflib
import json
import pathlib
import re
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve()
PATCHES_DIR = SCRIPT.parent
APPLY = PATCHES_DIR / "apply_autonomous.py"

DEFAULT_FLAGS = [
    "--telemetry-default-off",
    "--single-market-source",
    "--lazy-relay-login",
    "--forum-default-empty",
]

RE_COUNT = re.compile(r"^(?P<path>.+?): 预期 (?P<expected>\d+) 处，实得 (?P<found>\d+) 处: (?P<oldrepr>.*)$")
RE_MISSING = re.compile(r"^(?P<path>.+?): 文件不存在$")
RE_EXISTS = re.compile(r"^(?P<path>.+?): 已存在，拒绝覆盖$")


def extract_anchors():
    """用 AST 从 apply_autonomous.py 读出全部 plan.append((rel, old, new))。"""
    src = APPLY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    anchors = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "append"):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "plan"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Tuple):
            continue
        elts = node.args[0].elts
        if len(elts) < 2:
            continue
        rel_node, old_node = elts[0], elts[1]
        new_node = elts[2] if len(elts) > 2 else None
        try:
            rel = ast.literal_eval(rel_node)
            old = ast.literal_eval(old_node)
        except (ValueError, TypeError):
            continue
        if not isinstance(rel, str) or not isinstance(old, str):
            continue
        new_src = None
        if new_node is not None:
            a, b = new_node.lineno - 1, getattr(new_node, "end_lineno", new_node.lineno)
            new_src = "".join(lines[a:b])
        anchors.append({
            "index": len(anchors),
            "path": rel,
            "old": old,
            "old_line": old_node.lineno,
            "old_end_line": getattr(old_node, "end_lineno", old_node.lineno),
            "new_src": new_src,
            "new_line": new_node.lineno if new_node is not None else None,
            "new_end_line": getattr(new_node, "end_lineno", None) if new_node is not None else None,
        })
    return anchors, src, lines


def parse_failure(log_text):
    """把失败日志解析成结构化条目，返回 list[dict]。"""
    items = []
    for raw in log_text.splitlines():
        line = raw.strip()
        # 工作流会给日志每行加前缀，这里按整行 strip 后匹配 sys.exit 文案。
        m = RE_COUNT.match(line)
        if m:
            oldrepr = m.group("oldrepr").strip()
            try:
                old_prefix = ast.literal_eval(oldrepr)
            except (ValueError, SyntaxError):
                old_prefix = oldrepr
            items.append({
                "kind": "count",
                "path": m.group("path"),
                "expected": int(m.group("expected")),
                "found": int(m.group("found")),
                "old_prefix": old_prefix,
            })
            continue
        m = RE_MISSING.match(line)
        if m:
            items.append({"kind": "missing", "path": m.group("path")})
            continue
        m = RE_EXISTS.match(line)
        if m:
            items.append({"kind": "exists", "path": m.group("path")})
    return items


def match_anchor(failure, anchors):
    """把一条失败记录对应到 apply_autonomous.py 里的锚点。"""
    path = failure["path"]
    # 日志里的 path 多为绝对路径，锚点存的是相对路径，故用后缀匹配。
    cands = [a for a in anchors if path == a["path"] or path.endswith("/" + a["path"])]
    if not cands:
        return None
    prefix = failure.get("old_prefix", "")
    # 同文件多锚点时，靠 old 前缀相似度区分；低于阈值则退回第一个候选。
    if prefix:
        scored = sorted(cands, key=lambda a: difflib.SequenceMatcher(None, prefix, a["old"][:70]).ratio(), reverse=True)
        if scored and difflib.SequenceMatcher(None, prefix, scored[0]["old"][:70]).ratio() >= 0.5:
            return scored[0]
    return cands[0]


def find_candidates(src_dir, anchor, topn=5):
    """在新源码里，按相似度找 old 的候选替换行。"""
    p = pathlib.Path(src_dir) / anchor["path"]
    if not p.is_file():
        return []
    target = anchor["old"].strip()
    # 按行相似度找候选替换行，容忍上游小幅改写。
    out = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = difflib.SequenceMatcher(None, target, stripped).ratio()
        if ratio >= 0.35:
            out.append({"line": lineno, "text": stripped, "score": round(ratio, 3)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:topn]


def cmd_diagnose(args):
    anchors, _, _ = extract_anchors()
    log = pathlib.Path(args.log).read_text(encoding="utf-8") if args.log != "-" else sys.stdin.read()
    failures = parse_failure(log)
    result = []
    for f in failures:
        a = match_anchor(f, anchors)
        if a is None:
            result.append({"failure": f, "anchor": None, "note": "未匹配到锚点（可能是 newfile 或日志格式变化）"})
            continue
        result.append({
            "failure": f,
            "anchor": {"index": a["index"], "path": a["path"], "old": a["old"], "src_line": a["old_line"]},
            "candidates": find_candidates(args.src, a, args.topn),
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))


def splice_literal(src_lines, start_line, end_line, new_literal):
    """把 [start_line, end_line] 的源码行替换成一个新的字符串字面量，返回新源码文本。

    保留原首行的行首缩进，使输出的 diff 干净、可直接 apply。
    """
    first = src_lines[start_line - 1]
    indent = first[: len(first) - len(first.lstrip())]
    before = "".join(src_lines[: start_line - 1])
    after = "".join(src_lines[end_line:])
    return before + indent + repr(new_literal) + ",\n" + after


def cmd_propose(args):
    anchors, src, lines = extract_anchors()
    if args.index < 0 or args.index >= len(anchors):
        sys.exit("index 超出范围：0..{}".format(len(anchors) - 1))
    a = anchors[args.index]
    new_src = src
    new_lines = lines
    if args.new_old is not None:
        new_lines = splice_literal(new_lines, a["old_line"], a["old_end_line"], args.new_old).splitlines(keepends=True)
    if args.new_new is not None and a["new_line"] is not None:
        new_lines = splice_literal(new_lines, a["new_line"], a["new_end_line"], args.new_new).splitlines(keepends=True)
    new_src = "".join(new_lines)
    diff = "".join(difflib.unified_diff(
        src.splitlines(keepends=True), new_src.splitlines(keepends=True),
        fromfile="a/patches/apply_autonomous.py", tofile="b/patches/apply_autonomous.py",
    ))
    if not diff:
        sys.exit("没有产生任何改动（检查 --old/--new 是否与现有字面量相同）")
    print(diff, end="")
    if args.apply:
        APPLY.write_text(new_src, encoding="utf-8")
        print("已写入", APPLY, file=sys.stderr)


def cmd_verify(args):
    flags = args.flags if args.flags is not None else DEFAULT_FLAGS
    cmd = [sys.executable, str(APPLY), str(pathlib.Path(args.src).resolve()), args.repo] + flags + ["--dry-run"]
    print("运行:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diagnose", help="解析失败日志并在新源码树里找候选锚点")
    d.add_argument("src", help="新源码目录")
    d.add_argument("log", help="失败日志文件，或 - 表示 stdin")
    d.add_argument("--topn", type=int, default=5)
    d.set_defaults(fn=cmd_diagnose)

    p = sub.add_parser("propose", help="改写锚点并输出 unified diff")
    p.add_argument("src", help="源码目录（仅用于定位，实际改的是 apply_autonomous.py）")
    p.add_argument("--index", type=int, required=True, help="锚点序号（diagnose 输出里的 anchor.index）")
    p.add_argument("--old", dest="new_old", help="新的 old 字面量（当前源码原文）")
    p.add_argument("--new", dest="new_new", help="新的 new 字面量（替换目标，可选）")
    p.add_argument("--apply", action="store_true", help="直接写回 apply_autonomous.py（默认只输出 diff）")
    p.set_defaults(fn=cmd_propose)

    v = sub.add_parser("verify", help="dry-run 门禁校验")
    v.add_argument("src", help="源码目录")
    v.add_argument("--repo", default="damemod/Dharness")
    v.add_argument("--flags", nargs="*", default=None, help="覆盖默认开关（默认=%s）" % " ".join(DEFAULT_FLAGS))
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    rc = args.fn(args)
    if isinstance(rc, int):
        sys.exit(rc)


if __name__ == "__main__":
    main()
