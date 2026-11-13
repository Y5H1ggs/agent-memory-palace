"""pytest 入口：把剧本式自测接进 pytest / CI。

`memory_palace/demo.py` 是这份项目真正的测试套件（215 项断言 + 18 条突变审计），
它自带 `check()`，失败时抛 AssertionError。这里只是把它包成一个 pytest 用例，
让 `pytest`、IDE、CI 都能直接跑，不必另外维护第二套断言。

    pytest -q            # 约 1 秒
    python -m memory_palace.demo   # 等价的直接运行方式（能看到每一幕的输出）
"""
from memory_palace import demo


def test_selfcheck_suite() -> None:
    demo.main()
