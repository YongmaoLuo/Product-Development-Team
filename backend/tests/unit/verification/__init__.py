"""backend/tests/unit/verification — 验证子系统的单元测试。

A1′ 相关性判定内核（:mod:`verification.topic_relevance`）的测试在这里，
零 mock：判定内核是纯函数，唯一的外部依赖由调用方注入，所以每个用例塞一个
普通可调用对象进去就能断言「批量只调一次」这类契约。
"""