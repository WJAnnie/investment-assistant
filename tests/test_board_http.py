# -*- coding: utf-8 -*-
"""Tests for pandas-free EastMoney board HTTP client.

Uses injected fake sessions; never touches the real network.
Verifies contract compliance with AkShareIndustryProvider without importing pandas/akshare.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from app.market.board_http import EastMoneyBoardClient, RecordList
from app.market.global_markets import GlobalMarketDataError


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    """Bounded, test-only HTTP response."""

    def __init__(self, body: bytes | str = b"", status_code: int = 200) -> None:
        if isinstance(body, str):
            self._body = body.encode("utf-8")
        else:
            self._body = bytes(body)
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size: int | None = None):
        step = chunk_size or 65536
        for start in range(0, len(self._body), step):
            yield self._body[start : start + step]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Record call metadata and return configured FakeResponse without I/O."""

    def __init__(
        self,
        body: bytes | str | None = None,
        status_code: int = 200,
        error: Exception | None = None,
        responses: list[Any] | None = None,
    ) -> None:
        self.body = body if body is not None else b""
        self.status_code = status_code
        self.error = error
        self.responses = list(responses) if responses is not None else None
        self.calls: list[dict[str, Any]] = []
        self.trust_env = True

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if self.responses is not None and len(self.responses) > 0:
            resp = self.responses.pop(0)
            if isinstance(resp, FakeResponse):
                return resp
            if isinstance(resp, tuple):
                return FakeResponse(resp[0], resp[1])
            return FakeResponse(resp, 200)
        return FakeResponse(self.body, self.status_code)


F124 = 1789544372
EXPECTED_ISO_TIME = "2026-09-16T15:39:32+08:00"

EXPECTED_BOARD_KEYS = {
    "板块代码",
    "板块名称",
    "涨跌幅",
    "换手率",
    "上涨家数",
    "下跌家数",
    "近5日涨跌幅",
    "近20日涨跌幅",
    "数据时间",
}

EXPECTED_KLINE_KEYS = {
    "日期",
    "收盘",
}


