"""The API's producer-only Celery client (design.md §6.2a) — stub until task 4.4."""


def enqueue_process_upload(file_id: str, organization_id: str) -> None:
    """Publish process_upload for one file. Raises if Redis is unreachable."""
    raise NotImplementedError("tasks_client is built in task 4.4")
