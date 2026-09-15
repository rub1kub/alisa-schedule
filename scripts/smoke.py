"""Exercise a running skill. Prints checks only, never upstream teacher names or IDs."""

import argparse
import json
import urllib.request

from app.config import Settings
from app.responses import Responses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18791")
    parser.add_argument("--path", default="/webhook")
    parser.add_argument("--skill-id", default="local-smoke")
    args = parser.parse_args()
    responses = Responses.load(Settings.from_env().responses_file)
    state = {"session": {}, "application": {}}
    for index, command in enumerate(
        ("группа сто три", "что завтра", "какая первая пара", "сколько пар")
    ):
        body = {
            "version": "1.0",
            "session": {
                "session_id": "local-test",
                "message_id": index,
                "skill_id": args.skill_id,
                "new": index == 0,
                "application": {"application_id": "local-test"},
            },
            "request": {"type": "SimpleUtterance", "command": command},
            "state": state,
        }
        request = urllib.request.Request(
            args.url.rstrip("/") + args.path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
        assert data["version"] == "1.0" and 0 < len(data["response"]["text"]) <= 1024
        assert responses.text("failure") not in data["response"]["text"]
        state["session"] = data.get("session_state", {})
        state["application"] = data.get("application_state", state["application"])
        if index == 0:
            assert state["application"].get("group_id") == "103-Д9-3ИНС"
        print(f"Dialog step {index + 1}: OK")


if __name__ == "__main__":
    main()