def test_list_normal_diff_as_list():
    payload = {
        "rc": 0,
        "data": {
            "total": 2,
            "diff": [
                {
                    "f12": "BK0475",
                    "f14": "半导体",
                    "f3": 2.35,
                    "f8": 3.12,
                    "f104": 80,
                    "f105": 15,
                    "f109": 1.25,
                    "f110": 4.56,
                    "f124": F124,
                },
                {
                    "f12": "BK0476",
                    "f14": "软件开发",
                    "f3": -1.20,
                    "f8": 2.45,
                    "f104": 40,
                    "f105": 60,
                    "f109": -0.50,
                    "f110": 2.10,
                    "f124": F124,
                },
            ],
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    result = client.stock_board_industry_name_em()

    assert isinstance(result, list)
    assert isinstance(result, RecordList)
    assert hasattr(result, "to_dict")

    records = result.to_dict(orient="records")
    assert isinstance(records, list)
    assert len(records) == 2

    for row in records:
        assert set(row.keys()) == EXPECTED_BOARD_KEYS

    row0 = records[0]
    assert row0["板块代码"] == "BK0475"
    assert row0["板块名称"] == "半导体"
    assert row0["涨跌幅"] == 2.35
    assert row0["换手率"] == 3.12
    assert row0["上涨家数"] == 80
    assert row0["下跌家数"] == 15
    assert row0["近5日涨跌幅"] == 1.25
    assert row0["近20日涨跌幅"] == 4.56
    assert row0["数据时间"] == EXPECTED_ISO_TIME

    # Check request headers and query string
    assert len(session.calls) == 2
    call0 = session.calls[0]
    assert call0["params"] is None
    assert "push2delay.eastmoney.com" in call0["url"]
    assert "pn=1" in call0["url"]
    assert "pz=100" in call0["url"]
    assert "fs=m:90+t:2" in call0["url"]
    assert "fields=f12,f14,f3,f8,f104,f105,f109,f110,f124" in call0["url"]
    assert call0["headers"].get("Referer") == "https://quote.eastmoney.com/"
    assert call0["headers"].get("User-Agent") == "investment-assistant-public-trial/1.0"

    call1 = session.calls[1]
    assert "pn=2" in call1["url"]


def test_list_normal_diff_as_dict():
    payload = {
        "rc": 0,
        "data": {
            "total": 1,
            "diff": {
                "0": {
                    "f12": "BK0475",
                    "f14": "半导体",
                    "f3": "2.35",
                    "f8": "3.12",
                    "f104": "80",
                    "f105": "15",
                    "f109": "1.25",
                    "f110": "4.56",
                    "f124": F124,
                }
            },
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    result = client.stock_board_industry_name_em()

    records = result.to_dict(orient="records")
    assert len(records) == 1
    assert set(records[0].keys()) == EXPECTED_BOARD_KEYS
    assert records[0]["板块代码"] == "BK0475"
    assert records[0]["板块名称"] == "半导体"
    assert records[0]["涨跌幅"] == 2.35
    assert records[0]["换手率"] == 3.12
    assert records[0]["上涨家数"] == 80
    assert records[0]["下跌家数"] == 15
    assert records[0]["近5日涨跌幅"] == 1.25
    assert records[0]["近20日涨跌幅"] == 4.56
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME


def test_kline_normal():
    payload = {
        "rc": 0,
        "data": {
            "code": "BK0475",
            "name": "半导体",
            "klines": [
                "2026-08-01,1000.0,1050.0,1060.0,990.0,1000,10000,5.0,1.2,50.0,0.5",
                "2026-08-02,1050.0,1100.5,1120.0,1040.0,1200,13000,4.8,1.4,50.5,0.6",
                "20260803,1100.5,1150.0,1160.0,1090.0,1500,16000,4.5,1.5,49.5,0.7",
            ],
        },
    }
    session = FakeSession(json.dumps(payload))
    client = EastMoneyBoardClient(session=session)
    result = client.stock_board_industry_hist_em(
        symbol="BK0475",
        start_date="20260801",
        end_date="20260803",
        period="日k",
        adjust="",
    )

    assert isinstance(result, RecordList)
    records = result.to_dict(orient="records")
    assert len(records) == 3

    for row in records:
        assert set(row.keys()) == EXPECTED_KLINE_KEYS

    assert records[0] == {"日期": "2026-08-01", "收盘": 1050.0}
    assert records[1] == {"日期": "2026-08-02", "收盘": 1100.5}
    assert records[2] == {"日期": "2026-08-03", "收盘": 1150.0}

    # Verify query URL parameters
    assert len(session.calls) == 1
    url = session.calls[0]["url"]
    assert "secid=90.BK0475" in url
    assert "beg=20260801" in url
    assert "end=20260803" in url


def test_list_discard_missing_or_dash_counts():
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": "-",
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 20,
                    "f105": None,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0003",
                    "f14": "行业3",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": "",
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0004",
                    "f14": "行业4",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 20,
                    "f105": "-",
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0005",
                    "f14": "行业5",
                    "f3": 2.0,
                    "f8": 1.5,
                    "f104": 30,
                    "f105": 15,
                    "f109": 2.5,
                    "f110": 3.5,
                    "f124": F124,
                },
                {
                    "f12": "BK0006",
                    "f14": "行业6",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": "-",
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0007",
                    "f14": "行业7",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": None,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0008",
                    "f14": "行业8",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": "-",
                    "f124": F124,
                },
                {
                    "f12": "BK0009",
                    "f14": "行业9",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": None,
                    "f124": F124,
                },
            ]
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    result = client.stock_board_industry_name_em()
    records = result.to_dict(orient="records")

    assert len(records) == 1
    assert records[0]["板块代码"] == "BK0005"
    assert records[0]["板块名称"] == "行业5"
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME


def test_list_discard_missing_code_or_name():
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {"f14": "无代码", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": None, "f14": "空代码", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": "  ", "f14": "空格代码", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": "BK0001", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": "BK0002", "f14": None, "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": "BK0003", "f14": "   ", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
                {"f12": "BK0004", "f14": "有效行业", "f3": 1.0, "f8": 1.0, "f104": 10, "f105": 10, "f109": 1.0, "f110": 1.0, "f124": F124},
            ]
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    result = client.stock_board_industry_name_em()
    records = result.to_dict(orient="records")

    assert len(records) == 1
    assert records[0]["板块代码"] == "BK0004"
    assert records[0]["板块名称"] == "有效行业"
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME


def test_http_status_error_raises():
    session = FakeSession(body=b"Internal Server Error", status_code=500)
    client = EastMoneyBoardClient(session=session)

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_name_em()
    assert exc_info.value.code == "HTTP_STATUS_ERROR"

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_hist_em("BK0001")
    assert exc_info.value.code == "HTTP_STATUS_ERROR"


def test_redirect_blocked_raises():
    session = FakeSession(body=b"", status_code=302)
    client = EastMoneyBoardClient(session=session)

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_name_em()
    assert exc_info.value.code == "REDIRECT_BLOCKED"

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_hist_em("BK0001")
    assert exc_info.value.code == "REDIRECT_BLOCKED"


def test_malformed_json_raises():
    session = FakeSession(body=b"invalid json {{{", status_code=200)
    client = EastMoneyBoardClient(session=session)

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_name_em()
    assert exc_info.value.code == "MALFORMED_RESPONSE"

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_hist_em("BK0001")
    assert exc_info.value.code == "MALFORMED_RESPONSE"

    # Non-dict JSON payload
    session_non_dict = FakeSession(body=b"[1, 2, 3]", status_code=200)
    client_non_dict = EastMoneyBoardClient(session=session_non_dict)
    with pytest.raises(GlobalMarketDataError) as exc_info:
        client_non_dict.stock_board_industry_name_em()
    assert exc_info.value.code == "MALFORMED_RESPONSE"


def test_body_too_large_raises():
    large_body = b"x" * (1024 * 1024 + 1)
    session = FakeSession(body=large_body, status_code=200)
    client = EastMoneyBoardClient(session=session)

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_name_em()
    assert exc_info.value.code == "BODY_TOO_LARGE"

    with pytest.raises(GlobalMarketDataError) as exc_info:
        client.stock_board_industry_hist_em("BK0001")
    assert exc_info.value.code == "BODY_TOO_LARGE"


def test_empty_diff_and_klines_return_empty_frame():
    # Empty diff list
    client_empty_diff = EastMoneyBoardClient(session=FakeSession(json.dumps({"rc": 0, "data": {"diff": []}})))
    res1 = client_empty_diff.stock_board_industry_name_em()
    assert isinstance(res1, RecordList)
    assert len(res1) == 0
    assert res1.to_dict(orient="records") == []

    # Empty diff dict
    client_empty_dict = EastMoneyBoardClient(session=FakeSession(json.dumps({"rc": 0, "data": {"diff": {}}})))
    res2 = client_empty_dict.stock_board_industry_name_em()
    assert len(res2) == 0

    # data is None
    client_none_data = EastMoneyBoardClient(session=FakeSession(json.dumps({"rc": 0, "data": None})))
    res3 = client_none_data.stock_board_industry_name_em()
    assert len(res3) == 0

    # Empty klines list
    client_empty_kline = EastMoneyBoardClient(session=FakeSession(json.dumps({"rc": 0, "data": {"klines": []}})))
    res4 = client_empty_kline.stock_board_industry_hist_em("BK0001")
    assert isinstance(res4, RecordList)
    assert len(res4) == 0
    assert res4.to_dict(orient="records") == []

    # None klines
    client_none_kline = EastMoneyBoardClient(session=FakeSession(json.dumps({"rc": 0, "data": None})))
    res5 = client_none_kline.stock_board_industry_hist_em("BK0001")
    assert len(res5) == 0


def test_akshare_industry_provider_duck_typing_integration():
    from datetime import date, datetime, timezone
    from app.market.industries import AkShareIndustryProvider, SOURCE_AKSHARE_EASTMONEY_BOARD

    list_payload = {
        "rc": 0,
        "data": {
            "total": 1,
            "diff": [
                {
                    "f12": "BK0475",
                    "f14": "半导体",
                    "f3": 2.35,
                    "f8": 3.12,
                    "f104": 80,
                    "f105": 15,
                    "f109": 1.25,
                    "f110": 4.56,
                    "f124": F124,
                }
            ],
        },
    }
    klines_list = [
        f"2026-08-{i:02d},1000.0,{1000.0 + i},1050.0,990.0,1000,10000,1.0,1.2,50.0,0.5"
        for i in range(1, 26)
    ]
    # Replace the last one to be 2026-09-16
    klines_list[-1] = "2026-09-16,1020.0,1025.0,1030.0,1010.0,1000,10000,1.0,1.2,50.0,0.5"
    kline_payload = {
        "rc": 0,
        "data": {
            "code": "BK0475",
            "klines": klines_list,
        },
    }

    class MultiRouteSession:
        def __init__(self) -> None:
            self.trust_env = True
            self.calls: list[dict[str, Any]] = []

        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            self.calls.append({"url": url, **kwargs})
            if "clist/get" in url:
                if "pn=1" in url:
                    return FakeResponse(json.dumps(list_payload), 200)
                return FakeResponse(json.dumps({"rc": 0, "data": {"diff": []}}), 200)
            if "kline/get" in url:
                return FakeResponse(json.dumps(kline_payload), 200)
            return FakeResponse(b"", 404)

    client = EastMoneyBoardClient(session=MultiRouteSession())
    provider = AkShareIndustryProvider(
        client=client,
        clock=lambda: datetime(2026, 9, 16, 15, 30, tzinfo=timezone.utc),
    )
    result = provider.fetch(
        cutoff=datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc),
        market_date=date(2026, 9, 16),
    )

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.code == "BK0475"
    assert obs.name == "半导体"
    assert obs.source == SOURCE_AKSHARE_EASTMONEY_BOARD
    assert obs.return_1d_pct == 2.35
    assert obs.turnover_rate == 3.12
    assert obs.advancers == 80
    assert obs.decliners == 15
    assert len(result.errors) == 0


def test_board_http_module_closure_excludes_pandas_and_akshare():
    probe = textwrap.dedent("""
        import sys
        BLOCKED = ("pandas", "numpy", "akshare")
        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if any(name == item or name.startswith(item + ".") for item in BLOCKED):
                    raise RuntimeError("blocked heavy import: " + name)
                return None
        sys.meta_path.insert(0, Blocker())
        from app.market.board_http import EastMoneyBoardClient
        class _Session:
            trust_env = True
        EastMoneyBoardClient(session=_Session())
        leaked = sorted(item for item in BLOCKED if item in sys.modules)
        print("blocked", leaked)
        """)
    completed = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert "blocked []" in completed.stdout


def test_list_pagination():
    page1_payload = {
        "rc": 0,
        "data": {
            "total": 2,
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 2.0,
                    "f104": 10,
                    "f105": 5,
                    "f109": 3.0,
                    "f110": 4.0,
                    "f124": F124,
                }
            ],
        },
    }
    page2_payload = {
        "rc": 0,
        "data": {
            "total": None,
            "diff": [],
        },
    }
    session = FakeSession(responses=[json.dumps(page1_payload), json.dumps(page2_payload)])
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(session.calls) == 2
    assert "pn=1" in session.calls[0]["url"]
    assert "pn=2" in session.calls[1]["url"]
    assert len(records) == 1
    assert records[0]["板块代码"] == "BK0001"
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME


