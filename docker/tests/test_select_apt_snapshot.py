import unittest
from unittest.mock import patch

from docker import select_apt_snapshot as snapshot


class SelectAptSnapshot(unittest.TestCase):
    head = "1" * 40
    merge = "2" * 40
    head_date = "2025-11-24T08:21:45-08:00"
    merge_date = "2025-11-25T09:02:23+00:00"

    def setUp(self):
        self.pull = {
            "merge_commit_sha": self.merge,
            "merged_at": self.merge_date,
            "head": {"sha": self.head},
            "base": {"ref": "main", "repo": {"full_name": snapshot.REPOSITORY}},
        }

    def test_main_uses_the_tested_head_time_after_a_squash_merge(self):
        with (
            patch.object(
                snapshot.subprocess,
                "check_output",
                side_effect=[f"{self.head}\n{self.head_date}", f"{self.merge}\n{self.merge_date}"],
            ),
            patch.object(
                snapshot,
                "api",
                side_effect=[
                    [self.pull],
                    {
                        "sha": self.head,
                        "commit": {"committer": {"date": "2025-11-24T16:21:45Z"}},
                    },
                ],
            ) as api,
        ):
            branch = snapshot.debian_snapshot(self.head, canonical_main=False)
            api.assert_not_called()
            main = snapshot.debian_snapshot(self.merge, canonical_main=True)
        self.assertEqual(branch, ("20251124T162145Z", self.head))
        self.assertEqual(main, branch)

    def test_direct_main_push_ignores_pulls_that_did_not_create_the_commit(self):
        self.pull["merge_commit_sha"] = "3" * 40
        with (
            patch.object(
                snapshot.subprocess, "check_output", return_value=f"{self.merge}\n{self.head_date}"
            ),
            patch.object(snapshot, "api", return_value=[self.pull]) as api,
        ):
            self.assertEqual(
                snapshot.debian_snapshot(self.merge, canonical_main=True),
                ("20251124T162145Z", self.merge),
            )
        api.assert_called_once_with(f"commits/{self.merge}/pulls?per_page=100")

    def test_checkout_must_match_the_workflow_revision(self):
        with patch.object(
            snapshot.subprocess, "check_output", return_value=f"{self.head}\n{self.head_date}"
        ):
            with self.assertRaisesRegex(ValueError, "checkout"):
                snapshot.debian_snapshot(self.merge, canonical_main=False)

    def test_main_fails_if_github_cannot_confirm_the_pull_request(self):
        with (
            patch.object(
                snapshot.subprocess, "check_output", return_value=f"{self.merge}\n{self.head_date}"
            ),
            patch.object(snapshot, "api", side_effect=OSError),
        ):
            with self.assertRaises(OSError):
                snapshot.debian_snapshot(self.merge, canonical_main=True)
