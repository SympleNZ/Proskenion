"""The recovery web interface's routes: forms, confirmation tokens, refusals.

Nothing here drives a real disk — the destructive job functions
(`_do_partition_and_image`, `_do_restore`) are exercised by the Docker loop-
device stage (`appliance/tests/verify-recovery-in-docker.sh`). What belongs
in a fast, off-device suite is what these tests cover: that every page
renders, that a destructive action is never executed by the first POST, that
the second POST is refused without a matching token, and that the read-only
diagnostics routes never require one at all.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def client(webapp_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(webapp_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(webapp_module, "list_target_disks", lambda: [])
    monkeypatch.setattr(webapp_module, "find_images", lambda: [])
    monkeypatch.setattr(webapp_module, "find_archives", lambda: [])
    monkeypatch.setattr(
        webapp_module.network_lib,
        "current_devices",
        lambda run=None: [("eth0", "ethernet", "connected")],
    )
    app = webapp_module.create_app()
    app.config["TESTING"] = True
    return app.test_client()


class TestIndex:
    def test_renders(self, client: Any) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"Proskenion recovery" in response.data


class TestPartitionFlow:
    def test_get_shows_the_form(self, client: Any) -> None:
        response = client.get("/partition")
        assert response.status_code == 200
        assert b"Partition and image a new SSD" in response.data

    def test_first_post_only_reviews_never_executes(
        self,
        webapp_module: ModuleType,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        image_path = tmp_path / "capture.img"
        image_path.write_bytes(b"not a real package")

        class FakeManifest:
            type = "image"
            version = "v1.0.0"

        monkeypatch.setattr(webapp_module.image_lib, "check_anchors_present", lambda d: None)
        monkeypatch.setattr(
            webapp_module.image_lib, "verify_image", lambda p, anchors_dir: FakeManifest()
        )
        executed = []
        monkeypatch.setattr(
            webapp_module,
            "_do_partition_and_image",
            lambda job, disk, image: executed.append((disk, image)),
        )

        response = client.post("/partition", data={"disk": "/dev/sdz", "image": str(image_path)})
        assert response.status_code == 200
        assert b"This will erase /dev/sdz" in response.data
        assert not executed  # reviewing never runs the job

    def test_a_missing_or_wrong_token_does_not_execute(
        self,
        webapp_module: ModuleType,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        image_path = tmp_path / "capture.img"
        image_path.write_bytes(b"not a real package")

        class FakeManifest:
            type = "image"
            version = "v1.0.0"

        monkeypatch.setattr(webapp_module.image_lib, "check_anchors_present", lambda d: None)
        monkeypatch.setattr(
            webapp_module.image_lib, "verify_image", lambda p, anchors_dir: FakeManifest()
        )
        executed = []
        monkeypatch.setattr(
            webapp_module, "_do_partition_and_image", lambda job, disk, image: executed.append(1)
        )

        response = client.post(
            "/partition",
            data={"disk": "/dev/sdz", "image": str(image_path), "confirm_token": "wrong"},
        )
        assert response.status_code == 200  # shown the review page again, not redirected to a job
        assert not executed

    def test_the_matching_token_starts_a_job(
        self,
        webapp_module: ModuleType,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        image_path = tmp_path / "capture.img"
        image_path.write_bytes(b"not a real package")

        class FakeManifest:
            type = "image"
            version = "v1.0.0"

        monkeypatch.setattr(webapp_module.image_lib, "check_anchors_present", lambda d: None)
        monkeypatch.setattr(
            webapp_module.image_lib, "verify_image", lambda p, anchors_dir: FakeManifest()
        )
        executed = []
        monkeypatch.setattr(
            webapp_module,
            "_do_partition_and_image",
            lambda job, disk, image: executed.append((disk, str(image))),
        )

        token = webapp_module._confirm_token("partition", "/dev/sdz", str(image_path))
        response = client.post(
            "/partition",
            data={"disk": "/dev/sdz", "image": str(image_path), "confirm_token": token},
        )
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/jobs/")

    def test_a_refused_image_shows_the_error_and_no_review(
        self,
        webapp_module: ModuleType,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        image_path = tmp_path / "capture.img"
        image_path.write_bytes(b"garbage")

        def refuse(path: Path, anchors_dir: Path) -> None:
            raise webapp_module.image_lib.ImageError("bad signature")

        monkeypatch.setattr(webapp_module.image_lib, "check_anchors_present", lambda d: None)
        monkeypatch.setattr(webapp_module.image_lib, "verify_image", refuse)

        response = client.post("/partition", data={"disk": "/dev/sdz", "image": str(image_path)})
        assert response.status_code == 200
        assert b"bad signature" in response.data
        assert b"This will erase" not in response.data


class TestRestoreFlow:
    def test_get_shows_the_form(self, client: Any) -> None:
        response = client.get("/restore")
        assert response.status_code == 200
        assert b"Restore data from a backup archive" in response.data

    def test_an_unknown_archive_is_refused(self, client: Any) -> None:
        response = client.post("/restore", data={"source": "medium", "archive": "/nope"})
        assert response.status_code == 200
        assert b"not a known archive" in response.data

    def test_review_then_confirm(
        self,
        webapp_module: ModuleType,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "auditorium-20260920-0300.tar.zst"
        archive_path.write_bytes(b"x")
        monkeypatch.setattr(webapp_module, "find_archives", lambda: [archive_path])
        executed = []
        monkeypatch.setattr(
            webapp_module, "_do_restore", lambda job, path: executed.append(str(path))
        )

        review = client.post("/restore", data={"source": "medium", "archive": str(archive_path)})
        assert review.status_code == 200
        assert b"This will replace" in review.data
        assert not executed

        token = webapp_module._confirm_token("restore", str(archive_path))
        confirmed = client.post(
            "/restore",
            data={"source": "medium", "archive": str(archive_path), "confirm_token": token},
        )
        assert confirmed.status_code == 302
        assert confirmed.headers["Location"].startswith("/jobs/")


class TestNetworkAndDiagnostics:
    def test_network_get(self, client: Any) -> None:
        response = client.get("/network")
        assert response.status_code == 200
        assert b"Network settings" in response.data

    def test_diagnostics_get_needs_no_confirmation(self, client: Any) -> None:
        response = client.get("/diagnostics")
        assert response.status_code == 200
        assert b"Diagnostics" in response.data


class TestJobs:
    def test_an_unknown_job_is_404(self, client: Any) -> None:
        response = client.get("/jobs/does-not-exist")
        assert response.status_code == 404

    def test_a_started_job_can_be_polled(self, webapp_module: ModuleType, client: Any) -> None:
        job_id = webapp_module.start_job("A test job", lambda job: job.note("working"))
        # The background thread may not have finished; either state is fine —
        # what matters is the route answers something, not a 404.
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        assert b"A test job" in response.data


class TestConfirmToken:
    def test_the_same_inputs_produce_the_same_token(self, webapp_module: ModuleType) -> None:
        first = webapp_module._confirm_token("partition", "/dev/sdz", "img")
        second = webapp_module._confirm_token("partition", "/dev/sdz", "img")
        assert first == second

    def test_different_inputs_produce_different_tokens(self, webapp_module: ModuleType) -> None:
        a = webapp_module._confirm_token("partition", "/dev/sdz", "img")
        b = webapp_module._confirm_token("partition", "/dev/sdy", "img")
        assert a != b

    def test_check_confirm_rejects_a_tampered_token(self, webapp_module: ModuleType) -> None:
        token = webapp_module._confirm_token("restore", "archive.tar.zst")
        assert webapp_module._check_confirm(token, "restore", "archive.tar.zst")
        assert not webapp_module._check_confirm(token, "restore", "different.tar.zst")
        assert not webapp_module._check_confirm(token + "0", "restore", "archive.tar.zst")
