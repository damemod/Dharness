# 原生接缝安全门禁 —— 设计、证据、复核与落地

本文件记录 Dharness 的 `.github/workflows/seam-security.yml` 及其脚本守什么、
依据什么证据、当前能力边界，以及独立复核发现的问题与修复方向。

门禁的定位是**纵深防御的哨兵**：对上游源码中可直接观测的安全常量与白名单变化
做自动对比。它不能替代 dsh-go 源码侧的第一现场防线，也不能在现有实现下兑现
对所有语义放松的 fail-closed 拦截。具体边界见 §6「独立复核与修复记录」。

---

## 1. 背景与盲区分析

Dharness 自动跟随上游 `ccch1mneyyy/dsh-Go` 的 tag 重建并自签发布。现有门禁：

| 门禁 | 守什么 | 盲区 |
|---|---|---|
| `verify-fix.yml` ① | `apply_autonomous.py` 补丁能真应用（fail-closed） | 只校验本仓库自己的补丁 |
| `verify-fix.yml` ② | `check_build_config.py --strict` | 只校验本仓库自己的补丁配置点 |
| `autonomous-release.yml` build job | 构建+签名门禁 | 构建成功不代表安全语义没被放松 |

**盲区**：上游某版若放松原生插件桥的安全不变量 —— 放宽 effect 白名单、
把 scope 扩到消息原文、调高违规降级阈值、删掉 permission 硬拒 —— 上述门禁
不改变红，改动会被原样重新发布。本门禁针对的被检对象是上游源码的原生桥，
而不是本仓库自己的补丁。

安全模型沿用本仓库不可信区原则：`permissions: contents: read`，全程无 Secret、
无写权限；只读检查，不改源码、不触发构建。

---

## 2. 守什么（安全不变量 + 证据链）

所有常量与断言均在 2026-11-20 读取 `/tmp/dshgo-src`（v1.2.0, commit
`c1a84e3f833ad59ff07a5027994dae3935907a2d`）源码取证，非推断。

### 2.1 快照读取的真实常量（`scripts/seam-snapshot.mjs`）

| 快照路径 | 真实值 | 源码位置 |
|---|---|---|
| `protocol` | `1` | `limits.js:3` `export const PROTOCOL = 1` |
| `effects.static` | insert/command/open/link/toast/copy | `effects.js:7-8` `STATIC_TYPES` |
| `effects.all` | 上述 + refresh/patch | `effects.js:8` `ALL_TYPES` |
| `effects.deferred` | insert/copy/toast/patch | `effects.js:10` `DEFERRED_TYPES` |
| `effects.effectFeatures` | audio/voice/overlay | `capabilities.js:9` `EFFECT_FEATURES` |
| `effects.pickerTypes` | pickFile/pickImage | `capabilities.js:10` `PICKER_TYPES` |
| `capabilities.features` | audio/voice/micro/overlay/files | `capabilities.js:7` `FEATURES` |
| `capabilities.capEvents/terminalEvents` | 见基线 | `capabilities.js:25-31` |
| `scopeKeys` | sessionId/turnId/messageId/callId | `limits.js:73` `SCOPE_KEYS`（仅 id 类键，无原文键） |
| `limits.*` | violationsToDegrade=5, effectWindowMs=3000, manifestBytes=16384, viewBytes=32768, viewNodes=120, feeds=64, invokeBurst=8, invokePerSecond=4, invokeConcurrent=4, effectsPerInvoke=4 | `limits.js:5-50` `LIMITS` |

### 2.2 六条源码硬红线（`sourceGuards`，`lineHas()` 逐行取证）

| 探针 | 语义 | 源码证据 | 破坏后果 |
|---|---|---|---|
| `permissionHardDeny` | `command.name==='permission'` 硬拒 | `effects.js:77` | 插件可申请权限预设 |
| `linkHttpsOnly` | open/link 仅 https | `effects.js:92` `url.protocol !== 'https:'` | 可跳任意协议 |
| `linkNoCredentials` | URL 禁带 user/password | `effects.js:93` | 凭据泄漏 |
| `unknownEffectNoViolation` | 未知 effect 记 issue 但 violation:false | `effects.js:54` | 破坏 v1「未知即忽略」兼容基石 |
| `pickerUserGesture` | 文件选择需用户点按 | `capabilities.js:166` | 插件可静默读文件 |
| `degradeAtThreshold` | 违规达阈值触发 degraded | `core.js:314-318` | 恶意插件不被降级 |

