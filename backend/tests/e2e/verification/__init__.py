"""backend/tests/e2e/verification — 验证子系统的端到端契约测试。

这个包里的用例起**真的**东西：真的子进程、真的 git 仓库、真的 plan 目录。
钉的是对外的契约，不是内部实现 —— 读源码能证明「代码写成了这样」，不能
证明「跑起来是这样」。

为什么这个目录必须和它的父目录一起是包
----------------------------------------

``backend/tests/e2e/`` 与它的兄弟目录（``unit`` / ``integration`` /
``meta_tests``）一样带 ``__init__.py``。这不是形式：pytest 的
``resolve_package_path`` 从测试文件往上找 ``__init__.py``，找到哪一层就以
哪一层作为包根。少了 ``backend/tests/e2e/__init__.py``，这个目录里的测试
会以**顶层** ``verification.test_xxx`` 的名字被导入 —— 而 ``backend/`` 下有
一个同名的生产包 ``backend/verification``。两者抢同一个名字，被导入的顺序
一旦反过来，测试就收集不到（``No module named 'verification.test_xxx'``），
或者生产包的公开接口被顶掉（``cannot import name 'VerificationOrchestrator'
from 'verification'``）。

父目录是包时，本目录的模块名是 ``tests.e2e.verification.test_xxx``，
顶层 ``verification`` 仍然是生产包 —— 两边不再抢名字。
"""