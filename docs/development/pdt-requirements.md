# 部署级「操作者要求」机制（`PDT.md` 家族）

## 这份机制解决什么

仓库里原本只有一份**项目级**的 AI 使用说明：它跟着项目走 —— 换一台机器、
换一个 checkout，内容就变了；而且没有任何地方能放「这台部署上，所有阶段都要
遵守的额外要求」。

本机制提供的就是那个位置：**部署根目录**下的要求文件被读出来，渲染成一个提示
块，注入到每次 LLM 调用的 system 提示**最前**。要求文件由这台部署的维护者负责，
框架只做两件事：把文本读出来、把文本包成提示块。框架**不解析内容、不按关键词
特判、不替要求做决定** —— 一条要求该怎么执行，是要求文件自己的事。

## 三层文件与优先级

部署根目录（运行期取 `server._PROJECT_ROOT`，即仓库根）下最多有三个文件生效：

| 层级 | 文件名 | 作用范围 |
| --- | --- | --- |
| base | `PDT.md` | 所有 scene 都吃 |
| stage | `PDT.<stage>.md` | 该 stage 下的所有 scene |
| scene | `PDT.<scene>.md` | 只对该 scene 生效 |

拼接顺序固定为 **base → stage → scene**，冲突时**后者覆盖前者**。三层各自独立
可缺：缺哪层都不报错，只是不生效；三层都不存在时机制整体不生效（见下文
no-op）。

本版**没有**计划级（plan-level）文件。四层叠加的合并顺序不可预测，比"少一层"
更难排查 —— 少一层只是少一条要求，多一层却可能悄悄改变同一条要求的生效范围。

### scene 与 stage 的对应关系

stage 由 scene 查表得到。映射表是全仓**唯一**的一张表
（`backend/pdt_requirements.py` 里的 `STAGE_BY_SCENE`），要改只改那一处。

| stage | scene |
| --- | --- |
| `plan` | `prd`、`prd_refine`、`arch`、`arch_refine`、`test_design`、`test_refine`、`tasks_generation`、`interview`、`planning`、`refiner` |
| `execution` | `execution`、`service_restart` |
| `verification` | `verification`、`verification_plan_delta`、`verification_command_audit`、`audit_second_pass` |

`summarizer` 故意不在表里：它不属于任何阶段，只吃 base。**未知 scene 同样只吃
base**（安全回落，不报错）—— 认不出来的 scene 不该让整条链路失败。

当某个 scene 的名字与它的 stage 同名时，stage 文件与 scene 文件是同一份，只会
加载一次。

## 不可覆盖的红线

块里的第一段实质内容是一条**红线清单**，它由**代码写死**，不来自要求文件：

> [红线] 不得伪造或篡改证据/测试结论；不得跳过或禁用验证；不得擅自 push、合入
> 或对外发布；不得修改共享环境（生产 venv、其它 worktree、全局配置）。

因此要求文件**可以补充要求，却不能把红线删掉、改写或顶到后面去**：红线永远
排在块内最前，且不参与"后者覆盖前者"的拼接 —— 内容与红线冲突时红线优先。

红线是元层面的框架表态，不是某条具体策略。要求文件写什么都不能覆盖它，也
不能把它从提示里挤走。

## 长度上限：每个文件 8192 字节

单个要求文件超过 **8192 字节**（8 KB）时：

* 内容被**截断**到 8192 字节以内，且不切在多字节字符中间（不产生乱码）；
* 记一条 WARNING 日志（含文件名与实际字节数）；
* **不抛异常** —— 要求文件超长不该让一次 LLM 调用整个失败。

所以"要求文件超长"的表现是"要求被截断 + 日志里有告警"，不是报错。

## 缓存：按 mtime 判失效

读取结果按 `(路径, mtime, 大小)` 缓存：

* 文件没变 → 命中缓存，不重读盘；
* 文件改了（mtime 变化）→ 下一次调用即失效并重读。

也就是说：**改完要求文件立即生效，不需要重启任何进程**。

## 没有生效文件时是 no-op

三个文件都不存在（或都没有可读内容）时，注入是**逐字节 no-op**：

* system 提示原样返回，一个字符都不改，连一个多余换行都不加；
* `None` 进、`None` 出；
* 日志里不写生效文件相关的字段 —— 不写一对空值，因为空值会被读成"生效了，
  只是空的"。

机制在"没有要求文件"时不该在 system 提示上留下任何痕迹。

## 注入位置与幂等

块被放在 system 提示的**最前**（在原始 system 提示之前），因为这些要求是
"所有阶段都要遵守"的：

    <pdt_user_requirements source="PDT.md,PDT.plan.md">
    <优先级声明>
    <空行>
    [红线] …
    <空行>
    --- PDT.md ---
    <base 层内容>
    --- PDT.plan.md ---
    <stage 层内容>
    </pdt_user_requirements>

* 优先级声明是固定的第二句：内容由这台部署的维护者落盘，优先级高于框架默认
  行为；多层按 base → stage → scene 拼接，后者在冲突时覆盖前者；
* 注入**幂等**：system 提示里已经有块时原样返回，不叠加第二份；
* 有生效文件但传入的 system 提示为空或 `None` 时，返回块本身，块后不加多余
  换行 —— 否则不传 system 提示的调用点会静默跳过整个机制；
* 注入点是**统一的**：provider 侧在发起调用前调用同一个注入入口
  （`backend/coding_tool.py` 的 `CodingTool._apply_pdt_requirements()`），
  所以注进去的那块在任何一条 provider 路径上都是同一块。

## 可见性：日志里能看到本轮生效了哪些文件

每次 LLM 调用的上下文日志记录两件事：

* `pdt_requirements_files`：本轮生效的文件（相对部署根目录），按
  base → stage → scene 顺序；
* `pdt_requirements_sha256`：渲染出来的块的 sha256。

两个字段取自**本轮真正注入的那一块**，所以"日志里的哈希"与"注进去的内容"永远
一致；没有生效文件时这两个字段不写。

## 样例：先看 `example/`

仓库的 `example/` 目录里放了四份样例（与仓内其它 `.example` 配置样例同一约定）：

* `example/PDT.md.example`
* `example/PDT.plan.md.example`
* `example/PDT.execution.md.example`
* `example/PDT.verification.md.example`

**用法**：把样例**复制到仓库根**，并**去掉 `.example` 后缀** —— 即
`example/PDT.md.example` 变成仓库根下的 `PDT.md`，`example/PDT.plan.md.example`
变成仓库根下的 `PDT.plan.md`，其余两份同理。只有去掉后缀后的文件名才符合
运行期的命名规则；仍带 `.example` 后缀的文件不会被加载。

样例是起点，不是成品：里面写的是通用做法，具体要求由这台部署自己改。

## 真实要求文件不入库

仓库根下的真实要求文件描述的是**这台部署**的策略，属于本机配置，不要提交
入库。`example/` 下的样例带 `.example` 后缀，不会被运行期的文件名规则匹配，
可以放心留在版本库里。

## 源码位置

| 关注点 | 位置 |
| --- | --- |
| 加载层：文件名推导、8192 字节截断、mtime 缓存 | `backend/pdt_requirements.py` |
| 渲染层：块形状、红线、`inject()` / `describe()` | `backend/pdt_requirements.py` |
| 注入接入点 | `backend/coding_tool.py` |
| 本文档的契约测试 | `backend/tests/unit/test_pdt_requirements_docs.py` |
