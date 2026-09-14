from collections.abc import Iterable

from .daily import daily_report_job


def run_daily_reports(codes: Iterable[str], collector, notifier=None, **job_kwargs):
    """Run one research report per code and keep failures isolated."""
    if collector is None:
        raise ValueError("collector is required")

    symbols = list(codes or [])
    if not symbols or any(not isinstance(code, str) or not code.strip() for code in symbols):
        raise ValueError("codes must contain at least one non-empty string")

    options = dict(job_kwargs)
    options.setdefault("fetch_history", True)
    results = []
    for code in symbols:
        try:
            results.append(
                daily_report_job(
                    code,
                    collector=collector,
                    notifier=notifier,
                    **options,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "code": code,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    succeeded = sum(result.get("status") == "completed" for result in results)
    failed = len(results) - succeeded
    return {
        "status": "completed" if failed == 0 else "partial",
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