def test_list_max_pages_cap():
    page_payload = {
        "rc": 0,
        "data": {
            "total": 1000,
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                }
            ],
        },
    }
    # Session always returns non-empty diff
    session = FakeSession(body=json.dumps(page_payload))
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(session.calls) == 8
    assert "pn=1" in session.calls[0]["url"]
    assert "pn=8" in session.calls[7]["url"]
    assert len(records) == 1
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME


def test_list_deduplication_by_board_code():
    page1_payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 5,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0002",
                    "f14": "行业2-原版",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 10,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": F124,
                },
            ]
        },
    }
    page2_payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2-重复项",
                    "f3": 99.0,
                    "f8": 99.0,
                    "f104": 99,
                    "f105": 99,
                    "f109": 99.0,
                    "f110": 99.0,
                    "f124": F124,
                },
                {
                    "f12": "BK0003",
                    "f14": "行业3",
                    "f3": 3.0,
                    "f8": 3.0,
                    "f104": 30,
                    "f105": 15,
                    "f109": 3.0,
                    "f110": 3.0,
                    "f124": F124,
                },
            ]
        },
    }
    page3_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[
        json.dumps(page1_payload),
        json.dumps(page2_payload),
        json.dumps(page3_payload),
    ])
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(session.calls) == 3
    assert len(records) == 3
    codes = [r["板块代码"] for r in records]
    assert codes == ["BK0001", "BK0002", "BK0003"]
    assert records[1]["板块名称"] == "行业2-原版"
    assert records[1]["涨跌幅"] == 2.0
    assert records[1]["数据时间"] == EXPECTED_ISO_TIME