这六条探针在未变异的 v1.2.0 源码上均为 true，各自锚定的原始代码行确实实现
对应语义。但除 `unknownEffectNoViolation` 外，其余多为文本正则匹配：保留被匹配的
字面串、改动周围语义即可做到「探针 true、语义失守」。此外存在两处语义链路盲区：
`capabilities.js:131,161` 的 `unknown()` helper 无探针，`routes.js:366` 的
`userGesture` 传值链路不在快照范围。修复方向见 §6。

> 取证记录：`effects.js:54` 那行含模板串 `` `${JSON.stringify(e.type).slice(0, 40)}` ``，
> 最初的跨括号正则 `[^}]*` 被模板串里的 `}` 提前截断，导致探针误报 false。
> 改为 `lineHas(src, marker, predicate)` 逐行断言后修复。这一处误报记录了
> 证据链需要逐行校验、不能依赖宽泛正则的事实。

### 2.3 判定方向（`check_seam_security.py`）

- `limits.*` 数值**变大** = 防御放松 → 报「放宽」；
- 白名单**新增项** = 攻击面扩大 → 报「扩大/变更」；
- `scopeKeys` / `sourceGuards` / `protocol` = HARD 路径，任何变化直接 `[HARD]`；
- 任何差异都退出码 1（fail-closed），强制人工复核并显式更新 golden。

该 fail-closed 行为对「数值/白名单/scopeKeys 的直接改动」成立；对「保留匹配文本、
放松实际语义」的改写存在假绿，详见 §6。

---

## 3. 三道门禁（workflow 顺序）

1. **接缝安全快照 vs golden** —— 检出上游源码，`python3 self/scripts/check_seam_security.py upstream`。
2. **锚点预算** —— `python3 self/scripts/check_anchor_budget.py`。
   守的是「新功能不得再往 App 加功能专用持久补丁」这一约束。
   基线来自 `apply_autonomous.py build_plan()` 的 AST 实测锚点数：
   `recall_turn_id:1 / recall_footnote:2 / plugin_dismiss:2 / recall_user_msg:6`
   （`recall_user_msg` 的 6 锚点与 L2 分析「用户消息可撤=4 文件/6 锚点」交叉印证）。
   新增 flag 或已有 flag 锚点增加即拒；锚点减少视为「毕业/偿还补丁税」，放行。
   当前 AST 实现只统计直接 `if flag:` 下的直接 `plan.append`，对无条件 append、
   复合条件、`plan.extend`/`plan.insert`、helper 间接调用存在结构性绕过，详见 §6。
3. **门禁自测（意图驱动变异测试）** —— 7 项安全门禁测试 + 3 项锚点预算测试。
   每个意图含正常场景 + 变异场景：正常放行、放宽阈值/删硬拒/加原文 scope 键/
   扩大白名单/破坏兼容基石/新增功能 flag 被拒。该自测覆盖的是自带变异用例，
   不等于对全部语义放松形态的证明。

workflow 触发：`pull_request` / `push:main` / `workflow_dispatch`（可指定 `upstream_tag`）。
Node 版本 `24.22.1`（与上游 `goldens.yml`/`runtime.yml` 一致）。
该 workflow 目前与 `autonomous-release.yml` 无 `needs`/`workflow_run` 依赖，被检 ref
与实际发版 ref 可能不同，因此其绿灯不构成发版保证；修复方向见 §6。

---

## 4. 与上层设计的关系

本门禁对应「架构设计 §4 安全闸」中的供应链/发版检查与锚点增量=0 检查的工程实现：

