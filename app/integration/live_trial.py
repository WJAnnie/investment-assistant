"""Explicit private ledger lifecycle; never schedule, collect or send messages."""

import argparse
from datetime import datetime
import json

from app.utils.private_storage import private_path, read_private_bytes, require_private_execution, strict_json
from app.workflow.live_acceptance import LiveTrialLedger, ReceiptEvent
from app.workflow.portfolio import portfolio_rule_version


def build_parser():
    parser = argparse.ArgumentParser(description="Manage unverified private five-day observations.")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Freeze a caller-supplied calendar before trial start.")
    initialize.add_argument("--ledger", required=True)
    initialize.add_argument("--calendar", required=True, help="Private strict JSON calendar manifest.")
    initialize.add_argument("--start", required=True, help="First candidate date (YYYY-MM-DD); never auto-selected.")
    status = commands.add_parser("status", help="Read observation status, not an acceptance certificate.")
    status.add_argument("--ledger", required=True)
    receipt = commands.add_parser("record-receipt", help="Append a caller-supplied receipt; does not verify delivery.")
    receipt.add_argument("--ledger", required=True)
    receipt.add_argument("--receipt-file", required=True, help="Private JSON receipt referencing an existing run.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        require_private_execution()
        path = private_path(args.ledger)
        if args.command == "init":
            calendar = strict_json(read_private_bytes(args.calendar))
            required = {"schema", "source", "ordered_trading_dates", "calendar_coverage", "verified"}
            if (not isinstance(calendar, dict) or set(calendar) != required
                    or calendar["schema"] != "private_trial_calendar.v1"
                    or type(calendar["verified"]) is not bool
                    or type(calendar["ordered_trading_dates"]) is not list
                    or type(calendar["calendar_coverage"]) is not list):
                raise ValueError("invalid private trial calendar manifest")
            ledger = LiveTrialLedger.create(
                path, trial_start=args.start, rule_version=portfolio_rule_version(),
                calendar_source=calendar["source"],
                ordered_trading_dates=calendar["ordered_trading_dates"],
                calendar_coverage=calendar["calendar_coverage"],
                calendar_verified=calendar["verified"],
            )
            result = {"status": "initialized_unverified", "trial_dates": list(ledger.trial_dates),
                      "rule_version": ledger.rule_version, "independently_verified": False,
                      "scheduler_started": False, "auto_execute": False}
        else:
            ledger = LiveTrialLedger.load(path)
            if args.command == "status":
                result = ledger.summary()
            else:
                payload = strict_json(read_private_bytes(args.receipt_file))
                if not isinstance(payload, dict):
                    raise ValueError("receipt must be a JSON object")
                payload["receipt_observed_at"] = datetime.fromisoformat(payload["receipt_observed_at"])
                record = ledger.append_receipt(ReceiptEvent(**payload))
                result = {"status": "recorded_unverified", "entry_hash": record["entry_hash"],
                          "independently_verified": False}
    except Exception as exc:
        # Paths, input values and provider exception strings can contain private
        # data. Return only the category, without a traceback or original text.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    # Zero means the requested read/write succeeded, never five-day acceptance.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
