"""Start the agent, skill, MCP, and structured-output demonstration."""
import argparse
from client import Client, display

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", help="Idempotency key")
    parser.add_argument("--wait", action="store_true")
    options = parser.parse_args()
    client = Client()
    started = client.start("AgentCapabilities", "draft-review-webui", [], options.key)
    display(started)
    if options.wait:
        display(client.wait(started["id"]))