- 能力边界：插件层只拿注册点+批量上下文，多选状态机归 App（设计 §11）—— 架构层约束，非 CI。
- effect 白名单闭合：本门禁 §2.1/§2.2 守白名单与硬拒常量不被放松（受 §6 所列绕过限制）。
- 锚点增量=0：本门禁 §3 第 2 项（受 §6 所列 AST 绕过限制）。
- 发版门禁：本 workflow 在 PR/push/dispatch 时运行；与发版链的硬联动尚未建立，
  不应描述为「红了即不发」。
- fail-closed 自证：本门禁 §3 第 3 项变异测试。

---

## 5. 落地步骤

### 5.1 接入状态

以下文件位于 `Dharness` 仓库：

```
.github/workflows/seam-security.yml   # workflow
scripts/seam-snapshot.mjs             # 快照：import 桥模块 + 源码探针
scripts/check_seam_security.py        # 门禁 1
scripts/check_anchor_budget.py        # 门禁 2
golden/seam-baseline.json             # 安全快照基线，来源 v1.2.0
golden/anchor-budget.json             # 锚点预算基线
tests/test_seam_security_gate.py      # 7 项安全意图测试
tests/test_anchor_budget_gate.py      # 3 项锚点意图测试
SEAM_SECURITY.md                      # 本文件
```

截至复核时，上述门禁文件在 Dharness 工作树中仍处于 untracked 未提交状态，
GitHub 上尚未生效。生效的前置条件是提交到 `Dharness` 的 `main`。

提交示例（规范标题）：

```
ci: add seam security gate
```

### 5.2 上游更新时的处理决策链

上游 tag 更新 → `check_seam_security.py` 报差异时，按方向判断：

1. **新增 view 组件**（markdown/searchField 等）：本门禁快照不含组件清单，不报差异。
2. **effect 白名单/limits 真被放松**：这是攻击面扩大，默认不跟进该 tag。
   三条路：① 在上游提 issue/PR 收回；② 在自有 fork 打补丁收紧；③ 若确为安全改进
   （如阈值调小），人工确认后更新 golden 并提交，附理由。
3. **sourceGuards 失守**（如删掉 permission 硬拒）：`[HARD]`，禁止更新 golden 放行，
   必须先修上游源码或固定 patch。
4. **scopeKeys 变化**：新增原文类键 = 泄漏面扩大，按 2 处理；仅 id 类键扩展需人工确认。

更新 golden 的方式：确认变更安全后，用新上游源码重新生成并提交：

```
node scripts/seam-snapshot.mjs <新上游源码目录> > golden/seam-baseline.json
# 人工逐项 diff 确认无放松后提交
```

golden 变更必须伴随理由记录，不得仅为让 CI 变绿而改 golden。

### 5.3 已知边界

- 本门禁检查「可直接观测的安全常量与白名单是否变化」，不保证上游新增功能都符合
  架构意图（那靠设计 §7 的上游提案 + fork cherry-pick 流程）。
- 源探针基于源码文本；上游若大幅重构桥文件结构，探针可能失效。§3 第 3 项自测
  与 golden 来源 commit 记录用于发现这种情况，但不覆盖语义等价改写。
- 完整编译/真机验证仍需 `autonomous-release` 的 build job 与外部环境（本机 proot 无
  Flutter toolchain）。

---

## 6. 独立复核与修复记录

依据：`/root/workspace/_scratch/seam-gate-independent-review.md`（2026-11-20 独立复核）。
复核对新旧源码做语义等价变异、独立复跑门禁并记录退出码。以下结论为复核事实引用，
修复状态以代码与测试为准。

### 6.1 复核确认成立的部分

- golden 快照与真实 v1.2.0 源码逐字节一致，来源真实。
- 对朴素的数值放大、白名单新增、scope 原文键新增、硬拒语句整行删除，门禁确实
  拒绝放行（已实测）。
- workflow 只读、无 Secret、`permissions: contents: read`，两处 checkout
  `persist-credentials: false`，YAML 可被 pyyaml 解析。

### 6.2 复核发现的问题与修复方向

