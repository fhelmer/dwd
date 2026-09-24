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


# ---------------------------------------------------------------------------
# _findlatest
# ---------------------------------------------------------------------------

class TestFindLatest:
    def test_returns_current_cycle_when_ready(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 14, 30))
        check = MagicMock(return_value=True)
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == 12
        check.assert_called_once_with(datetime(2026, 9, 24, 12))

    def test_steps_back_until_ready(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 14, 30))
        check = MagicMock(side_effect=[False, False, True])
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == 6
        assert check.call_args_list == [
            call(datetime(2026, 9, 24, 12)),
            call(datetime(2026, 9, 24, 9)),
            call(datetime(2026, 9, 24, 6)),
        ]

    @pytest.mark.parametrize("hour,expected", [(0, 0), (2, 0), (3, 3), (8, 6), (21, 21), (23, 21)])
    def test_rounds_down_to_run_cycle(self, monkeypatch, hour, expected):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, hour, 59))
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", lambda d: True)

        assert dgd._findlatest() == expected
        assert expected in dgd.RUN_CYCLES

    def test_wraps_to_previous_day(self, monkeypatch):
        _frozen_now(monkeypatch, datetime(2026, 9, 24, 1, 0))
        check = MagicMock(side_effect=[False, True])
        monkeypatch.setattr(dgd, "run_cycle_is_up_to_date", check)

        assert dgd._findlatest() == 21
        assert check.call_args_list[1] == call(datetime(2026, 9, 23, 21))


# ---------------------------------------------------------------------------
# _download / _postprocess
# ---------------------------------------------------------------------------

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

    dgd._postprocess()

    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds == [
        "bunzip2 *.bz2",
        "cdo merge *.grib2 combined.grib2",
        "cdo sellonlatbox,10,14,54,57 combined.grib2 ../combined2.grib2",
        "rm *.grib2",
    ]
    assert all(c.kwargs["cwd"] == dgd.OUTPUT_FOLDER for c in run.call_args_list)


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
        order._findlatest.return_value = 18
        monkeypatch.setattr(dgd, "_findlatest", order._findlatest)
        monkeypatch.setattr(dgd, "_download", order._download)
        monkeypatch.setattr(dgd, "_postprocess", order._postprocess)

        result = CliRunner().invoke(dgd.cli, ["makefinalgrib"])

        assert result.exit_code == 0
        assert order.mock_calls == [call._findlatest(), call._download(18), call._postprocess()]
