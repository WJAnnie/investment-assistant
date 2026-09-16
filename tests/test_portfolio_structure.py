import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.chan.multi_cycle_confirm import ConfirmOutcome, ROLE_ORDER, REQUIRED_CYCLES
from app.chan.signal import ChanSignal
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.portfolio.structure import (
    CycleInput,
    StructurePolicy,
    StructureResult,
    build_structure_evidence,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _aware_dt(
    year=2026,
    month=9,
    day=16,
    hour=15,
    minute=0,
    second=0,
    tz=SHANGHAI,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


def _make_bars(
    count: int = 3,
    end_time: datetime | None = None,
    step_minutes: int = 5,
) -> tuple[KLine, ...]:
    if end_time is None:
        end_time = _aware_dt(hour=15, minute=0)
    bars = []
    start_time = end_time - timedelta(minutes=(count - 1) * step_minutes)
    for i in range(count):
        t = start_time + timedelta(minutes=i * step_minutes)
        bars.append(
            KLine(
                time=t.isoformat(),
                open=10.0 + i,
                high=11.0 + i,
                low=9.5 + i,
                close=10.5 + i,
                volume=1000.0,
            )
        )
    return tuple(bars)


def _default_bar_counts() -> dict[Timeframe, int]:
    return {
        Timeframe.WEEKLY: 3,
        Timeframe.DAILY: 3,
        Timeframe.MIN_120: 3,
        Timeframe.MIN_30: 4,
        Timeframe.MIN_15: 8,
        Timeframe.MIN_5: 12,
    }


def _make_cycle_input(
    timeframe: Timeframe,
    status: BarStatus = BarStatus.CLOSED,
    bar_count: int | None = None,
    end_time: datetime | None = None,
    source: str = "test.cycle",
) -> CycleInput:
    if bar_count is None:
        bar_count = _default_bar_counts().get(timeframe, 3)
    bars = _make_bars(count=bar_count, end_time=end_time)
    return CycleInput(bars=bars, status=status, source=source)


def _make_all_cycles(
    status_overrides: dict[Timeframe, BarStatus] | None = None,
    bar_count_overrides: dict[Timeframe, int] | None = None,
    end_time_overrides: dict[Timeframe, datetime] | None = None,
    exclude: set[Timeframe] | None = None,
    default_end_time: datetime | None = None,
) -> dict[Timeframe, CycleInput]:
    status_overrides = status_overrides or {}
    bar_count_overrides = bar_count_overrides or {}
    end_time_overrides = end_time_overrides or {}
    exclude = exclude or set()
    base_end_t = default_end_time or _aware_dt(hour=15, minute=0)

    cycles: dict[Timeframe, CycleInput] = {}
    for tf in ROLE_ORDER:
        if tf in exclude:
            continue
        st = status_overrides.get(tf, BarStatus.CLOSED)
        cnt = bar_count_overrides.get(tf, _default_bar_counts().get(tf, 3))
        end_t = end_time_overrides.get(tf, base_end_t)
        cycles[tf] = _make_cycle_input(
            timeframe=tf,
            status=st,
            bar_count=cnt,
            end_time=end_t,
        )
    return cycles


class PortfolioStructureTests(unittest.TestCase):
    def test_01_all_closed_depth_ok_confirmed(self):
        cutoff = _aware_dt(hour=15, minute=0)
        cycles = _make_all_cycles()

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )

        self.assertIsInstance(res, StructureResult)
        self.assertTrue(res.ready)
        self.assertEqual(res.limit, "confirmed")
        self.assertEqual(res.outcome, ConfirmOutcome.CONFIRMED)
        self.assertEqual(res.blocked_by, ())
        self.assertIsNone(res.reason_code)
        self.assertEqual(res.stale_cycles, ())
        self.assertEqual(res.insufficient_cycles, ())

    def test_02_missing_120m_is_unavailable(self):
        cutoff = _aware_dt(hour=15, minute=0)
        cycles = _make_all_cycles(exclude={Timeframe.MIN_120})

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )

        self.assertFalse(res.ready)
        self.assertEqual(res.limit, "unavailable")
        self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
        self.assertIn("120m", res.blocked_by)
        self.assertEqual(res.per_cycle_status["120m"], "missing")

    def test_03_1430_120m_forming_is_preconfirm(self):
        cutoff = _aware_dt(hour=14, minute=30)
        cycles = _make_all_cycles(
            status_overrides={Timeframe.MIN_120: BarStatus.FORMING},
            default_end_time=cutoff,
        )

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )

        self.assertEqual(res.outcome, ConfirmOutcome.PRECONFIRM)
        self.assertFalse(res.ready)
        self.assertEqual(res.limit, "unavailable")

    def test_04_daily_stale_over_max_lag(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # DAILY 最后一根时间为 cutoff - 10 天，超过 4 天门槛
        old_daily_end = cutoff - timedelta(days=10)
        cycles = _make_all_cycles(
            end_time_overrides={Timeframe.DAILY: old_daily_end}
        )

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )

        self.assertEqual(res.stale_cycles, ("daily",))
        self.assertIn("daily", res.confirm.unavailable_cycles)
        self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
        self.assertFalse(res.ready)
        self.assertEqual(res.limit, "unavailable")
        self.assertEqual(res.per_cycle_status["daily"], "stale")

    def test_05_insufficient_bars_120m(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # MIN_120 只有 2 根（门槛 3）
        cycles = _make_all_cycles(
            bar_count_overrides={Timeframe.MIN_120: 2}
        )

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )

        self.assertEqual(res.insufficient_cycles, ("120m",))
        self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
        self.assertIn("120m", res.blocked_by)
        self.assertFalse(res.ready)
        self.assertEqual(res.limit, "unavailable")

    def test_06_per_cycle_status_invariants(self):
        cutoff = _aware_dt(hour=15, minute=0)
        cycles = _make_all_cycles(exclude={Timeframe.MIN_30})

        res = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
        )

        self.assertEqual(len(res.per_cycle_status), 6)
        valid_status_values = {st.value for st in BarStatus}
        for tf in ROLE_ORDER:
            self.assertIn(tf.value, res.per_cycle_status)
            self.assertIn(res.per_cycle_status[tf.value], valid_status_values)
        self.assertEqual(res.per_cycle_status["30m"], "missing")

    def test_07_invalid_inputs(self):
        cutoff = _aware_dt(hour=15, minute=0)
        cycles = _make_all_cycles()

        # 1. cutoff naive -> ValueError
        naive_dt = datetime(2026, 9, 16, 15, 0, 0)
        with self.assertRaises(ValueError):
            build_structure_evidence(
                code="000001", market="SZ", cycles=cycles, cutoff=naive_dt
            )

        # 2. cutoff 非 datetime -> TypeError
        with self.assertRaises(TypeError):
            build_structure_evidence(
                code="000001", market="SZ", cycles=cycles, cutoff="2026-09-16T15:00:00"  # type: ignore[arg-type]
            )

        # 3. code 为空 -> ValueError
        with self.assertRaises(ValueError):
            build_structure_evidence(
                code="", market="SZ", cycles=cycles, cutoff=cutoff
            )
        with self.assertRaises(ValueError):
            build_structure_evidence(
                code="   ", market="SZ", cycles=cycles, cutoff=cutoff
            )

        # 4. cycles 键不是 Timeframe -> ValueError
        invalid_key_cycles = dict(cycles)
        invalid_key_cycles["120m"] = _make_cycle_input(Timeframe.MIN_120)  # type: ignore[index]
        del invalid_key_cycles[Timeframe.MIN_120]
        with self.assertRaises(ValueError):
            build_structure_evidence(
                code="000001", market="SZ", cycles=invalid_key_cycles, cutoff=cutoff
            )

        # 5. CycleInput.bars 乱序 -> ValueError (由 CycleEvidence 抛出)
        t1 = cutoff - timedelta(minutes=10)
        t2 = cutoff - timedelta(minutes=20)
        disordered_bars = (
            KLine(time=t1.isoformat(), open=10, high=11, low=9, close=10),
            KLine(time=t2.isoformat(), open=10, high=11, low=9, close=10),
        )
        bad_cycles = dict(cycles)
        bad_cycles[Timeframe.MIN_120] = CycleInput(
            bars=disordered_bars, status=BarStatus.CLOSED, source="test"
        )
        with self.assertRaises(ValueError):
            build_structure_evidence(
                code="000001", market="SZ", cycles=bad_cycles, cutoff=cutoff
            )

    def test_08_immutability(self):
        cutoff = _aware_dt(hour=15, minute=0)
        cycles = _make_all_cycles()
        res = build_structure_evidence(
            code="000001", market="SZ", cycles=cycles, cutoff=cutoff
        )
        policy = StructurePolicy()
        inp = cycles[Timeframe.DAILY]

        with self.assertRaises(FrozenInstanceError):
            res.ready = True  # type: ignore[misc]

        with self.assertRaises(FrozenInstanceError):
            policy.source = "other"  # type: ignore[misc]

        with self.assertRaises(FrozenInstanceError):
            inp.status = BarStatus.MISSING  # type: ignore[misc]

    def test_09_min_5_cannot_upgrade(self):
        cutoff = _aware_dt(hour=15, minute=0)
        for st5 in (
            BarStatus.CLOSED,
            BarStatus.FORMING,
            BarStatus.MISSING,
            BarStatus.STALE,
            BarStatus.INVALID,
        ):
            cycles = _make_all_cycles(
                status_overrides={Timeframe.MIN_5: st5}
            )
            res = build_structure_evidence(
                code="000001",
                market="SZ",
                cycles=cycles,
                cutoff=cutoff,
                core_signal=ChanSignal.WAIT,
                trend_confirm=True,
            )
            self.assertFalse(res.ready)
            self.assertNotIn("5m", res.blocked_by)

    def test_10_limit_never_confirmed_when_not_ready(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # 测试各种未就绪状态：缺周期、forming、stale、insufficient、WAIT信号
        test_cases = [
            _make_all_cycles(exclude={Timeframe.WEEKLY}),
            _make_all_cycles(status_overrides={Timeframe.MIN_120: BarStatus.FORMING}),
            _make_all_cycles(end_time_overrides={Timeframe.DAILY: cutoff - timedelta(days=10)}),
            _make_all_cycles(bar_count_overrides={Timeframe.MIN_30: 1}),
        ]
        for c in test_cases:
            res = build_structure_evidence(
                code="000001",
                market="SZ",
                cycles=c,
                cutoff=cutoff,
                core_signal="SECOND_BUY",
                trend_confirm=True,
            )
            self.assertFalse(res.ready)
            self.assertNotEqual(res.limit, "confirmed")
            self.assertEqual(res.limit, "unavailable")

    def test_11_policy_keys_must_match_role_order_exactly(self):
        # 1. 窄 policy（只含 MIN_120）构造时必须 ValueError
        with self.assertRaises(ValueError) as ctx_narrow:
            StructurePolicy(
                min_bars_by_cycle={Timeframe.MIN_120: 3},
                max_lag_by_cycle={Timeframe.MIN_120: timedelta(days=1)},
            )
        self.assertIn("missing", str(ctx_narrow.exception))

        # 2. 缺 1 个周期必须 ValueError
        missing_one_bars = {tf: 3 for tf in ROLE_ORDER if tf != Timeframe.WEEKLY}
        missing_one_lag = {
            tf: timedelta(days=1) for tf in ROLE_ORDER if tf != Timeframe.WEEKLY
        }
        with self.assertRaises(ValueError) as ctx_missing:
            StructurePolicy(
                min_bars_by_cycle=missing_one_bars,
                max_lag_by_cycle=missing_one_lag,
            )
        self.assertIn("missing", str(ctx_missing.exception))
        self.assertIn("weekly", str(ctx_missing.exception))

        # 3. 多 1 个未知周期必须 ValueError
        extra_bars = {tf: 3 for tf in ROLE_ORDER}
        extra_lag = {tf: timedelta(days=1) for tf in ROLE_ORDER}
        extra_bars["unknown_cycle"] = 3  # type: ignore[index]
        extra_lag["unknown_cycle"] = timedelta(days=1)  # type: ignore[index]
        with self.assertRaises(ValueError) as ctx_extra:
            StructurePolicy(
                min_bars_by_cycle=extra_bars,
                max_lag_by_cycle=extra_lag,
            )
        self.assertIn("extra", str(ctx_extra.exception))
        self.assertIn("unknown_cycle", str(ctx_extra.exception))

        # 4. 完整 6 键必须成功
        full_bars = {tf: 3 for tf in ROLE_ORDER}
        full_lag = {tf: timedelta(days=1) for tf in ROLE_ORDER}
        policy = StructurePolicy(
            min_bars_by_cycle=full_bars,
            max_lag_by_cycle=full_lag,
        )
        self.assertEqual(set(policy.min_bars_by_cycle.keys()), set(ROLE_ORDER))
        self.assertEqual(set(policy.max_lag_by_cycle.keys()), set(ROLE_ORDER))

    def test_12_defect_a_fail_closed_old_daily_never_confirmed(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # 日线最后一根早于 cutoff 10 天（默认门槛 4 天）
        old_daily_end = cutoff - timedelta(days=10)
        cycles = _make_all_cycles(
            end_time_overrides={Timeframe.DAILY: old_daily_end}
        )

        # 默认 policy 下
        res_default = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )
        self.assertEqual(res_default.outcome, ConfirmOutcome.WAIT)
        self.assertIn("daily", res_default.stale_cycles)
        self.assertEqual(res_default.per_cycle_status["daily"], "stale")
        self.assertFalse(res_default.ready)
        self.assertEqual(res_default.limit, "unavailable")

        # 断言此时任何自定义 policy 都不可能得到 CONFIRMED
        # 用完整 6 键的自定义 policy（等价或保守配置）也应得到 WAIT
        custom_policy = StructurePolicy(
            min_bars_by_cycle={tf: 3 for tf in ROLE_ORDER},
            max_lag_by_cycle={
                Timeframe.WEEKLY: timedelta(days=7),
                Timeframe.DAILY: timedelta(days=4),
                Timeframe.MIN_120: timedelta(days=1),
                Timeframe.MIN_30: timedelta(days=1),
                Timeframe.MIN_15: timedelta(days=1),
                Timeframe.MIN_5: timedelta(days=1),
            },
        )
        res_custom = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
            policy=custom_policy,
        )
        self.assertEqual(res_custom.outcome, ConfirmOutcome.WAIT)
        self.assertIn("daily", res_custom.stale_cycles)
        self.assertEqual(res_custom.per_cycle_status["daily"], "stale")
        self.assertFalse(res_custom.ready)
        self.assertEqual(res_custom.limit, "unavailable")

        # 窄 policy 无法构造（直接抛 ValueError 保证无法绕过）
        with self.assertRaises(ValueError):
            StructurePolicy(
                min_bars_by_cycle={Timeframe.MIN_120: 3},
                max_lag_by_cycle={Timeframe.MIN_120: timedelta(days=1)},
            )

    def test_13_defect_b_missing_or_invalid_with_old_bars_not_in_stale_cycles(self):
        cutoff = _aware_dt(hour=15, minute=0)
        old_daily_end = cutoff - timedelta(days=10)

        for bad_status in (BarStatus.MISSING, BarStatus.INVALID):
            cycles = _make_all_cycles(
                status_overrides={Timeframe.DAILY: bad_status},
                end_time_overrides={Timeframe.DAILY: old_daily_end},
            )
            res = build_structure_evidence(
                code="000001",
                market="SZ",
                cycles=cycles,
                cutoff=cutoff,
                core_signal="SECOND_BUY",
                trend_confirm=True,
            )
            # stale_cycles 必须为空元组（因为未发生降级）
            self.assertEqual(res.stale_cycles, ())
            # 该周期仍必须进 unavailable / blocked_by
            self.assertIn("daily", res.blocked_by)
            self.assertIn("daily", res.confirm.unavailable_cycles)
            self.assertEqual(res.per_cycle_status["daily"], bad_status.value)
            self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
            self.assertFalse(res.ready)
            self.assertEqual(res.limit, "unavailable")

    def test_14_very_old_bars_never_confirmed_across_any_cycle(self):
        cutoff = _aware_dt(hour=15, minute=0)
        very_old_end = cutoff - timedelta(days=365)

        for tf in REQUIRED_CYCLES:
            cycles = _make_all_cycles(end_time_overrides={tf: very_old_end})
            res = build_structure_evidence(
                code="000001",
                market="SZ",
                cycles=cycles,
                cutoff=cutoff,
                core_signal="SECOND_BUY",
                trend_confirm=True,
            )
            self.assertNotEqual(res.outcome, ConfirmOutcome.CONFIRMED)
            self.assertFalse(res.ready)
            self.assertNotEqual(res.limit, "confirmed")
            self.assertEqual(res.limit, "unavailable")
            self.assertIn(tf.value, res.stale_cycles)
            self.assertEqual(res.per_cycle_status[tf.value], "stale")

        # 针对 5m（辅助周期）：极旧时虽不阻断 outcome，但仍必须被正确标记为 stale，且 price_optimization_available=False
        cycles_5m = _make_all_cycles(end_time_overrides={Timeframe.MIN_5: very_old_end})
        res_5m = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles_5m,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )
        self.assertIn("5m", res_5m.stale_cycles)
        self.assertEqual(res_5m.per_cycle_status["5m"], "stale")
        self.assertFalse(res_5m.confirm.price_optimization_available)


if __name__ == "__main__":
    unittest.main()
