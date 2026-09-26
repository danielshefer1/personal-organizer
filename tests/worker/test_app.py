from __future__ import annotations

from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.system import PING_TASK


class TestProcrastinateApp:
    def test_tasks_are_registered(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert PING_TASK in app.tasks

    def test_worker_connects_as_the_runtime_role(self, settings: Settings) -> None:
        """Never as the owner: the worker must own nothing and run under RLS like
        everything else at runtime."""
        app = build_procrastinate_app(settings)
        pool_args = app.connector._pool_args  # type: ignore[attr-defined]
        assert "app_user" in pool_args["conninfo"]

    def test_ping_is_on_the_maintenance_queue(self, settings: Settings) -> None:
        app = build_procrastinate_app(settings)
        assert app.tasks[PING_TASK].queue == Queue.MAINTENANCE.value

    def test_building_twice_does_not_double_prefix_task_names(self, settings: Settings) -> None:
        """App.add_tasks_from mutates a Blueprint in place, so sharing one across two Apps
        produced `system:system:ping`. Registration functions avoid the shared state."""
        first = build_procrastinate_app(settings)
        second = build_procrastinate_app(settings)
        assert PING_TASK in first.tasks
        assert PING_TASK in second.tasks


class TestQueues:
    def test_every_queue_name_is_distinct(self) -> None:
        """A slow LLM turn sharing a queue with ingress would starve webhook
        acknowledgement, and Meta retries anything it does not see acked."""
        names = [queue.value for queue in Queue]
        assert len(names) == len(set(names))

    def test_default_subscriptions_exclude_the_agent_queue(self, settings: Settings) -> None:
        assert Queue.AGENT.value not in settings.worker.queues

    def test_default_subscriptions_include_the_queue_ping_runs_on(self, settings: Settings) -> None:
        """Dropping `maintenance` from WORKER__QUEUES breaks the walking-skeleton proof
        silently: the api still returns 200 {"deferred": true} and no worker ever runs it."""
        app = build_procrastinate_app(settings)
        assert app.tasks[PING_TASK].queue in settings.worker.queues
