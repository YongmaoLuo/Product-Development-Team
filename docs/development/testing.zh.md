# 测试

有一条规则优先于其他所有建议：**用 `backend/.venv` 跑测试套件，工作目录放在仓库
根。** 原因在[快速开始](../getting-started.md)里 —— 系统 Python 是在 **collection**
阶段失败的，不是在某个更靠后、更好解释的地方。

## 各条 lane

```bash
# 默认 CI lane。-m unit 会跳过 slow / integration / e2e / real_model 标记；
# 缺的模型由 backend/tests/conftest.py 装的桩满足。
backend/.venv/bin/python3 -m pytest backend/tests -m unit -q

# 集成：起服务并通过 HTTP 跟它说话。不需要真实模型，但需要一个空闲回环端口。
backend/.venv/bin/python3 -m pytest backend/tests -m "not slow" -q

# 端到端：走 dry-run 假后端跑完整计划生命周期。按测试设计的决定，只在 main 跑。
backend/.venv/bin/python3 -m pytest backend/tests -m e2e -q

# 单个模块，输出拉满
backend/.venv/bin/python3 -m pytest backend/tests/<file>.py -xvs
```

!!! tip "优先用包装脚本"
    `scripts/run_tests.sh` 是规范入口。它解析 venv、把目标默认成 `backend/tests`，
    并且不让**标志**改变**跑哪些**测试 —— 只传标志的调用曾经切换过套件，因为
    pytest 是按当前目录解析 inifile 的，而本仓有两个 addopts 不同的 `pytest.ini`。

    以管道结尾的脚本（`run_tests.sh | tail`）报出的是**管道**的退出码，不是 pytest
    的。真要判断时，别带管道看 `$?`。

## 两条值得钉住的 collection 规则

- **`pytest.ini` 的 collection 对目录敏感。** 在 `backend/` 下 pytest 读
  `backend/pytest.ini`（带 `--strict-markers`，所以标记名打错会**响亮失败**而不是
  静默匹配到零个）；在仓库根读根目录的 `pytest.ini`。选一个、整个 run 都待在那儿
  —— 两个文件枚举的标记相同，但 addopts 不同。
- **`backend/.venv` 是唯一能让测试干净导入的解释器。**

## 套件怎么组织

| 目录 | 放什么 |
|---|---|
| `unit/` | 单个模块的行为，隔离验证 |
| `integration/` | 把服务起起来，走 HTTP 驱动 |
| `e2e/` | 对着假后端跑完整计划生命周期 |
| `security/` | 攻击路径断言 |
| `contract/` | 调用方可以依赖的 HTTP 表面 |
| `meta_tests/` | 对审计文档本身的门禁 |
| `static_gates/` | 对**源码树**的门禁 |

最后两类是最让人意外的，也是贡献之前值得先搞懂的：

- **`meta_tests/`** 解析安全审计文档并断言它的形状 —— 每条发现都有必需元素、覆盖
  网格没有空格、引用表能解析。那份审计文档是**构建输入**，不是碰巧放在仓库里的一篇
  记录。
- **`static_gates/`** 审计**仓库本身**：前端是否都走了 API 包装、源码里有没有出现
  操作者私有路径、带凭据的文件是否被私有地写出、状态库路径是否只有一个解析器。
  它们大多存在，是因为被检查的那件事**已经坏过一次** —— 门禁的 docstring 会写清
  它来自哪次事故。

一个什么都不扫的门禁会**空洞地通过**，所以 `static_gates` 里的测试通常会带一条
"扫描结果非空"的断言，以及一条灵敏度测试，证明扫描器仍然能匹配它当初为之而写的
形状。你动一个门禁时，把这些一起保留。

## 写一个能活下来的门禁

有两种失败模式会杀死门禁，现有的那些都是为了同时避开两者而写的：

- **噪音。** 一个会对合法代码误报的扫描器，会被下一个被它烦到的人删掉。写的时候要
  把**反例**也写成测试 —— 那些**不该**匹配的形状 —— 而不只是正例。
- **自引用。** 门禁不能包含它自己要禁的那个字面量，否则它会匹配到自己的定义。现有
  门禁都用字符串片段拼接来构造模式，正是出于这个原因；在这条约定被采用之前，好几个
  门禁是被自己的断言抓出来的。
