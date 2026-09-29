# 贡献

## 贡献面

你在仓库结构里看到的分法，**就是**贡献面：

**后端各阶段**在 `backend/` 下，是一个 FastAPI 包。每个阶段 —— `prd`、`arch`、
`test_design`、`tasks`、`execution`、`verification` —— 有自己的生成器 / 执行器模块，
在 `backend/routes/` 下有各自的 HTTP 路由。改接口就**一起**改路由和生成器：背后
没有生成器的路由会在下一次请求返回 `501`，而契约测试已经钉住了这一点。

**UI** 是 `frontend/` 下的静态 SPA —— `index.html`、`app.js`、`style.css`，外加一个
小包装文件 `frontend/api.js`。**没有构建步骤**：改完文件刷新浏览器即可。每一次 fetch
都要走[运行它](../operations/running.md)里说的包装规则；绕过包装的新视图代码，既过
不了运行时预检，也过不了
`backend/tests/static_gates/test_frontend_uses_api_wrapper.py`。

**测试套件**全部在 `backend/tests/` 下。它怎么切分、你的改动属于哪条 lane，见
[测试](testing.md)。

## 一次改动的流程

1. **读拥有这块区域的产物。** 你要动的那个阶段的 PRD 章节；如果 PRD 早于它，读设计
   说明。
2. **判断这个改动属于哪个阶段。** 各阶段互相承重：改了 PRD 生成却没有配套的验收测试，
   会**通过 `pytest` 而依然是错的**，因为验证是拿运行中的代码去比前面阶段产出的文档。
3. **先写测试。** 关于仓库本身的规则用 `backend/tests/static_gates/`；生成器行为用
   对应的 `backend/tests/unit/` 模块；新端点用契约套件。
4. **实现它**，保持新测试为绿，然后跑完 unit lane 的其余部分确认没有别的东西动了。
5. **在 topic 分支上提交。** CI 的 unit lane 是合并门；integration 和 e2e lane 只在
   `main` 上夜间跑。

## 提交信息

提交信息可以写**改了什么、为什么**。它不可以把一个 AI 系统写成共同作者：

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>    ← 会被拒绝
```

提交的人不可能知道这件事。工具也许确实是 Claude Code，但某一次提交背后**是哪个模型**
是别处做的路由决定，提交里并没有记录 —— 写上它，等于用一个陈述事实的句式把一个猜测
写进永久的公开历史，而它在那里无法更正。

*谈论*工具是可以的。检查器看的是那个署名尾注，不是正文，所以正文里随便讨论
Claude Code、provider 或模型都没问题 —— 人类共同作者的尾注也照常放行。

`scripts/check_commit_msg.py` 是唯一的实现，有三条路径会走到它：`.git/hooks/commit-msg`
（由 `scripts/install_git_hooks.sh` 安装，或 `pre-commit install --hook-type commit-msg`）、
`.pre-commit-config.yaml` 里的 `commit-msg` 阶段、以及 CI 里针对推送区间的检查 ——
所以 `git commit --no-verify` 绕不过去。

## 关于名字的规则 {#the-rule-about-names}

如果你的改动引入了外部 provider，**不要**在模板上给它命名。一串真实名字属于部署
机器上的 `PDT_PROVIDER_*` 配置。写进 `example/` 等于把一个安装的 provider 列表编译
进每一次安装的 onboarding —— 见[配置](../operations/configuration.md)。

同样的推理也是本仓不分发任何操作者专属路径、本地 checkout 名字、以及对个人的归属的
原因。门禁管住了其中机械的那部分
（`test_no_local_home_path_in_first_party.py`、
`test_no_operator_attribution_in_source.py`），但规则比门禁更宽：
**写对软件成立的东西，不要写对你的机器成立的东西。**

## 文件该放哪里

`scripts/` 放的是**对软件成立**的脚本 —— CI（`.github/workflows/`）、pre-commit
钩子、以及开发者的 shell 会跑的那些。任何只对*一次运行*或*一台机器*成立的东西都
不放在那里：一次性的迁移、写本安装 nightly 产物的辅助脚本、只会跑两次的草稿脚本。
那些属于 `.pdt/` —— 它被 gitignore，而且本来就装着其余的本地运行态。

这是[关于名字的那条规则](#the-rule-about-names)用在*目录*上而不是名字上，也正是
它让这棵树保持可读：打开 `scripts/` 的人应该能跑其中任何一个。这个目录还以别的目录
没有的方式承重 ——
`backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py`
把里面每个脚本都当作 CI 会执行的 shell，而这个断言只有在目录里确实全是 CI 的东西时
才成立。

如果一个脚本*确实*通用 —— 别的安装也会想跑它 —— 那它属于 `scripts/`，并且 README
的目录表应该写出它。那份地图由 README 承载：每个目录是干什么的，以及哪些目录是本地
运行态而不是产品。塞不进一行表格的细节，写在它所属领域的页面上，就像这一页。

## 文档

你正在读的这个站是用 MkDocs 从 `docs/` 构建的：

```bash
backend/.venv/bin/python3 -m pip install -r docs/requirements.txt
backend/.venv/bin/python3 -m mkdocs serve     # http://127.0.0.1:8000
backend/.venv/bin/python3 -m mkdocs build --strict
```

`--strict` 就是门禁。nav 指向不存在的页面、或者链接指向不存在的页面，都会非零退出
—— 而 CI 在每个触及 `docs/` 或 `mkdocs.yml` 的 PR 上都跑这一条。

中英双语：`index.md` 是英文，`index.zh.md` 是它对应的中文；`nav:` 里**只引用不带
语言后缀的那个文件**，由插件按语言分发。新增一页时两侧一起加。

这里的 Markdown 是朴素的，在 github.com 上同样渲染，所以阅读文档并不依赖站点被构建。

## 注释

注释可以说**代码做什么、为什么**。它不可以引用某个人、点名一个兄弟仓、或者叙述
某台机器上发生过什么。

最后这条区分值得明说，因为写的时候两者读起来几乎一样：

- *"这个路径用了 `.parent.parent`，落到了错误的目录，因为每个调用方都从自己的
  `__file__` 重新推导了一遍"* —— 一个**缺陷**。任何人读代码都能重新得出它。写下来。
- *凡是叙述「跑它的那台机器上积了多少、积了多久、横跨多长窗口」的句子* —— 一次
  **事故**。没人能从源码重新得出它。不要写进公开文档。

第二条刻意写得很抽象，原因值得读两遍：早先的版本用了一个写得很真实的句子来举例，
而那等于把规则禁止的东西又发表了一次 —— 讲「不要泄露事故」的这一页，自己泄露了
一个。`backend/tests/static_gates/` 下的门禁认的是**形状**，所以它也会拦住你的；
请描述事故，永远不要引用它。

分界线就一句：**一个陌生人读了代码，能不能自己得出这个结论？** 能，它属于这个仓库；
不能，它不属于。
