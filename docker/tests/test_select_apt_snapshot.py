import io
import json
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from docker import select_apt_snapshot as snapshot


class SelectAptSnapshot(unittest.TestCase):
    head = "1" * 40
    merge = "2" * 40
    archive = "20261124T150911Z"
    security = "20261124T175831Z"

    def receipt_zip(self, source):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as receipt:
            receipt.writestr(snapshot.FILENAME, json.dumps({
                "source": source,
                "archive": self.archive,
                "security": self.security,
            }))
        return archive.getvalue()

    def test_main_uses_exact_tested_branch_imports(self):
        with (
            patch.object(snapshot, "output", return_value=self.merge),
            patch.object(snapshot, "merged_pull_request_head", return_value=self.head),
            patch.object(snapshot, "tested_imports", return_value=(self.archive, self.security)) as tested,
            patch.object(snapshot, "exact_archive") as resolve,
        ):
            self.assertEqual(
                snapshot.select(self.merge, canonical_main=True),
                (self.archive, self.security),
            )
        tested.assert_called_once_with(self.head)
        resolve.assert_not_called()

    def test_main_without_a_matching_receipt_selects_both_imports_for_a_new_build(self):
        with (
            patch.object(snapshot, "output", return_value=self.merge),
            patch.object(snapshot, "merged_pull_request_head", return_value=self.head),
            patch.object(snapshot, "tested_imports", return_value=None),
            patch.object(snapshot, "exact_archive", side_effect=[self.archive, self.security]) as resolve,
        ):
            self.assertEqual(
                snapshot.select(self.merge, canonical_main=True),
                (self.archive, self.security),
            )
        self.assertEqual([call.args[0] for call in resolve.call_args_list], ["debian", "debian-security"])

    def test_main_association_uses_the_exact_canonical_merged_pull(self):
        pull = {
            "merge_commit_sha": self.merge,
            "merged_at": "2026-11-24T18:00:00Z",
            "head": {"sha": self.head, "repo": {"full_name": snapshot.REPOSITORY}},
            "base": {"ref": "main", "repo": {"full_name": snapshot.REPOSITORY}},
        }
        with patch.object(snapshot, "api", return_value=[pull]) as request:
            self.assertEqual(snapshot.merged_pull_request_head(self.merge), self.head)
        request.assert_called_once_with(f"commits/{self.merge}/pulls?per_page=100")
        pull["merge_commit_sha"] = "3" * 40
        with patch.object(snapshot, "api", return_value=[pull]):
            self.assertIsNone(snapshot.merged_pull_request_head(self.merge))

    def test_checkout_must_match_the_workflow_revision(self):
        with patch.object(snapshot, "output", return_value=self.head):
            with self.assertRaisesRegex(ValueError, "checkout"):
                snapshot.select(self.merge, canonical_main=False)

    def test_archive_uses_completed_import_id_from_redirect(self):
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            f"https://snapshot.debian.org/archive/debian/{self.archive}/"
        )
        with patch.object(snapshot, "urlopen", return_value=response):
            self.assertEqual(snapshot.exact_archive("debian", self.security), self.archive)

    def test_only_successful_canonical_push_can_supply_the_named_receipt(self):
        run = {
            "id": 42,
            "run_attempt": 2,
            "path": snapshot.WORKFLOW,
            "head_sha": self.head,
            "head_repository": {"full_name": snapshot.REPOSITORY},
            "event": "push",
            "conclusion": "success",
        }
        payload = self.receipt_zip(self.head)
        artifact = {
            "name": f"debian-apt-{self.head}-2",
            "expired": False,
            "size_in_bytes": len(payload),
            "id": 123,
        }
        with (
            patch.object(snapshot, "api", side_effect=[{"workflow_runs": [run]}, {"artifacts": [artifact]}]),
            patch.object(snapshot.subprocess, "check_output", return_value=payload) as download,
        ):
            self.assertEqual(snapshot.tested_imports(self.head), (self.archive, self.security))
        download.assert_called_once_with(
            ["gh", "api", f"/repos/{snapshot.REPOSITORY}/actions/artifacts/123/zip"],
            timeout=20,
        )
        run["conclusion"] = "failure"
        with (
            patch.object(snapshot, "api", return_value={"workflow_runs": [run]}),
            patch.object(snapshot.subprocess, "check_output") as download,
        ):
            self.assertIsNone(snapshot.tested_imports(self.head))
        download.assert_not_called()

    def test_receipt_is_bound_to_exact_source_revision(self):
        self.assertIsNone(snapshot.receipt_data(self.receipt_zip(self.head), self.merge))
