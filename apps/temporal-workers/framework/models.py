from dataclasses import dataclass

@dataclass
class Turn:
    # Workflow history remains the source for prompts; native tool context lives
    # in the service's persisted Claude session.
    messages: list[dict]
    model: str
    session_id: str = ""
    agent: str = ""  # Empty preserves the request identity of historical activities.
    output_schema: dict | None = None
    effort: str | None = None
    webui_chat_id: str | None = None


