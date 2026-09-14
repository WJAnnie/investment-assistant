"""Command-line entry point for the investment research workflow."""

import argparse
import json
import sys
from datetime import datetime

from app.utils.logger import get_logger
from app.market.factory import create_default_collector
from app.market.collector import MarketCollector
from app.market.sina import SinaProvider
from app.notify.feishu import FeishuNotifier
from app.report.market_snapshot import DEFAULT_MARKET_CODES, run_market_snapshot
from app.utils.serialization import to_jsonable
from app.utils.private_storage import require_private_execution
from app.workflow.batch import run_daily_reports
from app.workflow.portfolio import run_portfolio_report
from app.workflow.reminders import SHANGHAI_TZ, build_reminder_plan
from app.workflow.scheduler import start_scheduler
from app.workflow.stage_store import StageStore
from app.workflow.live_acceptance import LiveTrialLedger

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - requirements include python-dotenv
    load_dotenv = None


logger = get_logger(__name__)


class _StoreExplicit(argparse.Action):
    """Retain explicit report/history options, even when equal to defaults."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace._explicit_options = (
            *getattr(namespace, "_explicit_options", ()), option_string
        )


def _aware_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--at requires an ISO timestamp with a timezone offset"
        ) from None
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "--at requires an ISO timestamp with a timezone offset"
        )
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run investment research reports or preview manual reminder checklists."
    )
    parser.add_argument(
        "--code",
        dest="codes",
        action="append",
        help="A-share or ETF code; repeat this option for multiple codes.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--snapshot",
        action="store_true",
        help="Only fetch a realtime market snapshot; --code is optional in this mode.",
    )
    modes.add_argument(
        "--schedule",
        action="store_true",
        help="Run the weekday Feishu scheduler at the configured report times.",
    )
    modes.add_argument(
        "--portfolio",
        action="store_true",
        help="Run one A/B/combined portfolio research reminder report.",
    )
    modes.add_argument(
        "--reminder-plan",
        action="store_true",
        help="Preview today's manual checklists offline; never schedule or notify.",
    )
    parser.add_argument(
        "--at",
        type=_aware_timestamp,
        help="Preview time as an aware ISO timestamp; requires --reminder-plan.",
    )
    parser.add_argument(
        "--schedule-profile",
        choices=("legacy", "four-stage", "full-day"),
        help="Select five legacy reports, four analysis stages, or nine checklists; requires --schedule.",
    )
    parser.add_argument(
        "--private-state-dir",
        help="Opt-in private stage evidence directory; only for portfolio/report schedules.",
    )
    parser.add_argument(
        "--live-ledger",
        help="Existing private observation ledger; requires four-stage schedule and private state.",
    )
    parser.add_argument(
        "--report-kind",
        action=_StoreExplicit,
        choices=("global", "morning", "midday", "trading", "closing"),
        default="closing",
        help="Portfolio report stage; used only with --portfolio.",
    )
    parser.add_argument(
        "--start", action=_StoreExplicit, help="History start date, e.g. 20260101."
    )
    parser.add_argument(
        "--end", action=_StoreExplicit, help="History end date, e.g. 20260911."
    )
    parser.add_argument(
        "--period",
        action=_StoreExplicit,
        choices=("daily", "weekly", "monthly", "1", "5", "15", "30", "60"),
        default="daily",
        help="K-line period; minute periods use realtime intraday data when supported.",
    )
    parser.add_argument(
        "--adjust",
        action=_StoreExplicit,
        choices=("", "qfq", "hfq"),
        default="",
        help="AkShare adjustment mode for historical bars.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not send Feishu notifications even when FEISHU_WEBHOOK is configured.",
    )
    return parser


def _json_safe(value):
    """Backward-compatible alias for callers and tests."""
    return to_jsonable(value)


def _print_json(value):
    """Print JSON without crashing on legacy Windows console encodings."""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    ensure_ascii = "utf" not in encoding
    print(json.dumps(_json_safe(value), ensure_ascii=ensure_ascii, indent=2))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.at is not None and not args.reminder_plan:
        parser.error("--at requires --reminder-plan")
    if args.schedule_profile is not None and not args.schedule:
        parser.error("--schedule-profile requires --schedule")
    if args.private_state_dir is not None and (
        not (args.portfolio or args.schedule) or args.schedule_profile == "full-day"
    ):
        parser.error("--private-state-dir requires --portfolio or a report schedule")
    if args.live_ledger is not None and (
        not args.schedule or args.schedule_profile != "four-stage" or args.private_state_dir is None
    ):
        parser.error("--live-ledger requires --schedule --schedule-profile four-stage and --private-state-dir")
    if args.reminder_plan:
        if args.codes or getattr(args, "_explicit_options", ()):
            parser.error(
                "--reminder-plan cannot be combined with --code or report/history options"
            )
        now = datetime.now(SHANGHAI_TZ) if args.at is None else args.at
        try:
            result = build_reminder_plan(now=now)
        except ValueError:
            parser.error("reminder preview time is outside the supported datetime range")
        _print_json(result)
        return 0

    if args.schedule:
        if args.codes:
            parser.error("--schedule cannot be combined with --code")
        if args.no_notify:
            parser.error("--schedule cannot use --no-notify; use --reminder-plan to preview")
    if args.portfolio and args.codes:
        parser.error("--portfolio cannot be combined with --code")
    if not args.portfolio and args.report_kind != "closing":
        parser.error("--report-kind requires --portfolio")
    if not (args.snapshot or args.schedule or args.portfolio) and not args.codes:
        parser.error("--code is required unless a report, schedule, or preview mode is used")

    private_options = {}
    try:
        if args.portfolio or (args.schedule and args.schedule_profile != "full-day"):
            require_private_execution()
        if args.private_state_dir is not None:
            private_options["private_state_dir"] = StageStore(args.private_state_dir).directory
        if args.live_ledger is not None:
            private_options["live_ledger"] = LiveTrialLedger.load(args.live_ledger).path
    except (OSError, ValueError, TypeError):
        parser.error("private state or execution boundary validation failed")

    if load_dotenv is not None:
        load_dotenv()
    if args.schedule:
        if args.schedule_profile is not None:
            private_options["profile"] = args.schedule_profile
        start_scheduler(**private_options)
        return 0

    notifier = None if args.no_notify else FeishuNotifier()
    if args.portfolio:
        result = run_portfolio_report(
            report_kind=args.report_kind,
            notifier=notifier,
            **private_options,
        )
    elif args.snapshot:
        codes = args.codes or DEFAULT_MARKET_CODES
        logger.info("Investment Assistant started for %s", ", ".join(codes))
        result = run_market_snapshot(
            codes,
            collector=MarketCollector(primary=SinaProvider()),
            notifier=notifier,
        )
    else:
        logger.info("Investment Assistant started for %s", ", ".join(args.codes))
        result = run_daily_reports(
            args.codes,
            collector=create_default_collector(),
            notifier=notifier,
            history_start=args.start,
            history_end=args.end,
            history_period=args.period,
            history_adjust=args.adjust,
        )
    _print_json(result)
    if args.portfolio:
        return 1 if result.get("status") == "failed" else 0
    return 0 if result.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
