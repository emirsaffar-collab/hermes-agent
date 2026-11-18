"""Cron job scheduling system for Hermes Agent."""

from cron.jobs import (
    create_job,
    get_job,
    list_jobs,
    mark_job_run,
    parse_schedule,
    remove_job,
    update_job,
)

__all__ = [  # noqa: F822
    "create_job",
    "get_job",
    "list_jobs",
    "mark_job_run",
    "parse_schedule",
    "remove_job",
    "tick",
    "update_job",
]


def __getattr__(name: str):
    if name == "tick":
        from cron.scheduler import tick

        return tick
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