| 问题 | 复核证据 | 修复方向 |
|---|---|---|
| 假绿：4 项 sourceGuards 为脆弱文本正则，保留匹配字面串即可放松语义（https-only / 无凭据 / permission 硬拒 / 降级阈值） | M2/M3/M4/D1 全部 rc=0 且探针仍 true | 改为导入模块后做行为探针，直接调用 `validate()`/`pickerEffect()`/`normalize()` 断言违规语义 |
| 快照覆盖不全：只取 `LIMITS` 的 10 个字段，`insertChars`/`copyChars`/`toolCard*`/stream 系列、`CAP_LIMITS`/`FILE_LIMITS` 全漏 | G1/G2/G3 rc=0 | 快照纳入全部安全相关预算 |
| 语义链路盲区：`routes.js:366` 的 `userGesture` 传值、`capabilities.js:131,161` 的 `unknown()` 无探针 | R1/P2 rc=0 | 增加链路与 helper 的探针或行为断言 |
| 锚点预算至少 5 类结构性绕过：无条件 append、复合条件、新 flag 藏 compound、`plan.extend`/`plan.insert`、helper 间接调用 | A1–A5/A7 rc=0 | 修 AST 计数：统计所有 `plan.{append,extend,insert}` 与间接调用，识别复合条件与无条件 append |
| HARD 标记误导：翻转的 guard 在差异表显示 `[REVIEW] ... (未知)`、hard_count=0 | `check_seam_security.py` 差异表 | 修正叶子路径的 HARD 判定与方向标记 |
| 与发版链未联动：与 `autonomous-release.yml` 无 `needs`/`workflow_run` 依赖，被检 ref 与发版 ref 可能不同 | `grep -rn seam .github/` 仅命中自身 | 把 seam 检查并入 `autonomous-release` build job，对同一个 `$TAG` 在应用补丁/构建前运行 |
| `workflow_dispatch.upstream_tag` 无 `v*` 校验、默认 `main` 与发版「最新 release tag」语义不一致 | dispatch 输入无格式校验 | 增加格式校验并统一 ref 语义 |
| 尚未提交：全部门禁文件为 untracked，GitHub 上未生效 | 复核前后 `git status --short` 均为 `??` | 提交到 `main` 并使 workflow 生效 |
| 文档结论强于实际能力：原文声称「三道门禁全绿」「fail-closed 已验证」 | 对比 Q3/Q4 实测 | 本文已改为与复核一致的中性表述 |

### 6.3 修复状态

脚本、workflow 与测试的修订由相关工作项推进；本文按「修复后的预期状态」描述，
具体能力以代码与测试为准，不以本文表述作为验收依据。复核中列出的假绿、漏字段、
锚点绕过、不联动、未提交等问题，只有在对应代码与测试落地后才视为解决。

### 6.4 位置裁决（引自复核）

安全不变量定义在 dsh-go 桥源码，主防线应在 dsh-go 源码侧（PR 保护 / CODEOWNERS /
桥自带 golden 测试）。Dharness 侧适合作为纵深防御的第二道闸，须与发版链硬联动、
使用与发版相同的 ref。仓库归属（是否 fork dsh-go）对用户仍是开放问题，本文不作
预设。

---

## 7. 验证记录

以下为复核时的本地实测记录，非 CI 全绿结论：

| 检查 | 结果 |
|---|---|
| 快照 6 项 sourceGuards（未变异源码） | 全 true（修复 1 个自身正则误报后） |
| golden 正常路径 | rc=0 放行 |
| 朴素放松：数值放大 / 白名单扩大 / scope 原文键 / 删硬拒 | rc≠0 拒绝 |
| 语义等价放松（M2/M3/M4/D1/P1b/G1/G2/G3/R1/P2） | rc=0 假绿，详见 §6.2 |
| 锚点预算正常路径 | rc=0，9 个 flag 与基线一致 |
| 直接 `if flag:` + 直接 `plan.append` 的锚点增量 | rc≠0 拒绝 |
| 锚点结构性绕过（A1–A5/A7） | rc=0 假绿，详见 §6.2 |
| workflow YAML | pyyaml 解析通过，1 job / 8 steps |
| CI 顺序全跑 | 仅在本地临时副本上跑通；门禁对语义放松存在假绿，不构成全绿结论 |

修复后的验证结果以更新后的代码与测试输出为准。
