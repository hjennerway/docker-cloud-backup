import pytest

from app.discovery import BackupError
from app.destinations import validate_settings
from app.notifications import EVENTS, render_notification
from app.store import Store


@pytest.mark.parametrize("event", EVENTS)
def test_custom_templates_persist_and_render(tmp_path, event):
    store = Store(tmp_path)
    store.save_settings({f"notification_{event}_title": "{stack}: {status}",
                         f"notification_{event}_message": "Job {job_id}\n{detail}\n{{literal}}"})
    settings = store.settings()
    validate_settings(settings)
    assert render_notification(settings, event, stack="media", detail="Error {unexpanded}", job_id="123") == (
        f"media: {event}", "Job 123\nError {unexpanded}\n{literal}")


@pytest.mark.parametrize("template", ["{unknown}", "{stack.__class__}", "{stack!r}", "{stack:>10}", "{", "", "x" * 4001])
def test_invalid_templates_rejected(tmp_path, template):
    settings = Store(tmp_path).settings()
    settings["notification_failed_message"] = template
    with pytest.raises(BackupError):
        validate_settings(settings)


def test_default_notification_preserves_existing_message(tmp_path):
    settings = Store(tmp_path).settings()
    assert render_notification(settings, "success", stack="media", detail="Complete") == (
        "Cloud Backup: success", "media\nComplete")
    settings["notification_success_title"] = "Title\nInjected header"
    with pytest.raises(BackupError):
        validate_settings(settings)
