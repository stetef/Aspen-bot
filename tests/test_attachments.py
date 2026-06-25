"""Characterization tests for the generic attachment upload channel.

Any file type is uploaded. Files generated under the workspace are archived
(moved) after a successful upload; files attached from elsewhere are uploaded
in place and never moved.
"""

from unittest.mock import MagicMock

# Minimal valid PNG header bytes (enough to look like a .png on disk).
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_uploads_workspace_figure_and_archives_it(sut, tmp_path):
    # A generated figure lives under the workspace -> archived after upload.
    fig = sut.WORKSPACE_ROOT / "plot1.png"
    fig.write_bytes(_PNG_BYTES)
    client = MagicMock()

    sut._upload_attachments([str(fig)], client, channel="C123", thread_ts="1.0")

    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs["channel"] == "C123"
    assert kwargs["thread_ts"] == "1.0"
    assert kwargs["filename"] == "plot1.png"
    assert kwargs["title"] == "plot1"
    # Moved out of the workspace into the archive after a successful upload.
    assert not fig.exists()
    assert (sut.FIGURE_ARCHIVE_DIR / "plot1.png").exists()


def test_uploads_arbitrary_file_type(sut, tmp_path):
    # Non-image attachments upload too (this is the generalization).
    data = sut.CALCULATIONS_ROOT / "results.csv"
    data.write_text("a,b\n1,2\n")
    client = MagicMock()

    sut._upload_attachments([str(data)], client, channel="C123", thread_ts="1.0")

    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs["filename"] == "results.csv"


def test_attached_source_file_is_uploaded_but_not_moved(sut, tmp_path):
    # A file from the (read-only) calculations root must be left in place.
    src = sut.CALCULATIONS_ROOT / "structure.cif"
    src.write_text("cif data")
    client = MagicMock()

    sut._upload_attachments([str(src)], client, channel="C123", thread_ts="1.0")

    client.files_upload_v2.assert_called_once()
    assert src.exists()  # not archived/moved


def test_skips_missing_files(sut, tmp_path):
    client = MagicMock()
    sut._upload_attachments([str(tmp_path / "ghost.png")], client, channel="C123", thread_ts="1.0")
    client.files_upload_v2.assert_not_called()


def test_upload_failure_is_swallowed_and_file_kept(sut, tmp_path):
    fig = sut.WORKSPACE_ROOT / "plot.png"
    fig.write_bytes(_PNG_BYTES)
    client = MagicMock()
    client.files_upload_v2.side_effect = RuntimeError("slack down")

    # Must not raise; the file stays put because archival follows a successful upload.
    sut._upload_attachments([str(fig)], client, channel="C123", thread_ts="1.0")
    assert fig.exists()
