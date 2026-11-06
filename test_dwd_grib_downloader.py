import ftplib
from datetime import datetime
from unittest.mock import MagicMock, call

import pytest
import requests
from click.testing import CliRunner

import dwd_grib_downloader as dgd


def _filename(run_cycle: str, step: str = "000", field: str = "t_2m") -> str:
    return f"{dgd.FILE_PREFIX}_{run_cycle}_{step}_2d_{field}.{dgd.FILE_SUFFIX}"


def _index_html(*hrefs: str) -> str:
    links = "\n".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><body><pre>{links}</pre></body></html>"


class FakeResponse:
    def __init__(self, text: str = "", chunks=(), error: Exception | None = None):
        self.text = text
        self._chunks = chunks
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def iter_content(self, chunk_size=1):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _frozen_now(monkeypatch, now: datetime):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(dgd, "datetime", FrozenDatetime)


# ---------------------------------------------------------------------------
# _download_files_with_prefix
# ---------------------------------------------------------------------------

class TestDownloadFilesWithPrefix:
    URL = "https://example.com/grib/12/t_2m/"

    def test_downloads_only_matching_files(self, monkeypatch, tmp_path):
        match1 = _filename("2026092412", "000")
        match2 = _filename("2026092412", "001")
        index = _index_html(
            "../",
            match1,
            match2,
            "other-prefix_2026092412.grib2.bz2",  # wrong prefix
            f"{dgd.FILE_PREFIX}_2026092412.grib2",  # wrong suffix
        )
        responses = {
            self.URL: FakeResponse(text=index),
            self.URL + match1: FakeResponse(chunks=[b"abc", b"def"]),
            self.URL + match2: FakeResponse(chunks=[b"xyz"]),
        }
        get = MagicMock(side_effect=lambda url, **kw: responses[url])
        monkeypatch.setattr(dgd.requests, "get", get)

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        assert sorted(p.name for p in tmp_path.iterdir()) == sorted([match1, match2])
        assert (tmp_path / match1).read_bytes() == b"abcdef"
        assert (tmp_path / match2).read_bytes() == b"xyz"
        get.assert_any_call(self.URL + match1, stream=True)

    def test_resolves_absolute_and_nested_hrefs(self, monkeypatch, tmp_path):
        name = _filename("2026092412")
        href = f"/weather/nwp/icon-d2/grib/12/t_2m/{name}"
        requested = []

        def get(url, **kw):
            requested.append(url)
            if url == self.URL:
                return FakeResponse(text=_index_html(href))
            return FakeResponse(chunks=[b"data"])

        monkeypatch.setattr(dgd.requests, "get", get)

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        assert requested[1] == "https://example.com" + href
        assert (tmp_path / name).read_bytes() == b"data"

    def test_creates_output_folder(self, monkeypatch, tmp_path):
        out = tmp_path / "nested" / "out"
        monkeypatch.setattr(dgd.requests, "get", lambda url, **kw: FakeResponse(text=_index_html()))

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(out))

        assert out.is_dir()

    def test_index_error_returns_without_downloading(self, monkeypatch, tmp_path, capsys):
        get = MagicMock(return_value=FakeResponse(error=requests.exceptions.HTTPError("404")))
        monkeypatch.setattr(dgd.requests, "get", get)

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        assert get.call_count == 1
        assert list(tmp_path.iterdir()) == []
        assert "Error accessing the webpage" in capsys.readouterr().out

    def test_failed_file_download_continues_with_others(self, monkeypatch, tmp_path, capsys):
        bad = _filename("2026092412", "000")
        good = _filename("2026092412", "001")
        responses = {
            self.URL: FakeResponse(text=_index_html(bad, good)),
            self.URL + bad: FakeResponse(error=requests.exceptions.HTTPError("500")),
            self.URL + good: FakeResponse(chunks=[b"ok"]),
        }
        monkeypatch.setattr(dgd.requests, "get", lambda url, **kw: responses[url])

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        out = capsys.readouterr().out
        assert f"Failed to download {bad}" in out
        assert "Downloaded 1 file(s)" in out
        assert (tmp_path / good).read_bytes() == b"ok"
        assert not (tmp_path / f"{bad}.tmp").exists()

    def test_skips_already_downloaded_non_empty_files(self, monkeypatch, tmp_path, capsys):
        match1 = _filename("2026092412", "000")
        match2 = _filename("2026092412", "001")
        # Pre-create match1
        (tmp_path / match1).write_bytes(b"existing_content")

        requested = []
        def get(url, **kw):
            requested.append(url)
            if url == self.URL:
                return FakeResponse(text=_index_html(match1, match2))
            return FakeResponse(chunks=[b"new_content"])

        monkeypatch.setattr(dgd.requests, "get", get)

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        out = capsys.readouterr().out
        assert f"File {match1} already exists, skipping..." in out
        assert "Downloaded 2 file(s)" in out
        assert (tmp_path / match1).read_bytes() == b"existing_content"
        assert (tmp_path / match2).read_bytes() == b"new_content"
        assert (self.URL + match1) not in requested
        assert (self.URL + match2) in requested

    def test_retries_transient_failure_and_succeeds(self, monkeypatch, tmp_path, capsys):
        match = _filename("2026092412", "000")
        attempts = 0

        def get(url, **kw):
            nonlocal attempts
            if url == self.URL:
                return FakeResponse(text=_index_html(match))
            attempts += 1
            if attempts == 1:
                raise requests.exceptions.ConnectionError("Connection reset by peer")
            return FakeResponse(chunks=[b"recovered_data"])

        monkeypatch.setattr(dgd.requests, "get", get)
        monkeypatch.setattr(dgd, "FILE_RETRY_BACKOFF", 0.001)

        dgd._download_files_with_prefix(self.URL, dgd.FILE_PREFIX, dgd.FILE_SUFFIX, str(tmp_path))

        assert attempts == 2
        assert (tmp_path / match).read_bytes() == b"recovered_data"
        assert not (tmp_path / f"{match}.tmp").exists()
        out = capsys.readouterr().out
        assert "Downloaded 1 file(s)" in out


