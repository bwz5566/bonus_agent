import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "verify: 走完整的独立验算（脚本里要写 verify 调用）")


@pytest.fixture(autouse=True)
def _scripted_runs_skip_verify(request, monkeypatch):
    """多数测试用写死的脚本模拟模型，测的是别的功能，脚本里没有独立验算这一步：默认跳过结束前的验算要求。
    独立验算本身由标了 @pytest.mark.verify 的测试覆盖。"""
    if request.node.get_closest_marker("verify"):
        return
    from bonus_agent.agent import Workspace
    monkeypatch.setattr(Workspace, "_verify_gaps", lambda self: [])
