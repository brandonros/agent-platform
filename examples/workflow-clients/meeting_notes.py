"""Start meeting-notes extraction; optionally wait for its result."""
import argparse
from client import Client, display

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notes")
    parser.add_argument("--key", help="Idempotency key")
    parser.add_argument("--wait", action="store_true")
    options = parser.parse_args()
    client = Client()
    started = client.start("MeetingNotes", "meeting-notes", [{"notes": options.notes}], options.key)
    display(started)
    if options.wait:
        display(client.wait(started["id"]))