def test_list_request_url_host_is_push2delay():
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(json.dumps(empty_payload))
    client = EastMoneyBoardClient(session=session)
    client.stock_board_industry_name_em()

    assert len(session.calls) == 1
    recorded_url = session.calls[0]["url"]
    assert recorded_url.startswith("https://push2delay.eastmoney.com/api/qt/clist/get")
    assert "push2.eastmoney.com" not in recorded_url.replace("push2delay.eastmoney.com", "")


@pytest.mark.parametrize(
    "bad_f124,omit_key",
    [
        (None, True),
        ("-", False),
        ("", False),
        (None, False),
        (True, False),
        (1e30, False),
        ("abc", False),
        (999999999, False),
    ],
)
def test_list_discard_invalid_f124(bad_f124: Any, omit_key: bool):
    row = {
        "f12": "BK0001",
        "f14": "行业1",
        "f3": 1.0,
        "f8": 1.0,
        "f104": 10,
        "f105": 10,
        "f109": 1.0,
        "f110": 1.0,
    }
    if not omit_key:
        row["f124"] = bad_f124
    payload = {"rc": 0, "data": {"diff": [row]}}
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")
    assert len(records) == 0


def test_list_f124_formats_accepted():
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": "1789544372",
                },
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": 1789544372.0,
                },
            ]
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")
    assert len(records) == 2
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME
    assert records[1]["数据时间"] == EXPECTED_ISO_TIME
    assert records[0]["数据时间"] == records[1]["数据时间"]


