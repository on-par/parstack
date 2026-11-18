"""A stand-in for gh used by test_file_slices.py. No network.

The tests copy this file to a temp dir as an executable named `gh`. It keeps
its state in state.json next to itself: every call, and every issue created.
`issue create` prints the new issue URL like gh does; numbers start at 41.
`issue view N --json ...` prints the stored issue. A title listed in
fail_titles (one per line, next to this file) makes `issue create` fail.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"


def opt(args, name):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]


def main(args):
    state = json.loads(STATE.read_text()) if STATE.exists() else {"next": 41, "issues": [], "calls": []}
    state["calls"].append(args)
    code = 0
    if args[:2] == ["issue", "create"]:
        repo, title = opt(args, "--repo")[0], opt(args, "--title")[0]
        body = Path(opt(args, "--body-file")[0]).read_text()
        fail = HERE / "fail_titles"
        if fail.exists() and title in fail.read_text().splitlines():
            print(f"could not create {title!r}", file=sys.stderr)
            code = 1
        else:
            number = state["next"]
            state["next"] += 1
            state["issues"].append({"number": number, "repo": repo, "title": title, "body": body,
                                    "labels": opt(args, "--label")})
            print(f"https://github.com/{repo}/issues/{number}")
    elif args[:2] == ["issue", "view"]:
        number = int(args[2])
        found = [i for i in state["issues"] if i["number"] == number]
        if found:
            i = found[0]
            print(json.dumps({"number": number, "title": i["title"], "body": i["body"],
                              "labels": [{"name": n} for n in i["labels"]],
                              "url": f"https://github.com/{i['repo']}/issues/{number}"}))
        else:
            print(f"no issue {number}", file=sys.stderr)
            code = 1
    else:
        print(f"fake gh: unsupported {args}", file=sys.stderr)
        code = 1
    STATE.write_text(json.dumps(state, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
