"""Start a draft or send a human review decision to an existing execution."""
import argparse
from client import Client, display

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("topic")
    start.add_argument("--max-rounds", type=int, default=2)
    start.add_argument("--key", help="Idempotency key")
    for decision in ("approve", "revise", "reject"):
        command = commands.add_parser(decision)
        command.add_argument("id")
        command.add_argument("note", nargs="?", default="")
    options = parser.parse_args()
    client = Client()
    if options.command == "start":
        display(client.start("DraftReviewWebUI", "draft-review-webui",
                             [{"topic": options.topic, "max_rounds": options.max_rounds}], options.key))
    else:
        display(client.signal(options.id, "submit_review", [options.command, options.note]))
