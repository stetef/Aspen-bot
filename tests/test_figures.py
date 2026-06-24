"""Characterization tests for figure upload + archival."""

from unittest.mock import MagicMock

# Minimal valid PNG header bytes (enough to look like a .png on disk).
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_uploads_png_and_archives_it(sut, tmp_path):
    fig = tmp_path / "plot1.png"
    fig.write_bytes(_PNG_BYTES)
    client = MagicMock()

    sut._upload_figures([str(fig)], client, channel="C123", thread_ts="1.0")

    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs["channel"] == "C123"
    assert kwargs["thread_ts"] == "1.0"
    assert kwargs["title"] == "plot1"
    # The file is moved out of its source into the archive after a successful upload.
    assert not fig.exists()
    assert (sut.FIGURE_ARCHIVE_DIR / "plot1.png").exists()


def test_skips_non_png_files(sut, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("not an image")
    client = MagicMock()

    sut._upload_figures([str(txt)], client, channel="C123", thread_ts="1.0")

    client.files_upload_v2.assert_not_called()
    assert txt.exists()  # untouched


def test_skips_missing_files(sut, tmp_path):
    client = MagicMock()
    sut._upload_figures([str(tmp_path / "ghost.png")], client, channel="C123", thread_ts="1.0")
    client.files_upload_v2.assert_not_called()


def test_upload_failure_is_swallowed(sut, tmp_path):
    fig = tmp_path / "plot.png"
    fig.write_bytes(_PNG_BYTES)
    client = MagicMock()
    client.files_upload_v2.side_effect = RuntimeError("slack down")

    # Must not raise; the file is left in place because the move follows a successful upload.
    sut._upload_figures([str(fig)], client, channel="C123", thread_ts="1.0")
    assert fig.exists()
