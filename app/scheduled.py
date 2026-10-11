"""Read-only, paginated projections of the backup scheduler's saved due times."""
import heapq
from datetime import datetime, timedelta, timezone

from .backup import next_run


PAGE_SIZE = 100


def scheduled_jobs(schedules, start, days, after=None, after_stack=""):
    end = start + timedelta(days=days)
    pending = []
    for schedule in schedules:
        if not schedule["enabled"]:
            continue
        due = datetime.fromisoformat(schedule["next_run"]) if schedule["next_run"] else datetime.fromisoformat(
            next_run(schedule["cron"], schedule["timezone"], start))
        name = schedule["stack"]
        if after is not None and (due, name) <= (after, after_stack):
            # Include ties belonging to stacks after the last displayed stack.
            base = after - timedelta(microseconds=1) if name > after_stack else after
            due = datetime.fromisoformat(next_run(schedule["cron"], schedule["timezone"], max(start, base)))
        if due <= end:
            heapq.heappush(pending, (due, name, schedule))

    jobs = []
    while pending and len(jobs) <= PAGE_SIZE:
        due, name, schedule = heapq.heappop(pending)
        jobs.append({"stack": name, "run_at": due.astimezone(timezone.utc).isoformat(),
                     "cron": schedule["cron"], "timezone": schedule["timezone"],
                     "overdue": due < start})
        if len(jobs) > PAGE_SIZE:
            break
        # Missed intervals collapse to one waiting run, just as BackupEngine.tick does.
        following = datetime.fromisoformat(next_run(schedule["cron"], schedule["timezone"], max(start, due)))
        if following <= end:
            heapq.heappush(pending, (following, name, schedule))

    has_more = len(jobs) > PAGE_SIZE
    jobs = jobs[:PAGE_SIZE]
    return {"days": days, "start": start.isoformat(), "end": end.isoformat(), "jobs": jobs,
            "next": {"after": jobs[-1]["run_at"], "after_stack": jobs[-1]["stack"]} if has_more else None}
