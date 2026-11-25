"""Select exact Debian imports and reuse those from a successful branch build."""

import datetime
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from docker.reuse_production_image import REPOSITORY, WORKFLOW, api, output

SHA = re.compile(r"[0-9a-f]{40}")
TIMESTAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
FILENAME = "debian-apt.json"
RECEIPT_LIMIT = 4096


def merged_pull_request_head(revision: str) -> str | None:
    # Use the scoped build token instead of GitHub's shared anonymous quota.
    pulls = api(f"commits/{revision}/pulls?per_page=100")

    heads = {
        pull["head"]["sha"]
        for pull in pulls
        if pull["merge_commit_sha"] == revision
        and pull["merged_at"] is not None
        and pull["base"]["ref"] == "main"
        and pull["base"]["repo"]["full_name"] == REPOSITORY
        and (pull["head"]["repo"] or {}).get("full_name") == REPOSITORY
    }
    if len(heads) > 1:
        raise ValueError("Multiple pull request heads match the main commit.")
    if not heads:
        return None
    head = heads.pop()
    if not SHA.fullmatch(head):
        raise ValueError("Invalid pull request head revision.")
    return head


def exact_archive(archive: str, requested: str) -> str:
    prefix = f"https://snapshot.debian.org/archive/{archive}/"
    request = Request(f"{prefix}{requested}/", method="HEAD")
    with urlopen(request, timeout=20) as response:
        final_url = response.geturl()
    actual = final_url.removeprefix(prefix).removesuffix("/")
    if not TIMESTAMP.fullmatch(actual) or actual > requested:
        raise ValueError("Debian did not resolve a completed archive import.")
    return actual


def receipt_data(payload: bytes, source: str) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if (
                archive.namelist() != [FILENAME]
                or archive.getinfo(FILENAME).file_size > RECEIPT_LIMIT
            ):
                return None
            with archive.open(FILENAME) as file:
                data = json.loads(file.read(RECEIPT_LIMIT))
        if data["source"] != source:
            return None
        if TIMESTAMP.fullmatch(data["archive"]) and TIMESTAMP.fullmatch(data["security"]):
            return data["archive"], data["security"]
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile):
        return None
    return None


def tested_imports(source: str) -> tuple[str, str] | None:
    runs = api(
        "actions/workflows/production-image.yml/runs"
        f"?event=push&head_sha={source}&status=success&per_page=10"
    )["workflow_runs"]
    for run in runs:
        if (
            run["path"] != WORKFLOW
            or run["head_sha"] != source
            or run["event"] != "push"
            or run["conclusion"] != "success"
            or run["head_repository"]["full_name"] != REPOSITORY
        ):
            continue

        run_id = int(run["id"])
        attempt = int(run["run_attempt"])
        name = f"debian-apt-{source}-{attempt}"
        artifacts = api(f"actions/runs/{run_id}/artifacts?name={name}")["artifacts"]
        receipts = [
            artifact for artifact in artifacts
            if artifact["name"] == name
            and not artifact["expired"]
            and 0 < artifact["size_in_bytes"] <= RECEIPT_LIMIT
        ]
        if len(receipts) != 1:
            continue

        artifact_id = int(receipts[0]["id"])
        payload = subprocess.check_output(
            ["gh", "api", f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
            timeout=20,
        )
        if len(payload) > RECEIPT_LIMIT:
            continue
        snapshots = receipt_data(payload, source)
        if snapshots:
            print(f"Reuse the Debian imports from successful branch CI run {run_id}.")
            return snapshots
    return None


def select(revision: str, canonical_main: bool) -> tuple[str, str]:
    if not SHA.fullmatch(revision) or output("git", "rev-parse", "HEAD") != revision:
        raise ValueError("The checkout does not match the build revision.")

    if canonical_main:
        head = merged_pull_request_head(revision)
        if head is not None:
            snapshots = tested_imports(head)
            if snapshots is not None:
                return snapshots
        print("No matching successful branch receipt. Validate new imports in the main build.")

    requested = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return exact_archive("debian", requested), exact_archive("debian-security", requested)


def main() -> None:
    revision = os.environ["GITHUB_SHA"]
    archive, security = select(
        revision,
        os.environ["GITHUB_REPOSITORY"] == REPOSITORY
        and os.environ["GITHUB_REF"] == "refs/heads/main",
    )
    receipt = {"source": revision, "archive": archive, "security": security}
    (Path(os.environ["RUNNER_TEMP"]) / FILENAME).write_text(json.dumps(receipt) + "\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output_file:
        output_file.write(f"archive={archive}\nsecurity={security}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"Debian imports `{archive}` and security `{security}`.\n")


if __name__ == "__main__":
    main()
