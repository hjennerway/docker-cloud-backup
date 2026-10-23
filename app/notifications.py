"""Plain-text notification templates with explicit, safe placeholders."""
from string import Formatter

from .discovery import BackupError


EVENTS = ("success", "partial", "failed", "test")
PLACEHOLDERS = {"stack", "status", "detail", "job_id"}
TEMPLATE_DEFAULTS = {}
for event in EVENTS:
    TEMPLATE_DEFAULTS[f"notification_{event}_title"] = (
        "Cloud Backup test" if event == "test" else "Cloud Backup: {status}")
    TEMPLATE_DEFAULTS[f"notification_{event}_message"] = (
        "{detail}" if event == "test" else "{stack}\n{detail}")


def validate_templates(settings):
    for key in TEMPLATE_DEFAULTS:
        value = settings[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 4000 or "\0" in value:
            raise BackupError(f"Enter a notification template of 1–4000 characters for {key}.")
        if key.endswith("_title") and any(c in value for c in "\r\n"):
            raise BackupError("Notification titles must use a single line.")
        try:
            for _, field, spec, conversion in Formatter().parse(value):
                if field is not None and (field not in PLACEHOLDERS or spec or conversion):
                    raise ValueError()
        except ValueError:
            raise BackupError(f"Invalid placeholder in {key}. Use {{stack}}, {{status}}, {{detail}} or {{job_id}}. Use doubled braces for literal braces.") from None


def render_notification(settings, event, *, stack="", detail="", job_id=""):
    context = dict(stack=stack, status=event, detail=detail, job_id=job_id)
    return tuple(settings[f"notification_{event}_{field}"].format_map(context)
                 for field in ("title", "message"))