def test_list_f124_iso_shanghai_offset():
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                }
            ]
        },
    }
    empty_payload = {"rc": 0, "data": {"diff": []}}
    session = FakeSession(responses=[json.dumps(payload), json.dumps(empty_payload)])
    client = EastMoneyBoardClient(session=session)
    records = client.stock_board_industry_name_em().to_dict(orient="records")
    assert len(records) == 1
    assert records[0]["数据时间"] == "2026-09-16T15:39:32+08:00"
    assert records[0]["数据时间"].endswith("+08:00")


def test_list_convergence_sweep1_divergent_sweep2_converges():
    """首轮不一致、次轮收敛：页1戳 T1、页2戳 T2，重抓后两页同为 T1。
    返回 2 行且所有 数据时间 相同；断言请求次数 == 页数 + 重抓页数。
    """
    t1_epoch = 1789544372
    t2_epoch = 1789544375
    expected_t1 = "2026-09-16T15:39:32+08:00"

    page1_payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2_divergent = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }
    page2_converged = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }

    session = FakeSession(responses=[
        json.dumps(page1_payload),
        json.dumps(page2_divergent),
        json.dumps(page2_converged),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=2)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    assert records[0]["数据时间"] == expected_t1
    assert records[1]["数据时间"] == expected_t1
    assert records[0]["数据时间"] == records[1]["数据时间"]
    # 请求次数 == 页数(2) + 重抓页数(1) = 3
    assert len(session.calls) == 3
    assert "pn=1" in session.calls[0]["url"]
    assert "pn=2" in session.calls[1]["url"]
    assert "pn=2" in session.calls[2]["url"]


def test_list_convergence_mixed_timestamps_in_single_page():
    """同一页内混戳：某页同时含 T1 与 T2 两行 → 该页被判为「不等于主导戳」并被重抓，最终收敛。"""
    t1_epoch = 1789544372
    t2_epoch = 1789544375
    expected_t1 = "2026-09-16T15:39:32+08:00"

    page_mixed = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                },
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                },
            ]
        },
    }
    page_fixed = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                },
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t1_epoch,
                },
            ]
        },
    }

    session = FakeSession(responses=[
        json.dumps(page_mixed),
        json.dumps(page_fixed),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=1)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    assert records[0]["数据时间"] == expected_t1
    assert records[1]["数据时间"] == expected_t1
    assert len(session.calls) == 2
    assert "pn=1" in session.calls[0]["url"]
    assert "pn=1" in session.calls[1]["url"]


def test_list_convergence_never_converges_bounded():
    """始终不收敛：始终返回两个不同戳 → 不抛异常，返回行中存在 2 个不同 数据时间，
    且请求次数精确等于可控页数下的上界。
    """
    t1_epoch = 1789544372
    t2_epoch = 1789544375

    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }

    # max_pages=2, max_sweeps=4:
    # Sweep 1: pn=1 (page1), pn=2 (page2) -> 2 requests
    # Sweep 2: pn=2 (page2) -> 1 request
    # Sweep 3: pn=2 (page2) -> 1 request
    # Sweep 4: pn=2 (page2) -> 1 request
    # Upper bound of requests for this 2-page scenario = 2 + (4 - 1) * 1 = 5
    session = FakeSession(responses=[
        json.dumps(page1),
        json.dumps(page2),
        json.dumps(page2),
        json.dumps(page2),
        json.dumps(page2),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=2, max_sweeps=4)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    timestamps = {r["数据时间"] for r in records}
    assert len(timestamps) == 2
    assert len(session.calls) == 5


def test_list_convergence_sweep1_consistent_no_refetch():
    """首轮即一致：请求次数恰好等于页数，没有任何重抓。"""
    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                }
            ]
        },
    }
    page2 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": F124,
                }
            ]
        },
    }

    session = FakeSession(responses=[json.dumps(page1), json.dumps(page2)])
    client = EastMoneyBoardClient(session=session, max_pages=2)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    assert records[0]["数据时间"] == EXPECTED_ISO_TIME
    assert records[1]["数据时间"] == EXPECTED_ISO_TIME
    assert len(session.calls) == 2


