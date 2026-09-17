from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import types
from typing import Any, Mapping
from zoneinfo import ZoneInfo
import pytest

from app.analysis.industry import (
    IndustryObservation,
    IndustryRankingPolicy,
    rank_industries,
)
from app.market.industries import (
    AkShareIndustryProvider,
    IndustryFetch,
    SOURCE_AKSHARE_EASTMONEY_BOARD,
    ERROR_NETWORK_UNAVAILABLE,
    ERROR_MALFORMED_RESPONSE,
    ERROR_NO_USABLE_DATA,
    ERROR_DATE_MISMATCH,
    VALID_ERROR_CODES,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class FakeDataFrame:
    """Lightweight test double for pandas DataFrame."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def to_dict(self, orient: str = "records") -> list[dict[str, Any]]:
        if orient != "records":
            raise ValueError(f"unsupported orient: {orient}")
        return [dict(r) for r in self._records]

    def __len__(self) -> int:
        return len(self._records)


class MockAkShareClient:
    """Mock client for testing AkShareIndustryProvider."""

    def __init__(
        self,
        board_names: Any = None,
        board_hists: Mapping[str, Any] | None = None,
        board_names_exc: Exception | None = None,
        board_hist_exc_map: Mapping[str, Exception] | None = None,
    ) -> None:
        self.board_names = board_names
        self.board_hists = dict(board_hists or {})
        self.board_names_exc = board_names_exc
        self.board_hist_exc_map = dict(board_hist_exc_map or {})
        self.hist_calls: list[dict[str, Any]] = []

    def stock_board_industry_name_em(self) -> Any:
        if self.board_names_exc is not None:
            raise self.board_names_exc
        return self.board_names

    def stock_board_industry_hist_em(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str = "日k",
        adjust: str = "",
    ) -> Any:
        self.hist_calls.append({
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "period": period,
            "adjust": adjust,
        })
        if symbol in self.board_hist_exc_map:
            raise self.board_hist_exc_map[symbol]
        return self.board_hists.get(symbol)


def _make_valid_hist_df(
    market_date: date,
    num_days: int = 30,
    base_price: float = 10.0,
    close_override: Mapping[int, float] | None = None,
) -> FakeDataFrame:
    """Generate ascending daily K-lines where the last day is market_date."""
    records = []
    overrides = close_override or {}
    for i in range(num_days):
        day_offset = num_days - 1 - i
        d = market_date - timedelta(days=day_offset)
        idx_from_end = - (num_days - i)  # -num_days to -1
        price = overrides.get(idx_from_end, base_price + i * 0.1)
        records.append({
            "日期": d.strftime("%Y-%m-%d"),
            "开盘": price,
            "收盘": price,
            "最高": price + 0.5,
            "最低": price - 0.5,
            "成交量": 10000,
        })
    return FakeDataFrame(records)


def _make_valid_board_records() -> list[dict[str, Any]]:
    return [
        {
            "板块代码": "BK0420",
            "板块名称": "半导体",
            "涨跌幅": 2.5,
            "换手率": 3.1,
            "上涨家数": 80,
            "下跌家数": 20,
        },
        {
            "板块代码": "BK0425",
            "板块名称": "互联网服务",
            "涨跌幅": 1.2,
            "换手率": 2.0,
            "上涨家数": 50,
            "下跌家数": 30,
        },
    ]


class TestAkShareIndustryProvider:
    # 1. 惰性导入：__init__ 不得导入 akshare，也不得发起网络请求
    def test_init_lazy_import_no_network(self) -> None:
        provider = AkShareIndustryProvider()
        assert provider.SOURCE == SOURCE_AKSHARE_EASTMONEY_BOARD
        assert provider._client is None

    # 2. fetch 入参校验：cutoff 必须是 tz-aware datetime
    def test_fetch_cutoff_validation(self) -> None:
        provider = AkShareIndustryProvider(client=MockAkShareClient())
        m_date = date(2026, 9, 17)

        # Naive datetime
        with pytest.raises(ValueError, match="cutoff must be timezone-aware"):
            provider.fetch(cutoff=datetime(2026, 9, 17, 16, 0), market_date=m_date)

        # Non-datetime
        with pytest.raises(TypeError, match="cutoff must be a datetime"):
            provider.fetch(cutoff="2026-09-17 16:00:00", market_date=m_date)  # type: ignore[arg-type]

    # 3. fetch 入参校验：market_date 必须是 datetime.date 且不能是 datetime
    def test_fetch_market_date_validation(self) -> None:
        provider = AkShareIndustryProvider(client=MockAkShareClient())
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        # datetime passed as market_date
        with pytest.raises(TypeError, match="market_date must be datetime.date and not datetime"):
            provider.fetch(cutoff=cutoff, market_date=datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI_TZ))  # type: ignore[arg-type]

        # String passed as market_date
        with pytest.raises(TypeError, match="market_date must be datetime.date and not datetime"):
            provider.fetch(cutoff=cutoff, market_date="2026-09-17")  # type: ignore[arg-type]

        # Bool passed
        with pytest.raises(TypeError, match="market_date must be datetime.date and not datetime"):
            provider.fetch(cutoff=cutoff, market_date=True)  # type: ignore[arg-type]

    # 4. max_boards 参数校验
    def test_max_boards_validation(self) -> None:
        with pytest.raises(ValueError, match="max_boards"):
            AkShareIndustryProvider(max_boards=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_boards"):
            AkShareIndustryProvider(max_boards=0)
        with pytest.raises(ValueError, match="max_boards"):
            AkShareIndustryProvider(max_boards=-5)
        with pytest.raises(TypeError, match="max_boards"):
            AkShareIndustryProvider(max_boards="10")  # type: ignore[arg-type]

    # 5. history_days 参数校验
    def test_history_days_validation(self) -> None:
        with pytest.raises(ValueError, match="history_days"):
            AkShareIndustryProvider(history_days=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="history_days"):
            AkShareIndustryProvider(history_days=0)
        with pytest.raises(TypeError, match="history_days"):
            AkShareIndustryProvider(history_days="40")  # type: ignore[arg-type]

    # 6. clock 参数校验
    def test_clock_validation(self) -> None:
        with pytest.raises(TypeError, match="clock must be callable"):
            AkShareIndustryProvider(clock=123)

        # Clock returns naive or non-datetime
        provider_naive = AkShareIndustryProvider(
            client=MockAkShareClient(),
            clock=lambda: datetime(2026, 9, 17, 16, 0),
        )
        with pytest.raises(ValueError, match="clock must return a timezone-aware datetime"):
            provider_naive.fetch(
                cutoff=datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ),
                market_date=date(2026, 9, 17),
            )

        provider_bool = AkShareIndustryProvider(
            client=MockAkShareClient(),
            clock=lambda: True,
        )
        with pytest.raises(TypeError, match="clock must return a datetime"):
            provider_bool.fetch(
                cutoff=datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ),
                market_date=date(2026, 9, 17),
            )

    # 7. 现货横截面异常不外抛：返回 IndustryFetch(observations=(), errors={}, truncated=False)
    def test_spot_call_exception_swallowed(self) -> None:
        client = MockAkShareClient(board_names_exc=ConnectionError("network down"))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=date(2026, 9, 17))
        assert res.observations == ()
        assert res.errors == {}
        assert res.truncated is False

    # 8. 现货横截面无 to_dict 或为空
    def test_spot_call_no_to_dict_or_empty(self) -> None:
        client = MockAkShareClient(board_names="not_a_df")
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=date(2026, 9, 17))
        assert res.observations == ()
        assert res.errors == {}
        assert res.truncated is False

    # 9. 成功获取完整观测并正确计算 5日 / 20日收益
    def test_successful_fetch_and_return_calculations(self) -> None:
        m_date = date(2026, 9, 17)
        hist_df = _make_valid_hist_df(
            market_date=m_date,
            num_days=30,
            close_override={-1: 12.0, -6: 10.0, -21: 8.0},
        )
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 1
        assert len(res.errors) == 0
        assert res.truncated is False

        obs = res.observations[0]
        assert obs.code == "BK0420"
        assert obs.name == "半导体"
        assert obs.source == SOURCE_AKSHARE_EASTMONEY_BOARD
        assert obs.as_of == datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI_TZ)
        assert obs.fetched_at == clock_dt
        assert obs.return_1d_pct == 2.5
        assert obs.turnover_rate == 3.1
        assert obs.advancers == 80
        assert obs.decliners == 20
        assert pytest.approx(obs.return_5d_pct, rel=1e-6) == 20.0
        assert pytest.approx(obs.return_20d_pct, rel=1e-6) == 50.0

    # 10. code 提取与校验：非字符串、含空白、单股票代码、config sector -> ERROR_MALFORMED_RESPONSE
    def test_invalid_code_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": " BK0420 ", "板块名称": "半导体", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "600519", "板块名称": "茅台", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "sector:tech", "板块名称": "科技", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("600519") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("sector:tech") == ERROR_MALFORMED_RESPONSE

    # 11. name 校验：非字符串、含空白、空、超长、config sector -> ERROR_MALFORMED_RESPONSE
    def test_invalid_name_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK001", "板块名称": " 半导体 ", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK003", "板块名称": "sector:tech", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK001") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK002") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK003") == ERROR_MALFORMED_RESPONSE

    # 12. 数值校验：return_1d_pct 拒绝 bool、非有限数、非数值 -> ERROR_MALFORMED_RESPONSE
    def test_invalid_return_1d_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK001", "板块名称": "半导体", "涨跌幅": True, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "银行", "涨跌幅": float("nan"), "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK003", "板块名称": "证券", "涨跌幅": "invalid_num", "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK001") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK002") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK003") == ERROR_MALFORMED_RESPONSE

    # 13. 数值校验：turnover_rate 拒绝 bool、非有限数、负数 -> ERROR_MALFORMED_RESPONSE
    def test_invalid_turnover_rate_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK001", "板块名称": "半导体", "涨跌幅": 1.0, "换手率": -0.5, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "银行", "涨跌幅": 1.0, "换手率": True, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK003", "板块名称": "证券", "涨跌幅": 1.0, "换手率": float("inf"), "上涨家数": 10, "下跌家数": 10},
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK001") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK002") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK003") == ERROR_MALFORMED_RESPONSE

    # 14. advancers/decliners 校验：拒绝 bool、必须为整数（浮点数转换后非整则失败）、二者之和 > 0
    def test_invalid_advancers_decliners_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK001", "板块名称": "半导体", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": True, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "银行", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10.5, "下跌家数": 10},
            {"板块代码": "BK003", "板块名称": "证券", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": -1, "下跌家数": 10},
            {"板块代码": "BK004", "板块名称": "医药", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 0, "下跌家数": 0},
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        provider = AkShareIndustryProvider(client=client)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK001") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK002") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK003") == ERROR_MALFORMED_RESPONSE
        assert res.errors.get("BK004") == ERROR_MALFORMED_RESPONSE

    # 15. advancers/decliners 支持整型浮点数（如 80.0 -> 80）
    def test_integer_float_for_advancers_accepted(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK0420", "板块名称": "半导体", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 80.0, "下跌家数": 20.0},
        ]
        hist_df = _make_valid_hist_df(market_date=m_date, num_days=25)
        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hists={"BK0420": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 1
        assert res.observations[0].advancers == 80
        assert res.observations[0].decliners == 20

    # 16. 时间校验：若 as_of > fetched_at 则记 ERROR_DATE_MISMATCH
    def test_as_of_later_than_fetched_at_generates_date_mismatch(self) -> None:
        m_date = date(2026, 9, 17)
        early_clock = datetime(2026, 9, 17, 14, 0, tzinfo=SHANGHAI_TZ)
        client = MockAkShareClient(board_names=FakeDataFrame(_make_valid_board_records()[:1]))
        provider = AkShareIndustryProvider(client=client, clock=lambda: early_clock)
        cutoff = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_DATE_MISMATCH

    # 17. 历史 K 线调用参数正确传递
    def test_hist_call_parameters(self) -> None:
        m_date = date(2026, 9, 17)
        hist_df = _make_valid_hist_df(market_date=m_date, num_days=25)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt, history_days=40)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(client.hist_calls) == 1
        call = client.hist_calls[0]
        assert call["symbol"] == "BK0420"
        assert call["start_date"] == (m_date - timedelta(days=40)).strftime("%Y%m%d")
        assert call["end_date"] == (m_date + timedelta(days=40)).strftime("%Y%m%d")
        assert call["period"] == "日k"
        assert call["adjust"] == ""

    # 18. 历史 K 线调用抛出网络异常 -> ERROR_NETWORK_UNAVAILABLE
    def test_hist_network_exception_generates_network_unavailable(self) -> None:
        m_date = date(2026, 9, 17)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hist_exc_map={"BK0420": TimeoutError("timeout fetching hist")},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_NETWORK_UNAVAILABLE

    # 19. 历史 K 线调用抛出非网络异常 -> ERROR_MALFORMED_RESPONSE
    def test_hist_generic_exception_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hist_exc_map={"BK0420": KeyError("missing key")},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE

    # 20. 历史 K 线空数据（0根）-> ERROR_NO_USABLE_DATA
    def test_hist_empty_rows_generates_no_usable_data(self) -> None:
        m_date = date(2026, 9, 17)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": FakeDataFrame([])},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_NO_USABLE_DATA

    # 21. 历史 K 线最后一根日期 != market_date -> ERROR_DATE_MISMATCH
    def test_hist_last_date_mismatch_generates_date_mismatch(self) -> None:
        m_date = date(2026, 9, 17)
        hist_df = _make_valid_hist_df(market_date=date(2026, 9, 16), num_days=25)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_DATE_MISMATCH

    # 22. 历史 K 线根数不足 21 根 -> ERROR_NO_USABLE_DATA
    def test_hist_insufficient_bars_generates_no_usable_data(self) -> None:
        m_date = date(2026, 9, 17)
        hist_df = _make_valid_hist_df(market_date=m_date, num_days=20)
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_NO_USABLE_DATA

    # 23. 历史 K 线收盘价 <= 0 -> ERROR_NO_USABLE_DATA
    def test_hist_close_non_positive_generates_no_usable_data(self) -> None:
        m_date = date(2026, 9, 17)
        hist_df_zero = _make_valid_hist_df(market_date=m_date, num_days=25, close_override={-1: 0.0})
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": hist_df_zero},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_NO_USABLE_DATA

        hist_df_neg = _make_valid_hist_df(market_date=m_date, num_days=25, close_override={-6: -1.0})
        client.board_hists["BK0420"] = hist_df_neg
        res2 = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res2.observations) == 0
        assert res2.errors.get("BK0420") == ERROR_NO_USABLE_DATA

    # 24. 历史 K 线损坏或缺失收盘价列 -> ERROR_MALFORMED_RESPONSE
    def test_hist_corrupted_columns_generates_malformed_response(self) -> None:
        m_date = date(2026, 9, 17)
        corrupt_records = [
            {"日期": "2026-09-17", "开盘": 10.0}
        ]
        client = MockAkShareClient(
            board_names=FakeDataFrame(_make_valid_board_records()[:1]),
            board_hists={"BK0420": FakeDataFrame(corrupt_records)},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE

    # 25. 故障隔离与互斥穷尽：一个板块成功，一个板块失败
    def test_isolation_and_mutual_exclusion(self) -> None:
        m_date = date(2026, 9, 17)
        records = _make_valid_board_records()
        hist_valid = _make_valid_hist_df(market_date=m_date, num_days=25)
        hist_short = _make_valid_hist_df(market_date=m_date, num_days=10)

        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hists={"BK0420": hist_valid, "BK0425": hist_short},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        obs_codes = {obs.code for obs in res.observations}
        err_codes = set(res.errors.keys())

        assert obs_codes.isdisjoint(err_codes)
        assert obs_codes | err_codes == {"BK0420", "BK0425"}
        assert "BK0420" in obs_codes
        assert res.errors["BK0425"] == ERROR_NO_USABLE_DATA
        assert res.truncated is False

    # 26. 错误码合法性：所有 errors 值必须严格属于 4 个错误码
    def test_all_errors_are_valid_error_codes(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK001", "板块名称": "", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "正常", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
        ]
        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hist_exc_map={"BK002": ConnectionError("timeout")},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        for code, err in res.errors.items():
            assert err in VALID_ERROR_CODES

    # 27. max_boards 截断与排序契约
    def test_max_boards_truncation_contract(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {"板块代码": "BK003", "板块名称": "板块三", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK001", "板块名称": "板块一", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
            {"板块代码": "BK002", "板块名称": "板块二", "涨跌幅": 1.0, "换手率": 1.0, "上涨家数": 10, "下跌家数": 10},
        ]
        hist_df = _make_valid_hist_df(market_date=m_date, num_days=25)
        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hists={"BK001": hist_df, "BK002": hist_df, "BK003": hist_df},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt, max_boards=2)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert res.truncated is True
        processed_codes = [obs.code for obs in res.observations]
        assert processed_codes == ["BK001", "BK002"]

        provider_large = AkShareIndustryProvider(client=client, clock=lambda: clock_dt, max_boards=5)
        res_large = provider_large.fetch(cutoff=cutoff, market_date=m_date)
        assert res_large.truncated is False
        assert len(res_large.observations) == 3

    # 28. IndustryFetch dataclass 校验与不可变性
    def test_industry_fetch_contract_checks(self) -> None:
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        as_of = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI_TZ)
        obs1 = IndustryObservation(
            code="BK001",
            name="行业一",
            as_of=as_of,
            fetched_at=clock_dt,
            source=SOURCE_AKSHARE_EASTMONEY_BOARD,
            return_1d_pct=1.0,
            return_5d_pct=2.0,
            return_20d_pct=3.0,
            turnover_rate=1.0,
            advancers=10,
            decliners=10,
        )

        with pytest.raises(ValueError, match="duplicate code"):
            IndustryFetch(observations=(obs1, obs1), errors={}, truncated=False)

        with pytest.raises(ValueError, match="errors keys must be non-empty strings"):
            IndustryFetch(observations=(), errors={"": ERROR_MALFORMED_RESPONSE}, truncated=False)

        with pytest.raises(ValueError, match="recognized error codes"):
            IndustryFetch(observations=(), errors={"BK002": "UNKNOWN_CODE"}, truncated=False)

        with pytest.raises(ValueError, match="mutually exclusive"):
            IndustryFetch(observations=(obs1,), errors={"BK001": ERROR_MALFORMED_RESPONSE}, truncated=False)

        with pytest.raises(TypeError, match="truncated must be a boolean"):
            IndustryFetch(observations=(), errors={}, truncated="yes")  # type: ignore[arg-type]

        fetch_obj = IndustryFetch(observations=(obs1,), errors={"BK002": ERROR_MALFORMED_RESPONSE}, truncated=False)
        assert isinstance(fetch_obj.errors, types.MappingProxyType)

    # 29. 端到端对接：fetch 返回的 observations 能被 rank_industries + IndustryRankingPolicy 直接消费
    def test_integration_with_rank_industries(self) -> None:
        m_date = date(2026, 9, 17)
        records = _make_valid_board_records()
        hist_df1 = _make_valid_hist_df(market_date=m_date, num_days=25, base_price=10.0)
        hist_df2 = _make_valid_hist_df(market_date=m_date, num_days=25, base_price=20.0)

        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hists={"BK0420": hist_df1, "BK0425": hist_df2},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)

        res = provider.fetch(cutoff=cutoff, market_date=m_date)
        assert len(res.observations) == 2

        policy = IndustryRankingPolicy(
            weight_1d=0.2,
            weight_5d=0.2,
            weight_20d=0.2,
            weight_breadth=0.2,
            weight_activity=0.2,
            min_coverage=2,
            max_age_hours=24,
            min_score=50,
        )

        ranking_result = rank_industries(
            observations=res.observations,
            cutoff=cutoff,
            policy=policy,
        )
        assert ranking_result.coverage == len(res.observations)
        assert len(ranking_result.items) == 2
        assert ranking_result.stamp.status.value == "READY"

    # 30. 包含 近5日涨跌幅 与 近20日涨跌幅 的行优先使用列表直出，且不调用 K线接口
    def test_fetch_with_list_supplied_5d_20d_returns(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 1
        assert len(res.errors) == 0
        obs = res.observations[0]
        assert obs.code == "BK0420"
        assert obs.return_5d_pct == 1.25
        assert obs.return_20d_pct == 4.56
        assert len(client.hist_calls) == 0

    # 31. 近5日涨跌幅 与 近20日涨跌幅 为数字字符串时仍可成功解析
    def test_fetch_with_list_supplied_returns_numeric_strings(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": "1.25",
                "近20日涨跌幅": "4.56",
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 1
        obs = res.observations[0]
        assert obs.return_5d_pct == 1.25
        assert obs.return_20d_pct == 4.56
        assert len(client.hist_calls) == 0

    # 32. 仅存在 近5日涨跌幅（缺少 20日） -> MALFORMED_RESPONSE
    def test_fetch_with_only_5d_present_errors_malformed(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE
        assert len(client.hist_calls) == 0

    # 33. 仅存在 近20日涨跌幅（缺少 5日） -> MALFORMED_RESPONSE
    def test_fetch_with_only_20d_present_errors_malformed(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近20日涨跌幅": 4.56,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE
        assert len(client.hist_calls) == 0

    # 34. 近5日涨跌幅 为 "-" 或 None 时（20日有效） -> MALFORMED_RESPONSE
    @pytest.mark.parametrize("bad_5d", ["-", None])
    def test_fetch_with_unparseable_or_none_5d_errors_malformed(self, bad_5d: Any) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": bad_5d,
                "近20日涨跌幅": 4.56,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_MALFORMED_RESPONSE
        assert len(client.hist_calls) == 0

    # 35. 存在 5日/20日收益率时，as_of > fetched_at 依然触发 DATE_MISMATCH
    def test_fetch_date_mismatch_preempts_list_supplied_returns(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        # fetched_at 在当天 14:00，早于 as_of (15:00)
        clock_dt = datetime(2026, 9, 17, 14, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_DATE_MISMATCH
        assert len(client.hist_calls) == 0

    # 36. 混合批次：一行使用列表直出，一行无列表收益回退至历史 K线，产出 2条观测且仅 1次 hist 调用
    def test_fetch_mixed_batch_list_supplied_and_kline_fallback(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
            },
            {
                "板块代码": "BK0425",
                "板块名称": "互联网服务",
                "涨跌幅": 1.2,
                "换手率": 2.0,
                "上涨家数": 50,
                "下跌家数": 30,
            },
        ]
        hist_df2 = _make_valid_hist_df(
            market_date=m_date,
            num_days=25,
            close_override={-1: 10.0, -6: 10.0, -21: 10.0},
        )
        client = MockAkShareClient(
            board_names=FakeDataFrame(records),
            board_hists={"BK0425": hist_df2},
        )
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 2
        assert len(res.errors) == 0
        assert len(client.hist_calls) == 1
        assert client.hist_calls[0]["symbol"] == "BK0425"

        obs_map = {o.code: o for o in res.observations}
        assert obs_map["BK0420"].return_5d_pct == 1.25
        assert obs_map["BK0420"].return_20d_pct == 4.56
        assert obs_map["BK0425"].return_5d_pct == 0.0
        assert obs_map["BK0425"].return_20d_pct == 0.0

    # 37. 单行携带真实"数据时间"：生成观测的 as_of 等于该带时区时间，且不等于默认编造的 15:00
    def test_fetch_row_with_source_data_time(self) -> None:
        m_date = date(2026, 9, 17)
        stamp_str = "2026-09-17T15:39:32+08:00"
        expected_dt = datetime(2026, 9, 17, 15, 39, 32, tzinfo=SHANGHAI_TZ)
        fabricated_dt = datetime.combine(m_date, time(15, 0), tzinfo=SHANGHAI_TZ)

        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": stamp_str,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 1
        assert len(res.errors) == 0
        obs = res.observations[0]
        assert obs.as_of == expected_dt
        assert obs.as_of != fabricated_dt

    # 38. 多行共享相同"数据时间"：全部保留且共享该 as_of
    def test_fetch_multiple_rows_share_identical_data_time(self) -> None:
        m_date = date(2026, 9, 17)
        stamp_str = "2026-09-17T15:39:32+08:00"
        expected_dt = datetime(2026, 9, 17, 15, 39, 32, tzinfo=SHANGHAI_TZ)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": stamp_str,
            },
            {
                "板块代码": "BK0425",
                "板块名称": "互联网服务",
                "涨跌幅": 1.2,
                "换手率": 2.0,
                "上涨家数": 50,
                "下跌家数": 30,
                "近5日涨跌幅": 0.5,
                "近20日涨跌幅": 2.0,
                "数据时间": stamp_str,
            },
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 2
        assert len(res.errors) == 0
        assert res.observations[0].as_of == expected_dt
        assert res.observations[1].as_of == expected_dt

    # 39. 两行具有窗口内不同的"数据时间"（28s 差异 <= 240s）：归一化为窗口终点，产生2条观测，errors为空
    def test_fetch_in_window_data_times_normalized_to_window_end(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": "2026-09-17T15:39:32+08:00",
            },
            {
                "板块代码": "BK0425",
                "板块名称": "互联网服务",
                "涨跌幅": 1.2,
                "换手率": 2.0,
                "上涨家数": 50,
                "下跌家数": 30,
                "近5日涨跌幅": 0.5,
                "近20日涨跌幅": 2.0,
                "数据时间": "2026-09-17T15:40:00+08:00",
            },
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 2
        assert len(res.errors) == 0
        expected_as_of = datetime(2026, 9, 17, 15, 40, 0, tzinfo=SHANGHAI_TZ)
        assert res.observations[0].as_of == expected_as_of
        assert res.observations[1].as_of == expected_as_of

    # 39b. 跨度超过窗口阈值（6分钟，360s > 240s）：触发冲突，0条观测且所有代码均标记为 ERROR_DATE_MISMATCH
    def test_fetch_out_of_window_data_times_fails_batch(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": "2026-09-17T15:39:32+08:00",
            },
            {
                "板块代码": "BK0425",
                "板块名称": "互联网服务",
                "涨跌幅": 1.2,
                "换手率": 2.0,
                "上涨家数": 50,
                "下跌家数": 30,
                "近5日涨跌幅": 0.5,
                "近20日涨跌幅": 2.0,
                "数据时间": "2026-09-17T15:45:32+08:00",
            },
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_DATE_MISMATCH
        assert res.errors.get("BK0425") == ERROR_DATE_MISMATCH

    # 40. 某行"数据时间"不可解析为乱码，而同批次其他行时间合法：乱码行记为 ERROR_DATE_MISMATCH，合法行成功
    def test_fetch_unparseable_data_time_reported_as_mismatch(self) -> None:
        m_date = date(2026, 9, 17)
        valid_stamp = "2026-09-17T15:39:32+08:00"
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": valid_stamp,
            },
            {
                "板块代码": "BK0425",
                "板块名称": "互联网服务",
                "涨跌幅": 1.2,
                "换手率": 2.0,
                "上涨家数": 50,
                "下跌家数": 30,
                "近5日涨跌幅": 0.5,
                "近20日涨跌幅": 2.0,
                "数据时间": "not-a-time",
            },
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 1
        assert res.observations[0].code == "BK0420"
        assert res.errors.get("BK0425") == ERROR_DATE_MISMATCH

    # 41. "数据时间"晚于 fetched_at：标记 ERROR_DATE_MISMATCH
    def test_fetch_data_time_later_than_fetched_at(self) -> None:
        m_date = date(2026, 9, 17)
        stamp_str = "2026-09-17T15:39:32+08:00"
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
                "数据时间": stamp_str,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        # clock 早于数据时间 15:39:32
        early_clock = datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: early_clock)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 0
        assert res.errors.get("BK0420") == ERROR_DATE_MISMATCH

    # 42. 无"数据时间"字段：保持向后兼容，使用默认 15:00 时间戳正常成功
    def test_fetch_no_data_time_backwards_compatibility(self) -> None:
        m_date = date(2026, 9, 17)
        records = [
            {
                "板块代码": "BK0420",
                "板块名称": "半导体",
                "涨跌幅": 2.5,
                "换手率": 3.1,
                "上涨家数": 80,
                "下跌家数": 20,
                "近5日涨跌幅": 1.25,
                "近20日涨跌幅": 4.56,
            }
        ]
        client = MockAkShareClient(board_names=FakeDataFrame(records))
        clock_dt = datetime(2026, 9, 17, 16, 0, tzinfo=SHANGHAI_TZ)
        provider = AkShareIndustryProvider(client=client, clock=lambda: clock_dt)
        cutoff = datetime(2026, 9, 17, 17, 0, tzinfo=SHANGHAI_TZ)
        res = provider.fetch(cutoff=cutoff, market_date=m_date)

        assert len(res.observations) == 1
        assert len(res.errors) == 0
        assert res.observations[0].as_of == datetime(2026, 9, 17, 15, 0, tzinfo=SHANGHAI_TZ)

