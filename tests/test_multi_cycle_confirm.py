import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.chan.models import KLine
from app.chan.multi_cycle_confirm import (
    BUY_SIGNALS,
    CORE_CYCLE,
    REQUIRED_CYCLES,
    ROLE_ORDER,
    ConfirmOutcome,
    CycleEvidence,
    MultiCycleConfirmResult,
    confirm_multi_cycle,
)
from app.chan.signal import ChanSignal
from app.decision.gates import evaluate_gates
from app.domain.bars import BarStatus
from app.domain.evidence import EvidenceStatus, Freshness, GateEvidence
from app.domain.timeframe import Timeframe

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
    use_iso_str: bool = False,
) -> tuple[KLine, ...]:
    if end_time is None:
        end_time = _aware_dt(hour=15, minute=0)
    bars = []
    start_time = end_time - timedelta(minutes=(count - 1) * step_minutes)
    for i in range(count):
        t = start_time + timedelta(minutes=i * step_minutes)
        t_val = t.isoformat() if use_iso_str else t
        bars.append(
            KLine(
                time=t_val,
                open=10.0 + i,
                high=11.0 + i,
                low=9.5 + i,
                close=10.5 + i,
                volume=1000.0,
            )
        )
    return tuple(bars)


def _make_evidence(
    timeframe: Timeframe,
    status: BarStatus = BarStatus.CLOSED,
    bar_count: int = 3,
    cutoff: datetime | None = None,
    code: str = "000001",
    market: str = "SZ",
    source: str = "test_source",
    bars: tuple[KLine, ...] | None = None,
) -> CycleEvidence:
    c = cutoff or _aware_dt(hour=15, minute=0)
    if bars is None:
        bars = _make_bars(count=bar_count, end_time=c)
    return CycleEvidence(
        timeframe=timeframe,
        code=code,
        market=market,
        bars=bars,
        status=status,
        cutoff=c,
        source=source,
    )


def _make_all_evidences(
    status_map: dict[Timeframe, BarStatus] | None = None,
    cutoff: datetime | None = None,
    code: str = "000001",
    market: str = "SZ",
    bar_count: int = 3,
) -> list[CycleEvidence]:
    c = cutoff or _aware_dt(hour=15, minute=0)
    status_map = status_map or {}
    evidences = []
    for tf in ROLE_ORDER:
        st = status_map.get(tf, BarStatus.CLOSED)
        evidences.append(
            _make_evidence(
                timeframe=tf,
                status=st,
                bar_count=bar_count,
                cutoff=c,
                code=code,
                market=market,
            )
        )
    return evidences