def test_list_convergence_boundary_single_page_and_empty_diff():
    """边界：仅一页且只有 1 行有效 → 1 次请求即收敛；diff 为空 → 返回 0 行且无异常。"""
    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": F124,
                }
            ]
        },
    }
    session1 = FakeSession(responses=[json.dumps(page1)])
    client1 = EastMoneyBoardClient(session=session1, max_pages=1)
    records1 = client1.stock_board_industry_name_em().to_dict(orient="records")
    assert len(records1) == 1
    assert records1[0]["数据时间"] == EXPECTED_ISO_TIME
    assert len(session1.calls) == 1

    empty_payload = {"rc": 0, "data": {"diff": []}}
    session2 = FakeSession(responses=[json.dumps(empty_payload)])
    client2 = EastMoneyBoardClient(session=session2)
    records2 = client2.stock_board_industry_name_em().to_dict(orient="records")
    assert len(records2) == 0
    assert len(session2.calls) == 1


def test_list_convergence_retry_empty_does_not_drop_rows():
    """重抓返回空 diff 时不丢弃已有行：两页戳不一致，重抓页2返回空 diff。
    断言返回行数 == 2（BK0001 与 BK0002 都在，页2 不得消失），且戳不唯一。
    """
    t1_epoch = 1789544372
    t2_epoch = 1789544375

    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }
    empty_page = {"rc": 0, "data": {"diff": []}}

    # max_pages=2, 默认 max_sweeps=4: Sweep 1 (2 calls) + 3 retries (3 calls) = 5
    session = FakeSession(responses=[
        json.dumps(page1),
        json.dumps(page2),
        json.dumps(empty_page),
        json.dumps(empty_page),
        json.dumps(empty_page),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=2)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    codes = {r["板块代码"] for r in records}
    assert codes == {"BK0001", "BK0002"}
    timestamps = {r["数据时间"] for r in records}
    assert len(timestamps) == 2


def test_list_convergence_retry_empty_then_recovers():
    """重抓先空后恢复：第一次重抓页2返回空、第二次重抓页2返回与 T1 一致的页 → 最终收敛为 2 行且戳全为 T1。"""
    t1_epoch = 1789544372
    t2_epoch = 1789544375
    expected_t1 = "2026-09-16T15:39:32+08:00"

    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2_divergent = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }
    page2_converged = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    empty_page = {"rc": 0, "data": {"diff": []}}

    # Sweep 1: page1, page2_divergent -> divergent_pages = [2]
    # Sweep 2 retry: empty_page -> 保留 page2_divergent -> 仍未收敛
    # Sweep 3 retry: page2_converged -> 收敛为 expected_t1
    session = FakeSession(responses=[
        json.dumps(page1),
        json.dumps(page2_divergent),
        json.dumps(empty_page),
        json.dumps(page2_converged),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=2)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    assert {r["板块代码"] for r in records} == {"BK0001", "BK0002"}
    assert records[0]["数据时间"] == expected_t1
    assert records[1]["数据时间"] == expected_t1
    assert len(session.calls) == 4


def test_list_convergence_retry_empty_keeps_retrying_bounded():
    """两页始终不一致且重抓始终为空：不抛异常、返回 2 行、请求次数严格有界。"""
    t1_epoch = 1789544372
    t2_epoch = 1789544375

    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }
    empty_page = {"rc": 0, "data": {"diff": []}}

    # max_pages=2, max_sweeps=4:
    # 请求次数严格有界 == 2 + (max_sweeps - 1) * 1 = 5
    session = FakeSession(responses=[
        json.dumps(page1),
        json.dumps(page2),
        json.dumps(empty_page),
        json.dumps(empty_page),
        json.dumps(empty_page),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=2, max_sweeps=4)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 2
    assert {r["板块代码"] for r in records} == {"BK0001", "BK0002"}
    assert len(session.calls) == 5
    timestamps = {r["数据时间"] for r in records}
    assert len(timestamps) == 2


def test_list_convergence_retry_empty_preserves_data_within_sweep():
    """三页：页1/页3 戳 T1，页2 戳 T2；重抓页2 全部返回空 → 断言返回 3 行、页1 与页3 的行原样保留、戳仍不唯一。"""
    t1_epoch = 1789544372
    t2_epoch = 1789544375

    page1 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0001",
                    "f14": "行业1",
                    "f3": 1.0,
                    "f8": 1.0,
                    "f104": 10,
                    "f105": 10,
                    "f109": 1.0,
                    "f110": 1.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    page2 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0002",
                    "f14": "行业2",
                    "f3": 2.0,
                    "f8": 2.0,
                    "f104": 20,
                    "f105": 20,
                    "f109": 2.0,
                    "f110": 2.0,
                    "f124": t2_epoch,
                }
            ]
        },
    }
    page3 = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "BK0003",
                    "f14": "行业3",
                    "f3": 3.0,
                    "f8": 3.0,
                    "f104": 30,
                    "f105": 30,
                    "f109": 3.0,
                    "f110": 3.0,
                    "f124": t1_epoch,
                }
            ]
        },
    }
    empty_page = {"rc": 0, "data": {"diff": []}}

    # max_pages=3, max_sweeps=4:
    # Sweep 1: page1, page2, page3 -> divergent_pages = [2]
    # Sweep 2: empty_page -> 保留 page2
    # Sweep 3: empty_page -> 保留 page2
    # Sweep 4: empty_page -> 保留 page2
    # 达到上限，走末尾并集分支，返回 3 行
    session = FakeSession(responses=[
        json.dumps(page1),
        json.dumps(page2),
        json.dumps(page3),
        json.dumps(empty_page),
        json.dumps(empty_page),
        json.dumps(empty_page),
    ])
    client = EastMoneyBoardClient(session=session, max_pages=3, max_sweeps=4)
    records = client.stock_board_industry_name_em().to_dict(orient="records")

    assert len(records) == 3
    by_code = {r["板块代码"]: r for r in records}
    assert set(by_code.keys()) == {"BK0001", "BK0002", "BK0003"}
    # 页1 与页3 的行原样保留
    assert by_code["BK0001"]["数据时间"] == by_code["BK0003"]["数据时间"]
    assert by_code["BK0002"]["数据时间"] != by_code["BK0001"]["数据时间"]
    timestamps = {r["数据时间"] for r in records}
    assert len(timestamps) == 2

