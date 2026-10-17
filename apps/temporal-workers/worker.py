"""One launcher; choose the independently deployed worker with WORKER_KIND."""
import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker
from framework.webui import create_chat, claude_turn


async def main():
    kind = os.environ.get("WORKER_KIND", "draft-review")
    if kind == "draft-review":
        from workflows.draft_review import DraftReviewWebUI
        from workflows.capabilities import AgentCapabilities
        workflows, activities = [DraftReviewWebUI, AgentCapabilities], [create_chat, claude_turn]
        default_queue = "draft-review-webui"
    elif kind == "meeting-notes":
        from workflows.meeting_notes import MeetingNotes
        workflows, activities = [MeetingNotes], [create_chat, claude_turn]
        default_queue = "meeting-notes"
    else:
        raise ValueError(f"Unknown WORKER_KIND: {kind}")
    logging.basicConfig(level=logging.INFO)
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal-dev-server.chat.svc.cluster.local:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    queue = os.getenv("TEMPORAL_TASK_QUEUE", default_queue)
    logging.info("worker up: kind=%s queue=%s", kind, queue)
    await Worker(client, task_queue=queue, workflows=workflows, activities=activities,
                 max_concurrent_activities=int(os.getenv("MAX_CONCURRENT_ACTIVITIES", "4"))).run()


if __name__ == "__main__":
    asyncio.run(main())