# ---------------------------------------------------------------------------
# HTTP Session and Retries
# ---------------------------------------------------------------------------

class TestHttpSession:
    def test_get_session_configuration(self):
        session = dgd.get_session(reset=True)
        assert session.headers["User-Agent"] == dgd.USER_AGENT
        for prefix in ("https://", "http://"):
            adapter = session.adapters.get(prefix)
            assert adapter is not None
            assert adapter.max_retries.total == 5
            assert adapter.max_retries.connect == 5
            assert adapter.max_retries.read == 5
            assert 429 in adapter.max_retries.status_forcelist
            assert 503 in adapter.max_retries.status_forcelist


# ---------------------------------------------------------------------------
# run_cycle_is_up_to_date
# ---------------------------------------------------------------------------

class TestRunCycleIsUpToDate:
    RUN = datetime(2026, 9, 24, 12)

    def test_all_fields_current_returns_true(self, monkeypatch):
        requested = []

        def get(url, **kw):
            requested.append(url)
            return FakeResponse(text=_index_html("../", _filename("2026092412", "000"),
                                                 _filename("2026092412", "001")))

        monkeypatch.setattr(dgd.requests, "get", get)

        assert dgd.run_cycle_is_up_to_date(self.RUN) is True
        assert requested == [f"{dgd.BASE_URL}12/{sf}/" for sf in dgd.SINGLE_FIELDS]

    def test_hour_is_zero_padded_in_url(self, monkeypatch):
        requested = []

        def get(url, **kw):
            requested.append(url)
            return FakeResponse(text=_index_html(_filename("2026092403")))

        monkeypatch.setattr(dgd.requests, "get", get)

        assert dgd.run_cycle_is_up_to_date(datetime(2026, 9, 24, 3)) is True
        assert requested[0] == f"{dgd.BASE_URL}03/{dgd.SINGLE_FIELDS[0]}/"

    def test_stale_file_returns_false(self, monkeypatch):
        index = _index_html(_filename("2026092412", "000"), _filename("2026092312", "001"))
        monkeypatch.setattr(dgd.requests, "get", lambda url, **kw: FakeResponse(text=index))

        assert dgd.run_cycle_is_up_to_date(self.RUN) is False

    def test_stale_in_later_field_returns_false(self, monkeypatch):
        fresh = FakeResponse(text=_index_html(_filename("2026092412")))
        stale = FakeResponse(text=_index_html(_filename("2026092312")))
        get = MagicMock(side_effect=[fresh, stale])
        monkeypatch.setattr(dgd.requests, "get", get)

        assert dgd.run_cycle_is_up_to_date(self.RUN) is False
        assert get.call_count == 2

    def test_non_matching_files_are_ignored(self, monkeypatch):
        index = _index_html(_filename("2026092412"), "unrelated_2020010100.grib2.bz2", "../")
        monkeypatch.setattr(dgd.requests, "get", lambda url, **kw: FakeResponse(text=index))

        assert dgd.run_cycle_is_up_to_date(self.RUN) is True

    def test_request_error_returns_false(self, monkeypatch):
        def get(url, **kw):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(dgd.requests, "get", get)

        assert dgd.run_cycle_is_up_to_date(self.RUN) is False

    def test_empty_directory_returns_false(self, monkeypatch):
        index = _index_html("../", "unrelated_file.txt")
        monkeypatch.setattr(dgd.requests, "get", lambda url, **kw: FakeResponse(text=index))

        assert dgd.run_cycle_is_up_to_date(self.RUN) is False


