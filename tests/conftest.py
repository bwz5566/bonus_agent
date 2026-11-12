import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "verify: 走完整的独立验算（脚本里要写 verify 调用）")
    config.addinivalue_line("markers", "reread: 走完整的独立复读（夹具需提供全部方案独立算式）")
    config.addinivalue_line("markers", "evidence: 检查参数来源和精确位置绑定")


@pytest.fixture(autouse=True)
def _scripted_runs_skip_verify(request, monkeypatch):
    """多数测试用写死的脚本模拟模型，测的是别的功能，脚本里没有独立验算这一步：默认跳过结束前的验算要求。
    独立验算本身由标了 @pytest.mark.verify 的测试覆盖。"""
    from bonus_agent.agent import Workspace
    if not request.node.get_closest_marker("verify"):
        monkeypatch.setattr(Workspace, "_verify_gaps", lambda self: [])
    if not request.node.get_closest_marker("reread"):
        monkeypatch.setattr(Workspace, "_reread_gaps", lambda self, skipped: [])
    if not request.node.get_closest_marker("evidence"):
        monkeypatch.setattr(Workspace, "_binding_gaps", lambda self: [])