class MultiCycleConfirmTests(unittest.TestCase):
    def test_01_all_closed_second_buy_trend_confirm_is_confirmed(self):
        cutoff = _aware_dt(hour=15, minute=0)
        evidences = _make_all_evidences(cutoff=cutoff)

        res = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )

        self.assertIsInstance(res, MultiCycleConfirmResult)
        self.assertEqual(res.outcome, ConfirmOutcome.CONFIRMED)
        self.assertTrue(res.structure_gate.passed)
        self.assertEqual(res.blocked_by, ())
        self.assertIsNone(res.reason_code)
        self.assertTrue(res.price_optimization_available)
        self.assertEqual(
            res.closed_cycles,
            tuple(tf.value for tf in ROLE_ORDER),
        )
        self.assertEqual(res.forming_cycles, ())
        self.assertEqual(res.unavailable_cycles, ())
        self.assertEqual(res.core_signal, ChanSignal.SECOND_BUY)
        self.assertTrue(res.trend_confirm)
        self.assertEqual(res.code, "000001")
        self.assertEqual(res.market, "SZ")
        self.assertEqual(res.cutoff, cutoff)
        self.assertLessEqual(res.as_of, cutoff)
        self.assertEqual(res.structure_gate.name, "structure")
        self.assertEqual(res.structure_gate.stamp.status, EvidenceStatus.READY)
        self.assertEqual(res.structure_gate.stamp.freshness, Freshness.RECENT)
        self.assertIsNone(res.structure_gate.stamp.reason_code)

    def test_02_1430_120m_forming_is_preconfirm(self):
        cutoff = _aware_dt(hour=14, minute=30)
        status_map = {Timeframe.MIN_120: BarStatus.FORMING}
        evidences = _make_all_evidences(status_map=status_map, cutoff=cutoff)

        res = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )

        self.assertEqual(res.outcome, ConfirmOutcome.PRECONFIRM)
        self.assertFalse(res.structure_gate.passed)
        self.assertIn("120m", res.forming_cycles)
        self.assertEqual(res.reason_code, "UNCONFIRMED_STRUCTURE")
        self.assertIn("120m", res.blocked_by)
        self.assertEqual(res.structure_gate.stamp.status, EvidenceStatus.DEGRADED)
        self.assertEqual(res.structure_gate.stamp.freshness, Freshness.RECENT)
        self.assertEqual(
            res.structure_gate.stamp.reason_code, "UNCONFIRMED_STRUCTURE"
        )
        self.assertEqual(
            res.structure_gate.criterion_code, "STRUCTURE_CRITERION_FAILED"
        )

    def test_03_exhaustive_120m_forming_never_confirmed(self):
        cutoff = _aware_dt(hour=14, minute=30)
        candidate_statuses = [
            BarStatus.CLOSED,
            BarStatus.FORMING,
            BarStatus.MISSING,
        ]

        # 遍历其他周期的状态组合子集，固定 120m 为 FORMING
        for other_st in candidate_statuses:
            for tf_var in (Timeframe.WEEKLY, Timeframe.DAILY, Timeframe.MIN_30, Timeframe.MIN_5):
                status_map = {
                    Timeframe.MIN_120: BarStatus.FORMING,
                    tf_var: other_st,
                }
                evidences = _make_all_evidences(status_map=status_map, cutoff=cutoff)
                for sig in (ChanSignal.SECOND_BUY, ChanSignal.WAIT):
                    for tc in (True, False):
                        res = confirm_multi_cycle(
                            evidences,
                            cutoff=cutoff,
                            core_signal=sig,
                            trend_confirm=tc,
                        )
                        self.assertNotEqual(
                            res.outcome,
                            ConfirmOutcome.CONFIRMED,
                            f"Failed on status_map={status_map}, sig={sig}, tc={tc}",
                        )
                        self.assertFalse(
                            res.structure_gate.passed,
                            "structure_gate must not pass when 120m is FORMING",
                        )

    def test_04_min_5_cannot_upgrade_and_not_in_blocked_by(self):
        cutoff = _aware_dt(hour=15, minute=0)

        # 1. 其余 5 个周期全闭合但 core_signal=WAIT 时，5m 各状态结果都只能是 WAIT
        for st5 in (
            BarStatus.CLOSED,
            BarStatus.FORMING,
            BarStatus.MISSING,
            BarStatus.STALE,
            BarStatus.INVALID,
        ):
            status_map = {Timeframe.MIN_5: st5}
            evidences = _make_all_evidences(status_map=status_map, cutoff=cutoff)
            res = confirm_multi_cycle(
                evidences,
                cutoff=cutoff,
                core_signal=ChanSignal.WAIT,
                trend_confirm=True,
            )
            self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
            self.assertEqual(res.reason_code, "UNCONFIRMED_STRUCTURE")
            self.assertNotIn("5m", res.blocked_by)
            self.assertIn("core_signal", res.blocked_by)

        # 2. 5m 缺失（不在 evidences 中）：不影响 CONFIRMED 场景
        req_evidences = [
            _make_evidence(tf, status=BarStatus.CLOSED, cutoff=cutoff)
            for tf in REQUIRED_CYCLES
        ]
        res_no_5m = confirm_multi_cycle(
            req_evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )
        self.assertEqual(res_no_5m.outcome, ConfirmOutcome.CONFIRMED)
        self.assertTrue(res_no_5m.structure_gate.passed)
        self.assertFalse(res_no_5m.price_optimization_available)
        self.assertIn("5m", res_no_5m.unavailable_cycles)
        self.assertNotIn("5m", res_no_5m.blocked_by)
        self.assertEqual(res_no_5m.blocked_by, ())

        # 3. 5m 状态异常也不进入 blocked_by
        ev_stale_5m = _make_all_evidences(
            status_map={Timeframe.MIN_5: BarStatus.STALE}, cutoff=cutoff
        )
        res_stale_5m = confirm_multi_cycle(
            ev_stale_5m,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )
        self.assertEqual(res_stale_5m.outcome, ConfirmOutcome.CONFIRMED)
        self.assertFalse(res_stale_5m.price_optimization_available)
        self.assertNotIn("5m", res_stale_5m.blocked_by)

    def test_05_missing_weekly_is_wait_data_missing(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # 缺少 WEEKLY
        evidences = [
            _make_evidence(tf, status=BarStatus.CLOSED, cutoff=cutoff)
            for tf in ROLE_ORDER
            if tf != Timeframe.WEEKLY
        ]

        res = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )

        self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
        self.assertIn("weekly", res.unavailable_cycles)
        self.assertEqual(res.reason_code, "STRUCTURE_DATA_MISSING")
        self.assertIn("weekly", res.blocked_by)
        self.assertEqual(res.structure_gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(res.structure_gate.stamp.freshness, Freshness.UNKNOWN)
        self.assertFalse(res.structure_gate.passed)

    def test_06_insufficient_bars_is_wait_incomplete(self):
        cutoff = _aware_dt(hour=15, minute=0)
        # daily 的 bars 只有 2 根，少于 min_bars_per_cycle=3
        evidences = []
        for tf in ROLE_ORDER:
            cnt = 2 if tf == Timeframe.DAILY else 3
            evidences.append(
                _make_evidence(
                    tf, status=BarStatus.CLOSED, bar_count=cnt, cutoff=cutoff
                )
            )

        res = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
            min_bars_per_cycle=3,
        )

        self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
        self.assertIn("daily", res.unavailable_cycles)
        self.assertEqual(res.reason_code, "STRUCTURE_INCOMPLETE")
        self.assertIn("daily", res.blocked_by)
        self.assertEqual(res.structure_gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(res.structure_gate.stamp.freshness, Freshness.UNKNOWN)

    def test_07_status_missing_stale_invalid_is_wait_incomplete(self):
        cutoff = _aware_dt(hour=15, minute=0)
        for bad_status in (BarStatus.MISSING, BarStatus.STALE, BarStatus.INVALID):
            with self.subTest(status=bad_status):
                evidences = _make_all_evidences(
                    status_map={Timeframe.DAILY: bad_status},
                    cutoff=cutoff,
                )
                res = confirm_multi_cycle(
                    evidences,
                    cutoff=cutoff,
                    core_signal=ChanSignal.SECOND_BUY,
                    trend_confirm=True,
                )
                self.assertEqual(res.outcome, ConfirmOutcome.WAIT)
                self.assertIn("daily", res.unavailable_cycles)
                self.assertEqual(res.reason_code, "STRUCTURE_INCOMPLETE")
                self.assertIn("daily", res.blocked_by)
                self.assertEqual(
                    res.structure_gate.stamp.status, EvidenceStatus.NOT_READY
                )

    def test_08_value_errors_on_contract_violations(self):
        cutoff = _aware_dt(hour=15, minute=0)

        # 1. 不同 code
        ev1 = _make_evidence(Timeframe.DAILY, code="000001", cutoff=cutoff)
        ev2 = _make_evidence(Timeframe.MIN_120, code="000002", cutoff=cutoff)
        with self.assertRaises(ValueError):
            confirm_multi_cycle([ev1, ev2], cutoff=cutoff)

        # 2. 不同 market
        ev_m1 = _make_evidence(Timeframe.DAILY, market="SZ", cutoff=cutoff)
        ev_m2 = _make_evidence(Timeframe.MIN_120, market="SH", cutoff=cutoff)
        with self.assertRaises(ValueError):
            confirm_multi_cycle([ev_m1, ev_m2], cutoff=cutoff)

        # 3. evidence.cutoff 与 confirm_multi_cycle 入参 cutoff 不一致
        other_cutoff = cutoff - timedelta(days=1)
        ev_diff_cutoff = _make_evidence(Timeframe.DAILY, cutoff=other_cutoff)
        with self.assertRaises(ValueError):
            confirm_multi_cycle([ev_diff_cutoff], cutoff=cutoff)

        # 4. bar 时间超过 cutoff
        future_bar = KLine(
            time=cutoff + timedelta(minutes=5),
            open=10,
            high=11,
            low=9,
            close=10,
        )
        with self.assertRaises(ValueError):
            CycleEvidence(
                timeframe=Timeframe.DAILY,
                code="000001",
                market="SZ",
                bars=(future_bar,),
                status=BarStatus.CLOSED,
                cutoff=cutoff,
                source="test",
            )

        # 5. bars 乱序（递减）
        t1 = cutoff - timedelta(minutes=10)
        t2 = cutoff - timedelta(minutes=20)
        bars_disordered = (
            KLine(time=t1, open=10, high=11, low=9, close=10),
            KLine(time=t2, open=10, high=11, low=9, close=10),
        )
        with self.assertRaises(ValueError):
            CycleEvidence(
                timeframe=Timeframe.DAILY,
                code="000001",
                market="SZ",
                bars=bars_disordered,
                status=BarStatus.CLOSED,
                cutoff=cutoff,
                source="test",
            )

        # 6. bars 重复时间戳
        bars_duplicate = (
            KLine(time=t1, open=10, high=11, low=9, close=10),
            KLine(time=t1, open=10, high=11, low=9, close=10),
        )
        with self.assertRaises(ValueError):
            CycleEvidence(
                timeframe=Timeframe.DAILY,
                code="000001",
                market="SZ",
                bars=bars_duplicate,
                status=BarStatus.CLOSED,
                cutoff=cutoff,
                source="test",
            )

        # 7. timeframe 重复
        ev_dup1 = _make_evidence(Timeframe.DAILY, cutoff=cutoff)
        ev_dup2 = _make_evidence(Timeframe.DAILY, cutoff=cutoff)
        with self.assertRaises(ValueError):
            confirm_multi_cycle([ev_dup1, ev_dup2], cutoff=cutoff)

        # 8. 空 code / market / source
        with self.assertRaises(ValueError):
            _make_evidence(Timeframe.DAILY, code="", cutoff=cutoff)
        with self.assertRaises(ValueError):
            _make_evidence(Timeframe.DAILY, market="   ", cutoff=cutoff)
        with self.assertRaises(ValueError):
            _make_evidence(Timeframe.DAILY, source="", cutoff=cutoff)

    def test_09_type_and_value_errors_on_cutoff_and_flags(self):
        cutoff = _aware_dt(hour=15, minute=0)
        evidences = _make_all_evidences(cutoff=cutoff)

        # 1. cutoff 非 datetime
        with self.assertRaises(TypeError):
            confirm_multi_cycle(evidences, cutoff="2026-09-16T15:00:00Z")  # type: ignore[arg-type]

        # 2. cutoff 是 naive datetime
        naive_cutoff = datetime(2026, 9, 16, 15, 0, 0)
        with self.assertRaises(ValueError):
            confirm_multi_cycle(evidences, cutoff=naive_cutoff)

        # 3. trend_confirm=1 (严格 bool 校验)
        with self.assertRaises(TypeError):
            confirm_multi_cycle(evidences, cutoff=cutoff, trend_confirm=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            confirm_multi_cycle(evidences, cutoff=cutoff, trend_confirm="True")  # type: ignore[arg-type]

        # 4. min_bars_per_cycle 必须是正整数
        with self.assertRaises((ValueError, TypeError)):
            confirm_multi_cycle(evidences, cutoff=cutoff, min_bars_per_cycle=0)
        with self.assertRaises((ValueError, TypeError)):
            confirm_multi_cycle(evidences, cutoff=cutoff, min_bars_per_cycle=-2)
        with self.assertRaises((ValueError, TypeError)):
            confirm_multi_cycle(evidences, cutoff=cutoff, min_bars_per_cycle=True)  # type: ignore[arg-type]

        # 5. evidences 包含非 CycleEvidence
        with self.assertRaises(TypeError):
            confirm_multi_cycle(["not_cycle_evidence"], cutoff=cutoff)  # type: ignore[list-item]

        # 6. evidences 为空
        with self.assertRaises(ValueError):
            confirm_multi_cycle([], cutoff=cutoff)

    def test_10_interop_with_evaluate_gates(self):
        cutoff = _aware_dt(hour=15, minute=0)

        # 1. CONFIRMED 时结构闸门通过，整组七闸通过
        evidences_confirmed = _make_all_evidences(cutoff=cutoff)
        res_confirmed = confirm_multi_cycle(
            evidences_confirmed,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )
        self.assertEqual(res_confirmed.outcome, ConfirmOutcome.CONFIRMED)

        gates_pass = {
            "market": True,
            "asset": True,
            "fundamental": True,
            "valuation": True,
            "structure": res_confirmed.structure_gate,
            "risk": True,
            "execution": True,
        }
        eval_pass = evaluate_gates(gates_pass)
        self.assertTrue(eval_pass.passed)
        self.assertEqual(eval_pass.blocked_by, ())

        # 2. 非 CONFIRMED (如 PRECONFIRM 或 WAIT) 时整组七闸被 structure 阻塞
        status_map = {Timeframe.MIN_120: BarStatus.FORMING}
        evidences_preconfirm = _make_all_evidences(
            status_map=status_map, cutoff=cutoff
        )
        res_preconfirm = confirm_multi_cycle(
            evidences_preconfirm,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )
        self.assertEqual(res_preconfirm.outcome, ConfirmOutcome.PRECONFIRM)

        gates_block = {
            "market": True,
            "asset": True,
            "fundamental": True,
            "valuation": True,
            "structure": res_preconfirm.structure_gate,
            "risk": True,
            "execution": True,
        }
        eval_block = evaluate_gates(gates_block)
        self.assertFalse(eval_block.passed)
        self.assertEqual(eval_block.blocked_by, ("GATE_5_STRUCTURE",))

    def test_11_immutability_and_as_of_stamp_invariants(self):
        cutoff = _aware_dt(hour=15, minute=0)
        evidences = _make_all_evidences(cutoff=cutoff)
        res = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )

        # 1. 结果对象不可变
        with self.assertRaises(FrozenInstanceError):
            res.outcome = ConfirmOutcome.WAIT  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            res.code = "600000"  # type: ignore[misc]

        # 2. CycleEvidence 不可变
        ev = evidences[0]
        with self.assertRaises(FrozenInstanceError):
            ev.status = BarStatus.MISSING  # type: ignore[misc]

        # 3. structure_gate 不可变
        with self.assertRaises(FrozenInstanceError):
            res.structure_gate.complete = False  # type: ignore[misc]

        # 4. structure_gate.stamp 不可变
        with self.assertRaises(FrozenInstanceError):
            res.structure_gate.stamp.source = "other"  # type: ignore[misc]

        # 5. as_of 与 stamp 不变量
        self.assertLessEqual(res.as_of, cutoff)
        self.assertEqual(res.structure_gate.stamp.as_of, res.as_of)
        self.assertEqual(res.structure_gate.stamp.cutoff, cutoff)
        self.assertEqual(res.structure_gate.stamp.fetched_at, cutoff)
        self.assertIsInstance(res.structure_gate.stamp.market_date, date)
        self.assertNotIsInstance(res.structure_gate.stamp.market_date, datetime)
        self.assertEqual(
            res.structure_gate.stamp.market_date,
            cutoff.astimezone(SHANGHAI).date(),
        )

        # 6. outcome == CONFIRMED 等价于 structure_gate.passed
        self.assertEqual(
            res.outcome == ConfirmOutcome.CONFIRMED,
            res.structure_gate.passed,
        )

    def test_12_core_signal_normalization_and_buy_signals(self):
        cutoff = _aware_dt(hour=15, minute=0)
        evidences = _make_all_evidences(cutoff=cutoff)

        # 1. core_signal 为 None -> 归一化为 WAIT
        res_none = confirm_multi_cycle(
            evidences, cutoff=cutoff, core_signal=None, trend_confirm=True
        )
        self.assertEqual(res_none.core_signal, ChanSignal.WAIT)
        self.assertEqual(res_none.outcome, ConfirmOutcome.WAIT)
        self.assertIn("core_signal", res_none.blocked_by)

        # 2. core_signal 为有效字符串 -> 成功解析
        res_str = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal="SECOND_BUY",
            trend_confirm=True,
        )
        self.assertEqual(res_str.core_signal, ChanSignal.SECOND_BUY)
        self.assertEqual(res_str.outcome, ConfirmOutcome.CONFIRMED)

        # 3. core_signal 为非法字符串 -> 归一化为 WAIT，不抛异常穿透
        res_invalid = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal="INVALID_SIGNAL",
            trend_confirm=True,
        )
        self.assertEqual(res_invalid.core_signal, ChanSignal.WAIT)
        self.assertEqual(res_invalid.outcome, ConfirmOutcome.WAIT)
        self.assertIn("core_signal", res_invalid.blocked_by)

        # 4. 全部 BUY_SIGNALS 测试
        for sig in BUY_SIGNALS:
            with self.subTest(sig=sig):
                r = confirm_multi_cycle(
                    evidences, cutoff=cutoff, core_signal=sig, trend_confirm=True
                )
                self.assertEqual(r.outcome, ConfirmOutcome.CONFIRMED)
                self.assertTrue(r.structure_gate.passed)

        # 5. 非买点 (SELL_RISK) -> 只能是 WAIT + core_signal in blocked_by
        res_sell = confirm_multi_cycle(
            evidences,
            cutoff=cutoff,
            core_signal=ChanSignal.SELL_RISK,
            trend_confirm=True,
        )
        self.assertEqual(res_sell.outcome, ConfirmOutcome.WAIT)
        self.assertIn("core_signal", res_sell.blocked_by)
        self.assertEqual(res_sell.reason_code, "UNCONFIRMED_STRUCTURE")


if __name__ == "__main__":
    unittest.main()