# ---------------------------------------------------------------------------
# _findlatest
# ---------------------------------------------------------------------------

class TestFindLatest:
    def test_returns_current_cycle_when_ready(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 14, 30))
        check = MagicMock(return_value=True)
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == datetime(2026, 9, 24, 12)
        check.assert_called_once_with(datetime(2026, 9, 24, 12))

    def test_steps_back_until_ready(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 14, 30))
        check = MagicMock(side_effect=[False, False, True])
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == datetime(2026, 9, 24, 6)
        assert check.call_args_list == [
            call(datetime(2026, 9, 24, 12)),
            call(datetime(2026, 9, 24, 9)),
            call(datetime(2026, 9, 24, 6)),
        ]

    def test_raises_when_no_cycle_ready_within_max_steps(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 12, 0))
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", lambda d: False)

        with pytest.raises(RuntimeError, match="No complete run cycle found"):
            dgd._findlatest(max_steps=3)

    @pytest.mark.parametrize("hour,expected", [(0, 0), (2, 0), (3, 3), (8, 6), (21, 21), (23, 21)])
    def test_rounds_down_to_run_cycle(self, monkeypatch, hour, expected):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, hour, 59))
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", lambda d: True)

        assert dgd._findlatest() == datetime(2026, 9, 24, expected)
        assert expected in dgd.RUN_CYCLES

    def test_wraps_to_previous_day(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 1, 0))
        check = MagicMock(side_effect=[False, True])
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == datetime(2026, 9, 23, 21)
        assert check.call_args_list[1] == call(datetime(2026, 9, 23, 21))


# ---------------------------------------------------------------------------
# _download / _postprocess
# ---------------------------------------------------------------------------

def test_get_grib_filename():
    assert dgd.get_grib_filename(datetime(2026, 9, 24, 12)) == "2026092412.grib2"
    assert dgd.get_grib_filename(datetime(2026, 1, 5, 3)) == "2026010503.grib2"


def test_to_naive_utc():
    from datetime import timezone
    naive = datetime(2026, 9, 24, 12)
    assert dgd._to_naive_utc(naive) == naive

    aware = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    assert dgd._to_naive_utc(aware) == naive


def test_parse_grib_filename():
    assert dgd.parse_grib_filename("2026110118.grib2") == datetime(2026, 11, 1, 18)
    assert dgd.parse_grib_filename("path/to/2026092403.grib2") == datetime(2026, 9, 24, 3)
    assert dgd.parse_grib_filename("combined2.grib2") is None
    assert dgd.parse_grib_filename("other.txt") is None
    assert dgd.parse_grib_filename("2026110118.grib2.tmp") is None
    assert dgd.parse_grib_filename("2026130118.grib2") is None  # invalid month 13


def test_find_latest_grib_file():
    files = ["2026110106.grib2", "combined2.grib2", "2026110118.grib2", "2026110112.grib2"]
    assert dgd.find_latest_grib_file(files) == (datetime(2026, 11, 1, 18), "2026110118.grib2")
    assert dgd.find_latest_grib_file(["invalid.txt", "combined2.grib2"]) is None
    assert dgd.find_latest_grib_file([]) is None


