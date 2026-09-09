"""AkShare 接口契约测试：验证已接入接口「可调用 + 关键字段存在」。

AkShare 是爬虫库，上游（东方财富等）页面改版会导致接口静默失效
（不报错，返回空 DataFrame 或错列）。本测试每天跑一次，能第一时间
定位是哪个接口烂了，而不是等到用户点按钮才暴露。

注意：契约测试只校验「接口可用 + 返回结构正确」，不校验具体取值
（网络/上游偶发抖动不应让测试变 flaky）。具体取值是否正确由端到端
生成流程（report.html 含真实数据）保证。
"""
import pytest

from fundamental_agent.data.akshare import AkShareClient

pytestmark = pytest.mark.contract


def test_akshare_importable():
    c = AkShareClient()
    assert c.available() is True


def test_get_identity_shape():
    """接口可调用，且返回 dict 含全部约定字段（值可为 None，键不能丢）。"""
    c = AkShareClient()
    ident = c.get_identity("600519")
    assert isinstance(ident, dict)
    for key in ("code", "name", "industry", "board", "list_date", "total_mv",
                "float_mv", "holders_count", "employees", "controller"):
        assert key in ident, f"get_identity 缺少字段 {key}"


def test_get_identity_board_local():
    """上市板块为纯本地推断（零网络），必须稳定正确。"""
    c = AkShareClient()
    assert c.get_identity("600519")["board"] == "沪市主板"
    assert c.get_identity("300184")["board"] == "创业板"
    assert c.get_identity("688001")["board"] == "科创板"
