"""Choose a Debian archive time that survives merging a tested PR head."""

import datetime
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "SatoshiAndKin/web3-proxy"
SHA = re.compile(r"[0-9a-f]{40}")


def api(path: str) -> dict | list:
    # Both endpoints are public. The build token needs no additional permission.
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def merged_pull_request_head(revision: str) -> str | None:
    pulls = api(f"commits/{revision}/pulls?per_page=100")
    heads = {
        pull["head"]["sha"]
        for pull in pulls
        if pull["merge_commit_sha"] == revision
        and pull["merged_at"] is not None
        and pull["base"]["ref"] == "main"
        and pull["base"]["repo"]["full_name"] == REPOSITORY
    }
    if len(heads) > 1:
        raise ValueError("Multiple pull request heads match the main commit.")
    if not heads:
        return None
    head = heads.pop()
    if not SHA.fullmatch(head):
        raise ValueError("Invalid pull request head revision.")
    return head


def debian_snapshot(revision: str, canonical_main: bool) -> tuple[str, str]:
    if not SHA.fullmatch(revision):
        raise ValueError("Require a full source commit.")
    checkout = subprocess.check_output(
        ["git", "show", "-s", "--format=%H%n%cI", "HEAD"],
        text=True,
        timeout=10,
    )
    checked_out, date = checkout.splitlines()
    if checked_out != revision:
        raise ValueError("The checkout does not match the build revision.")

    # Branch pushes use the exact tested head. GitHub records that same head
    # against the main commit even when a PR is squash merged.
    source = merged_pull_request_head(revision) if canonical_main else None
    if source is not None and source != revision:
        commit = api(f"commits/{source}")
        if commit["sha"] != source:
            raise ValueError("GitHub returned a different commit.")
        date = commit["commit"]["committer"]["date"]

    selected = datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
    selected = selected.astimezone(datetime.timezone.utc)
    if selected > datetime.datetime.now(datetime.timezone.utc):
        raise ValueError("The source commit date is in the future.")
    # The archive serves the newest import at or before the requested UTC time.
    return selected.strftime("%Y%m%dT%H%M%SZ"), source or revision


def main() -> None:
    timestamp, source = debian_snapshot(
        os.environ["GITHUB_SHA"],
        os.environ["GITHUB_REPOSITORY"] == REPOSITORY
        and os.environ["GITHUB_REF"] == "refs/heads/main",
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"timestamp={timestamp}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"Debian APT snapshot `{timestamp}` as of source `{source}`.\n")


if __name__ == "__main__":
    main()