def test_find_latest_local_grib(tmp_path):
    (tmp_path / "2026110106.grib2").touch()
    (tmp_path / "2026110118.grib2").touch()
    (tmp_path / "2026110112.grib2").touch()
    (tmp_path / "combined2.grib2").touch()

    assert dgd.find_latest_local_grib(str(tmp_path)) == (datetime(2026, 11, 1, 18), "2026110118.grib2")
    assert dgd.find_latest_local_grib(str(tmp_path / "nonexistent")) is None


def test_download_fetches_every_single_field(monkeypatch):
    fetch = MagicMock()
    monkeypatch.setattr(dgd, "_download_files_with_prefix", fetch)

    dgd._download(6)

    assert fetch.call_args_list == [
        call(f"{dgd.BASE_URL}06/{sf}/", dgd.FILE_PREFIX, dgd.FILE_SUFFIX, dgd.OUTPUT_FOLDER)
        for sf in dgd.SINGLE_FIELDS
    ]


def test_postprocess_runs_pipeline_in_order(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(dgd, "run_command", run)

    dgd._postprocess("2026092412.grib2")

    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds == [
        "bunzip2 *.bz2",
        "cdo merge *.grib2 combined.grib2",
        "cdo select,minute=0 combined.grib2 final.grib2",
        "cdo sellonlatbox,10,14,54,57 final.grib2 ../2026092412.grib2",
        "rm *.grib2",
    ]
    assert all(c.kwargs["cwd"] == dgd.OUTPUT_FOLDER for c in run.call_args_list)


# ---------------------------------------------------------------------------
# _upload
# ---------------------------------------------------------------------------

class TestUpload:
    def test_upload_successful(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.write_bytes(b"GRIB_DATA_123")

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        uploaded_data = []
        mock_ftp.storbinary.side_effect = lambda cmd, fp: uploaded_data.append(fp.read())
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(
            str(grib_file),
            host="helmer.se",
            user="testuser",
            password="testpassword",
            remote_dir="grib",
            port=21,
        )

        mock_ftp.connect.assert_called_once_with("helmer.se", 21)
        mock_ftp.login.assert_called_once_with(user="testuser", passwd="testpassword")
        mock_ftp.cwd.assert_called_once_with("grib")
        assert mock_ftp.storbinary.call_count == 1
        call_args = mock_ftp.storbinary.call_args
        assert call_args[0][0] == "STOR combined2.grib2"
        assert uploaded_data == [b"GRIB_DATA_123"]

    def test_upload_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="GRIB file not found"):
            dgd._upload(
                "nonexistent_file.grib2",
                host="helmer.se",
                user="testuser",
                password="secret",
            )

    def test_upload_missing_user_raises_runtime_error(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.touch()

        monkeypatch.setattr(dgd.netrc, "netrc", MagicMock(side_effect=FileNotFoundError))

        with pytest.raises(RuntimeError, match="FTP username is required"):
            dgd._upload(str(grib_file), host="helmer.se", user=None)

    def test_upload_reads_credentials_from_netrc(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.touch()

        mock_netrc_instance = MagicMock()
        mock_netrc_instance.authenticators.return_value = ("netrc_user", "account", "netrc_pass")
        monkeypatch.setattr(dgd.netrc, "netrc", MagicMock(return_value=mock_netrc_instance))

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(str(grib_file), host="helmer.se", user=None, password=None)

        mock_ftp.login.assert_called_once_with(user="netrc_user", passwd="netrc_pass")

    def test_upload_splits_host_with_path(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(str(grib_file), host="helmer.se/custom_dir", user="user", password="pwd")

        mock_ftp.connect.assert_called_once_with("helmer.se", 21)
        mock_ftp.cwd.assert_called_once_with("custom_dir")

    def test_upload_creates_remote_dir_if_not_exists(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.cwd.side_effect = [ftplib.error_perm("550 Directory not found"), None]
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(str(grib_file), host="helmer.se", user="user", password="pwd", remote_dir="grib")

        mock_ftp.mkd.assert_called_once_with("grib")
        assert mock_ftp.cwd.call_count == 2

    def test_upload_empty_remote_dir(self, monkeypatch, tmp_path):
        grib_file = tmp_path / "combined2.grib2"
        grib_file.touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(str(grib_file), host="helmer.se", user="user", password="pwd", remote_dir="")

        mock_ftp.cwd.assert_not_called()

    def test_upload_default_finds_latest_local(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "2026110112.grib2").write_bytes(b"OLD_DATA")
        (tmp_path / "2026110118.grib2").write_bytes(b"LATEST_DATA")

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        uploaded_data = []
        mock_ftp.storbinary.side_effect = lambda cmd, fp: uploaded_data.append(fp.read())
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._upload(
            host="helmer.se",
            user="testuser",
            password="testpassword",
            remote_dir="grib",
            port=21,
        )

        assert mock_ftp.storbinary.call_args[0][0] == "STOR 2026110118.grib2"
        assert uploaded_data == [b"LATEST_DATA"]

    def test_upload_default_no_local_files_raises(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="No local timestamped GRIB file found to upload"):
            dgd._upload(host="helmer.se", user="testuser", password="pwd")


# ---------------------------------------------------------------------------
# _download_ftp
# ---------------------------------------------------------------------------

class TestDownloadFtp:
    def test_download_ftp_successful(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"FTP_GRIB_CONTENT")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(dest_file),
            host="helmer.se",
            user="testuser",
            password="testpassword",
            remote_dir="grib",
            port=21,
        )

        mock_ftp.connect.assert_called_once_with("helmer.se", 21)
        mock_ftp.login.assert_called_once_with(user="testuser", passwd="testpassword")
        mock_ftp.cwd.assert_called_once_with("grib")
        assert mock_ftp.retrbinary.call_count == 1
        assert mock_ftp.retrbinary.call_args[0][0] == "RETR combined2.grib2"
        assert dest_file.read_bytes() == b"FTP_GRIB_CONTENT"
        assert not (tmp_path / "combined2.grib2.tmp").exists()

    def test_download_ftp_missing_user_raises_runtime_error(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        monkeypatch.setattr(dgd.netrc, "netrc", MagicMock(side_effect=FileNotFoundError))

        with pytest.raises(RuntimeError, match="FTP username is required"):
            dgd._download_ftp(str(dest_file), host="helmer.se", user=None)

    def test_download_ftp_reads_credentials_from_netrc(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        mock_netrc_instance = MagicMock()
        mock_netrc_instance.authenticators.return_value = ("netrc_user", "account", "netrc_pass")
        monkeypatch.setattr(dgd.netrc, "netrc", MagicMock(return_value=mock_netrc_instance))

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"NETRC_DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(str(dest_file), host="helmer.se", user=None, password=None)

        mock_ftp.login.assert_called_once_with(user="netrc_user", passwd="netrc_pass")
        assert dest_file.read_bytes() == b"NETRC_DATA"

    def test_download_ftp_splits_host_with_path(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(str(dest_file), host="helmer.se/custom_dir", user="user", password="pwd")

        mock_ftp.connect.assert_called_once_with("helmer.se", 21)
        mock_ftp.cwd.assert_called_once_with("custom_dir")

    def test_download_ftp_empty_remote_dir(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(str(dest_file), host="helmer.se", user="user", password="pwd", remote_dir="")

        mock_ftp.cwd.assert_not_called()

    def test_download_ftp_custom_remote_file(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "local.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"CUSTOM_REMOTE")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(dest_file),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_file="remote_archive.grib2",
        )

        assert mock_ftp.retrbinary.call_args[0][0] == "RETR remote_archive.grib2"
        assert dest_file.read_bytes() == b"CUSTOM_REMOTE"

    def test_download_ftp_creates_destination_directory(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "sub" / "folder" / "combined2.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"NESTED")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(str(dest_file), host="helmer.se", user="user", password="pwd")

        assert dest_file.read_bytes() == b"NESTED"

    def test_download_ftp_cleans_up_tmp_on_failure(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp

        def fail_retr(cmd, cb):
            cb(b"partial_corrupt")
            raise ftplib.error_perm("550 File not found")

        mock_ftp.retrbinary.side_effect = fail_retr
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        with pytest.raises(ftplib.error_perm, match="550 File not found"):
            dgd._download_ftp(str(dest_file), host="helmer.se", user="user", password="pwd")

        assert not dest_file.exists()
        assert not (tmp_path / "combined2.grib2.tmp").exists()

    def test_download_ftp_preserves_existing_file_on_failure(self, monkeypatch, tmp_path):
        dest_file = tmp_path / "combined2.grib2"
        dest_file.write_bytes(b"EXISTING_VALID_DATA")

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = ftplib.error_perm("550 Transfer failed")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        with pytest.raises(ftplib.error_perm, match="550 Transfer failed"):
            dgd._download_ftp(str(dest_file), host="helmer.se", user="user", password="pwd")

        assert dest_file.read_bytes() == b"EXISTING_VALID_DATA"
        assert not (tmp_path / "combined2.grib2.tmp").exists()

    def test_download_ftp_update_newer_remote_downloads(self, monkeypatch, tmp_path):
        (tmp_path / "2026110112.grib2").touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["2026110112.grib2", "2026110118.grib2", "notes.txt"]
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"NEW_GRIB_DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_dir="grib",
            update=True,
        )

        mock_ftp.nlst.assert_called_once()
        assert mock_ftp.retrbinary.call_args[0][0] == "RETR 2026110118.grib2"
        assert (tmp_path / "2026110118.grib2").read_bytes() == b"NEW_GRIB_DATA"

    def test_download_ftp_update_already_up_to_date_skips(self, monkeypatch, tmp_path):
        (tmp_path / "2026110118.grib2").touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["2026110112.grib2", "2026110118.grib2"]
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_dir="grib",
            update=True,
        )

        mock_ftp.nlst.assert_called_once()
        mock_ftp.retrbinary.assert_not_called()

    def test_download_ftp_update_no_local_files_downloads(self, monkeypatch, tmp_path):
        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["2026110118.grib2"]
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_dir="grib",
            update=True,
        )

        assert mock_ftp.retrbinary.call_args[0][0] == "RETR 2026110118.grib2"
        assert (tmp_path / "2026110118.grib2").read_bytes() == b"DATA"

    def test_download_ftp_update_no_timestamped_files_skips(self, monkeypatch, tmp_path):
        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["readme.txt", "combined.grib"]
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            update=True,
        )

        mock_ftp.retrbinary.assert_not_called()

    def test_download_ftp_update_with_explicit_remote_file_already_current(self, monkeypatch, tmp_path):
        (tmp_path / "2026110118.grib2").touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_file="2026110118.grib2",
            update=True,
        )

        mock_ftp.retrbinary.assert_not_called()

    def test_download_ftp_update_nlst_with_paths(self, monkeypatch, tmp_path):
        (tmp_path / "2026110112.grib2").touch()

        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["grib/2026110112.grib2", "grib/2026110118.grib2"]
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"PATH_GRIB_DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            str(tmp_path),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_dir="grib",
            update=True,
        )

        assert mock_ftp.retrbinary.call_args[0][0] == "RETR 2026110118.grib2"
        assert (tmp_path / "2026110118.grib2").read_bytes() == b"PATH_GRIB_DATA"

    def test_download_ftp_default_filepath_downloads_latest(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.nlst.return_value = ["2026110112.grib2", "2026110118.grib2"]
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"LATEST_GRIB_DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dgd._download_ftp(
            host="helmer.se",
            user="user",
            password="pwd",
        )

        assert mock_ftp.retrbinary.call_args[0][0] == "RETR 2026110118.grib2"
        assert (tmp_path / "2026110118.grib2").read_bytes() == b"LATEST_GRIB_DATA"

    def test_download_ftp_update_non_timestamped_remote_file(self, monkeypatch, tmp_path):
        mock_ftp = MagicMock()
        mock_ftp.__enter__.return_value = mock_ftp
        mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"CUSTOM_DATA")
        monkeypatch.setattr(dgd.ftplib, "FTP", MagicMock(return_value=mock_ftp))

        dest_file = tmp_path / "custom.grib2"
        dgd._download_ftp(
            str(dest_file),
            host="helmer.se",
            user="user",
            password="pwd",
            remote_file="custom.grib2",
            update=True,
        )

        assert mock_ftp.retrbinary.call_args[0][0] == "RETR custom.grib2"
        assert dest_file.read_bytes() == b"CUSTOM_DATA"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_findlatest_command(self, monkeypatch):
        find = MagicMock(return_value=9)
        monkeypatch.setattr(dgd, "_findlatest", find)

        result = CliRunner().invoke(dgd.cli, ["findlatest"])

        assert result.exit_code == 0
        assert "Searching for the most recent valid run cycle" in result.output
        find.assert_called_once_with()

    def test_download_command_passes_hour(self, monkeypatch):
        download = MagicMock()
        monkeypatch.setattr(dgd, "_download", download)

        result = CliRunner().invoke(dgd.cli, ["download", "--run_cycle_hour", "15"])

        assert result.exit_code == 0
        download.assert_called_once_with(15)

    def test_download_command_rejects_non_int(self, monkeypatch):
        monkeypatch.setattr(dgd, "_download", MagicMock())

        result = CliRunner().invoke(dgd.cli, ["download", "--run_cycle_hour", "abc"])

        assert result.exit_code != 0
        dgd._download.assert_not_called()

    def test_makefinalgrib_chains_steps(self, monkeypatch):
        order = MagicMock()
        order._findlatest.return_value = datetime(2026, 9, 24, 18)
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib"])

        assert result.exit_code == 0
        assert order.mock_calls == [
            call._findlatest(),
            call._download(18),
            call._postprocess("2026092418.grib2"),
            call._upload(
                "2026092418.grib2",
                host="helmer.se",
                user=None,
                password=None,
                remote_dir="grib",
                port=21,
            ),
        ]

    def test_makefinalgrib_skip_upload(self, monkeypatch):
        order = MagicMock()
        order._findlatest.return_value = datetime(2026, 9, 24, 18)
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib", "--skip-upload"])

        assert result.exit_code == 0
        assert order.mock_calls == [
            call._findlatest(),
            call._download(18),
            call._postprocess("2026092418.grib2"),
        ]

    def test_makefinalgrib_passes_ftp_options(self, monkeypatch):
        upload = MagicMock()
        monkeypatch.setattr(dgd, "_findlatest", MagicMock(return_value=datetime(2026, 9, 24, 12)))
        monkeypatch.setattr(dgd, "_download", MagicMock())
        monkeypatch.setattr(dgd, "_postprocess", MagicMock())
        monkeypatch.setattr(dgd, "_upload", upload)

        result = CliRunner().invoke(dgd.cli, [
            "makefinalgrib",
            "--ftp-host", "other.host",
            "--ftp-user", "myuser",
            "--ftp-password", "mypass",
            "--ftp-dir", "custom/dir",
            "--ftp-port", "2121",
        ])

        assert result.exit_code == 0
        upload.assert_called_once_with(
            "2026092412.grib2",
            host="other.host",
            user="myuser",
            password="mypass",
            remote_dir="custom/dir",
            port=2121,
        )

    def test_makefinalgrib_update_when_newer(self, monkeypatch):
        order = MagicMock()
        order._findlatest.return_value = datetime(2026, 9, 24, 18)
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)
        monkeypatch.setattr(
            dgd,
            "find_latest_local_grib",
            MagicMock(return_value=(datetime(2026, 9, 24, 12), "2026092412.grib2")),
        )

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib", "--update"])

        assert result.exit_code == 0
        assert order._download.call_count == 1
        assert order._postprocess.call_count == 1
        assert order._upload.call_count == 1

    def test_makefinalgrib_update_when_already_up_to_date(self, monkeypatch):
        order = MagicMock()
        order._findlatest.return_value = datetime(2026, 9, 24, 18)
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)
        monkeypatch.setattr(
            dgd,
            "find_latest_local_grib",
            MagicMock(return_value=(datetime(2026, 9, 24, 18), "2026092418.grib2")),
        )

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib", "--update"])

        assert result.exit_code == 0
        assert "Already up to date" in result.output
        order._download.assert_not_called()
        order._postprocess.assert_not_called()
        order._upload.assert_not_called()

    def test_makefinalgrib_update_no_local_files(self, monkeypatch):
        order = MagicMock()
        order._findlatest.return_value = datetime(2026, 9, 24, 18)
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)
        monkeypatch.setattr(dgd, "find_latest_local_grib", MagicMock(return_value=None))

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib", "--update"])

        assert result.exit_code == 0
        assert order._download.call_count == 1
        assert order._postprocess.call_count == 1
        assert order._upload.call_count == 1

    def test_makefinalgrib_update_with_int_cycle_up_to_date(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 20, 0))
        order = MagicMock()
        order._findlatest.return_value = 18
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)
        monkeypatch.setattr(dgd, "_upload", order._upload)
        monkeypatch.setattr(
            dgd,
            "find_latest_local_grib",
            MagicMock(return_value=(datetime(2026, 9, 24, 18), "2026092418.grib2")),
        )

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib", "--update"])

        assert result.exit_code == 0
        assert "Already up to date" in result.output
        order._download.assert_not_called()
        order._postprocess.assert_not_called()
        order._upload.assert_not_called()

    def test_upload_command_defaults(self, monkeypatch):
        upload = MagicMock()
        monkeypatch.setattr(dgd, "_upload", upload)

        result = CliRunner().invoke(dgd.cli, ["upload"])

        assert result.exit_code == 0
        upload.assert_called_once_with(
            None,
            host="helmer.se",
            user=None,
            password=None,
            remote_dir="grib",
            port=21,
        )

    def test_upload_command_custom_options(self, monkeypatch):
        upload = MagicMock()
        monkeypatch.setattr(dgd, "_upload", upload)

        result = CliRunner().invoke(dgd.cli, [
            "upload",
            "--file", "custom.grib2",
            "--ftp-host", "ftp.example.com",
            "--ftp-user", "user1",
            "--ftp-password", "pass1",
            "--ftp-dir", "weather",
            "--ftp-port", "2121",
        ])

        assert result.exit_code == 0
        upload.assert_called_once_with(
            "custom.grib2",
            host="ftp.example.com",
            user="user1",
            password="pass1",
            remote_dir="weather",
            port=2121,
        )

    def test_upload_command_env_vars(self, monkeypatch):
        upload = MagicMock()
        monkeypatch.setattr(dgd, "_upload", upload)

        result = CliRunner().invoke(
            dgd.cli,
            ["upload"],
            env={
                "FTP_HOST": "env.helmer.se",
                "FTP_USER": "envuser",
                "FTP_PASSWORD": "envpassword",
                "FTP_DIR": "envgrib",
                "FTP_PORT": "2122",
            },
        )

        assert result.exit_code == 0
        upload.assert_called_once_with(
            None,
            host="env.helmer.se",
            user="envuser",
            password="envpassword",
            remote_dir="envgrib",
            port=2122,
        )

    def test_downloadftp_command_defaults(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(dgd.cli, ["downloadftp"])

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath=None,
            host="helmer.se",
            user=None,
            password=None,
            remote_dir="grib",
            port=21,
            remote_file=None,
            update=False,
        )

    def test_downloadftp_command_update_flag(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(dgd.cli, ["downloadftp", "--update"])

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath=None,
            host="helmer.se",
            user=None,
            password=None,
            remote_dir="grib",
            port=21,
            remote_file=None,
            update=True,
        )

    def test_downloadftp_command_custom_options(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(dgd.cli, [
            "downloadftp",
            "--file", "custom.grib2",
            "--remote-file", "remote_archive.grib2",
            "--ftp-host", "ftp.example.com",
            "--ftp-user", "user1",
            "--ftp-password", "pass1",
            "--ftp-dir", "weather",
            "--ftp-port", "2121",
        ])

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath="custom.grib2",
            host="ftp.example.com",
            user="user1",
            password="pass1",
            remote_dir="weather",
            port=2121,
            remote_file="remote_archive.grib2",
            update=False,
        )

    def test_downloadftp_command_env_vars(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(
            dgd.cli,
            ["downloadftp"],
            env={
                "FTP_HOST": "env.helmer.se",
                "FTP_USER": "envuser",
                "FTP_PASSWORD": "envpassword",
                "FTP_DIR": "envgrib",
                "FTP_PORT": "2122",
            },
        )

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath=None,
            host="env.helmer.se",
            user="envuser",
            password="envpassword",
            remote_dir="envgrib",
            port=2122,
            remote_file=None,
            update=False,
        )

    def test_download_ftp_alias_command(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(dgd.cli, ["download-ftp"])

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath=None,
            host="helmer.se",
            user=None,
            password=None,
            remote_dir="grib",
            port=21,
            remote_file=None,
            update=False,
        )

    def test_download_ftp_underscore_alias_command(self, monkeypatch):
        download_ftp = MagicMock()
        monkeypatch.setattr(dgd, "_download_ftp", download_ftp)

        result = CliRunner().invoke(dgd.cli, ["download_ftp"])

        assert result.exit_code == 0
        download_ftp.assert_called_once_with(
            filepath=None,
            host="helmer.se",
            user=None,
            password=None,
            remote_dir="grib",
            port=21,
            remote_file=None,
            update=False,
        )
