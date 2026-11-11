import click
from datetime import datetime, timedelta, timezone
from util import run_command
import ftplib
import netrc
import os
import re
import shlex
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

RUN_CYCLES = [0, 3, 6, 9, 12, 15, 18, 21]
BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-d2/grib/"
SINGLE_FIELDS = ["u_10m", "v_10m", "vmax_10m", "tot_prec", "t_2m", "cape_ml", "h_snow", "pmsl", "clct"]
FILE_PREFIX = "icon-d2_germany_regular-lat-lon_single-level"
FILE_SUFFIX = "grib2.bz2"
OUTPUT_FOLDER = "./downloaded_files"
DEFAULT_FTP_HOST = "helmer.se"
DEFAULT_FTP_DIR = "grib"
DEFAULT_FTP_PORT = 21
DEFAULT_KEEP_FILES = 10

DEFAULT_TIMEOUT = (10, 60)
USER_AGENT = "dwd-grib-downloader/1.0 (https://opendata.dwd.de; weather data ingest)"
MAX_FILE_RETRIES = 3
FILE_RETRY_BACKOFF = 0.2

_ORIGINAL_REQUESTS_GET = requests.get
_session: requests.Session | None = None


def get_session(reset: bool = False) -> requests.Session:
    """Return a shared requests.Session configured with connection pooling, retries, and User-Agent."""
    global _session
    if _session is None or reset:
        if _session is not None:
            _session.close()
        _session = requests.Session()
        retries = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def _http_get(url: str, session: requests.Session | None = None, **kwargs):
    """Perform HTTP GET using persistent session, or fall back to requests.get if monkeypatched."""
    if requests.get != _ORIGINAL_REQUESTS_GET:
        return requests.get(url, **kwargs)
    sess = session or get_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return sess.get(url, **kwargs)


def get_grib_filename(run_cycle: datetime) -> str:
    return f"{run_cycle.strftime('%Y%m%d%H')}.grib2"


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_grib_filename(filename: str) -> datetime | None:
    """Extract run cycle datetime from a GRIB filename like '2026110118.grib2'."""
    basename = os.path.basename(filename)
    match = re.match(r"^(\d{10})\.grib2$", basename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H")
    except ValueError:
        return None


def find_latest_grib_file(filenames: list[str]) -> tuple[datetime, str] | None:
    """Find the most recent GRIB file with a valid run cycle timestamp from a list of filenames."""
    latest_dt = None
    latest_fn = None
    for fn in filenames:
        dt = parse_grib_filename(fn)
        if dt is not None:
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_fn = fn
    if latest_dt is not None and latest_fn is not None:
        return latest_dt, latest_fn
    return None


def find_local_grib_files(directory: str = ".") -> list[tuple[datetime, str]]:
    """Find all timestamped GRIB files in directory, sorted chronologically (oldest first)."""
    if not os.path.isdir(directory):
        return []
    try:
        filenames = os.listdir(directory)
    except OSError:
        return []
    files: list[tuple[datetime, str]] = []
    for fn in filenames:
        dt = parse_grib_filename(fn)
        if dt is not None:
            files.append((dt, os.path.join(directory, fn)))
    files.sort(key=lambda item: (item[0], item[1]))
    return files


def cleanup_local_grib_files(directory: str = ".", keep: int = DEFAULT_KEEP_FILES) -> list[str]:
    """Keep only the latest `keep` timestamped GRIB files in `directory`. Older files are deleted."""
    if keep <= 0:
        return []
    files = find_local_grib_files(directory)
    if len(files) <= keep:
        return []

    to_delete = files[:-keep]
    deleted = []
    for _, filepath in to_delete:
        try:
            os.remove(filepath)
            deleted.append(filepath)
            click.echo(f"Removed old GRIB file: {os.path.basename(filepath)}")
        except OSError as e:
            click.echo(f"Failed to remove {filepath}: {e}")
    return deleted


def find_latest_local_grib(directory: str = ".") -> tuple[datetime, str] | None:
    """Find the most recent timestamped GRIB file in the given local directory."""
    if not os.path.isdir(directory):
        return None
    try:
        filenames = os.listdir(directory)
    except OSError:
        return None
    return find_latest_grib_file(filenames)


def _download_files_with_prefix(url, prefix, suffix, output_folder, session=None):
    # Ensure the output directory exists
    os.makedirs(output_folder, exist_ok=True)

    print(f"Connecting to {url}...")
    try:
        response = _http_get(url, session=session)
        # Check if the page loaded successfully
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error accessing the webpage: {e}")
        return

    # Parse the HTML index page to find all hyperlinks
    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)

    download_count = 0

    for tag in links:
        href = tag["href"]

        # Extract just the filename from the link path to check the prefix
        filename = os.path.basename(urlparse(href).path)

        # Check if the file matches your prefix requirement
        if filename.startswith(prefix) and filename.endswith(suffix):
            # Construct the absolute URL if the href is relative
            file_url = urljoin(url, href)
            local_file_path = os.path.join(output_folder, filename)

            if os.path.exists(local_file_path) and os.path.getsize(local_file_path) > 0:
                print(f"File {filename} already exists, skipping...")
                download_count += 1
                continue

            click.echo(f"\rDownloading: {filename}...", nl=False)
            temp_file_path = f"{local_file_path}.tmp"
            for attempt in range(1, MAX_FILE_RETRIES + 1):
                try:
                    # Stream the download so it handles large files smoothly without using too much RAM
                    with _http_get(file_url, session=session, stream=True) as file_response:
                        file_response.raise_for_status()
                        with open(temp_file_path, "wb") as f:
                            for chunk in file_response.iter_content(chunk_size=65536):
                                f.write(chunk)
                    os.replace(temp_file_path, local_file_path)
                    download_count += 1
                    break
                except requests.exceptions.RequestException as e:
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    if attempt < MAX_FILE_RETRIES:
                        time.sleep(FILE_RETRY_BACKOFF * attempt)
                    else:
                        print(f"Failed to download {filename}: {e}")

    print(f"\n✅ Task completed! Downloaded {download_count} file(s) to '{output_folder}'.")


def _uncompress_files(output_folder: str = OUTPUT_FOLDER) -> None:
    if not os.path.isdir(output_folder):
        click.echo("No .bz2 files found to uncompress.")
        return
    bz2_files = sorted(
        [
            f
            for f in os.listdir(output_folder)
            if f.endswith(".bz2") and os.path.isfile(os.path.join(output_folder, f))
        ]
    )
    total = len(bz2_files)
    if total == 0:
        click.echo("No .bz2 files found to uncompress.")
        return
    for i, filename in enumerate(bz2_files, 1):
        click.echo(f"\rUncompressing files: {i}/{total}...", nl=False)
        run_command(f"bunzip2 {shlex.quote(filename)}", cwd=output_folder)
    click.echo()


def _postprocess(output_file: str, output_folder: str | None = None) -> str:
    if output_folder is None:
        output_folder = OUTPUT_FOLDER
    filename = os.path.basename(output_file)
    _uncompress_files(output_folder)
    click.echo("Done. Merging all gribfiles into one ...")
    run_command("cdo merge *.grib2 combined.grib2", cwd=output_folder)
    click.echo("Done. Cleaning time series ...")
    run_command("cdo select,minute=0 combined.grib2 final.grib2", cwd=output_folder)
    click.echo("Done. Separate file for southern Sweden ...")
    run_command(f"cdo sellonlatbox,9,17,54,58 final.grib2 ../{filename}", cwd=output_folder)
    click.echo("Done. Deleting all intermediate files ...")
    run_command("rm *.grib2", cwd=output_folder)
    click.echo("\n✅DONE")
    return filename


@click.group()
def cli():
    pass

def run_cycle_is_up_to_date(run_cycle_date: datetime) -> bool:
    """
    Check if a run cycle has completed. All files need to have date which corresponds to the recent run cycle.
    Otherwise, it is not ready and it will return False.
    """

    run_cycle_str = str(run_cycle_date.hour).zfill(2)
    for sf in SINGLE_FIELDS:
        url = f"{BASE_URL}{run_cycle_str}/{sf}/"
        try:
            response = _http_get(url)
            # Check if the page loaded successfully
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error accessing the webpage: {e}")
            return False

        # Parse the HTML index page to find all hyperlinks
        soup = BeautifulSoup(response.text, "html.parser")
        links = soup.find_all("a", href=True)

        # Check that all links are newer than run cycle timestamp
        num_ok = 0
        for tag in links:
            href = tag["href"]
            filename = os.path.basename(urlparse(href).path)

            # Check if the file matches your prefix requirement
            run_cycle_date_str = run_cycle_date.strftime("%Y%m%d%H")
            if filename.startswith(FILE_PREFIX) and filename.endswith(FILE_SUFFIX):
                if run_cycle_date_str in filename:
                    num_ok += 1
                else:
                    click.echo(f"Check failed for {run_cycle_date_str} and filename: {filename}")
                    return False
        if num_ok == 0:
            click.echo(f"Check failed: no matching files found for {url}")
            return False
        click.echo(f"{url} {run_cycle_str} PASSED")
    return True

def _findlatest(max_steps: int = 8) -> datetime:
    now = datetime.now(timezone.utc)
    h = now.hour

    run_cycle_hour = (h // 3) * 3
    run_cycle_date = datetime(year=now.year, month=now.month, day=now.day, hour=run_cycle_hour)
    steps = 0
    while not run_cycle_is_up_to_date(run_cycle_date):
        steps += 1
        if steps >= max_steps:
            raise RuntimeError(f"No complete run cycle found within the last {max_steps * 3} hours.")
        # Stepping back past 00 moves to 21 on the previous day
        run_cycle_date -= timedelta(hours=3)
    click.echo(f"{run_cycle_date} is the most recent complete run cycle")
    return run_cycle_date

@cli.command()
def findlatest():
    click.echo('Searching for the most recent valid run cycle')
    _findlatest()

def _download(run_cycle_hour: int | datetime):
    if isinstance(run_cycle_hour, datetime):
        run_cycle_hour = run_cycle_hour.hour
    run_cycle_str = str(run_cycle_hour).zfill(2)
    for sf in SINGLE_FIELDS:
        _download_files_with_prefix(
            f"{BASE_URL}{run_cycle_str}/{sf}/",
            FILE_PREFIX,
            FILE_SUFFIX,
            OUTPUT_FOLDER)

@cli.command()
@click.option('--run_cycle_hour', type=int, help='Run cycle hour')
def download(run_cycle_hour: int):
    _download(run_cycle_hour)

def _upload(
    filepath: str | None = None,
    host: str = DEFAULT_FTP_HOST,
    user: str | None = None,
    password: str | None = None,
    remote_dir: str = DEFAULT_FTP_DIR,
    port: int = DEFAULT_FTP_PORT,
) -> None:
    if "/" in host:
        host_part, _, dir_part = host.partition("/")
        host = host_part
        if dir_part and remote_dir == DEFAULT_FTP_DIR:
            remote_dir = dir_part

    if filepath is None:
        latest = find_latest_local_grib()
        if latest is None:
            raise FileNotFoundError("No local timestamped GRIB file found to upload.")
        filepath = latest[1]

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"GRIB file not found: {filepath}")

    if not user:
        try:
            auth = netrc.netrc().authenticators(host)
            if auth:
                user = auth[0]
                if not password:
                    password = auth[2]
        except Exception:
            pass

    if not user:
        raise RuntimeError(
            "FTP username is required. Please set FTP_USER environment variable, pass --ftp-user, or configure ~/.netrc."
        )

    target_dir = remote_dir.strip("/") if not remote_dir.startswith("//") else "/" + remote_dir.lstrip("/")
    filename = os.path.basename(filepath)

    click.echo(f"Connecting to FTP server {host}:{port}...")
    with ftplib.FTP() as ftp:
        ftp.connect(host, port)
        ftp.login(user=user, passwd=password or "")
        if target_dir:
            try:
                ftp.cwd(target_dir)
            except ftplib.error_perm:
                try:
                    ftp.mkd(target_dir)
                    ftp.cwd(target_dir)
                except Exception:
                    raise

        dest_display = f"{host}/{target_dir}/{filename}" if target_dir else f"{host}/{filename}"
        click.echo(f"Uploading {filepath} to ftp://{dest_display}...")
        with open(filepath, "rb") as f:
            ftp.storbinary(f"STOR {filename}", f)
        click.echo(f"✅ Successfully uploaded {filename} to ftp://{dest_display}")


def _download_ftp(
    filepath: str | None = None,
    host: str = DEFAULT_FTP_HOST,
    user: str | None = None,
    password: str | None = None,
    remote_dir: str = DEFAULT_FTP_DIR,
    port: int = DEFAULT_FTP_PORT,
    remote_file: str | None = None,
    update: bool = False,
    keep: int = DEFAULT_KEEP_FILES,
) -> None:
    if "/" in host:
        host_part, _, dir_part = host.partition("/")
        host = host_part
        if dir_part and remote_dir == DEFAULT_FTP_DIR:
            remote_dir = dir_part

    if not user:
        try:
            auth = netrc.netrc().authenticators(host)
            if auth:
                user = auth[0]
                if not password:
                    password = auth[2]
        except Exception:
            pass

    if not user:
        raise RuntimeError(
            "FTP username is required. Please set FTP_USER environment variable, pass --ftp-user, or configure ~/.netrc."
        )

    target_dir = remote_dir.strip("/") if not remote_dir.startswith("//") else "/" + remote_dir.lstrip("/")

    is_dir = False
    if filepath:
        if filepath.endswith(("/", "\\")) or os.path.isdir(filepath):
            is_dir = True
            dest_dir = filepath.rstrip("/\\")
            local_dir = dest_dir if dest_dir else "."
        else:
            dest_dir = os.path.dirname(filepath)
            local_dir = dest_dir if dest_dir else "."
    else:
        dest_dir = ""
        local_dir = "."

    click.echo(f"Connecting to FTP server {host}:{port}...")
    with ftplib.FTP() as ftp:
        ftp.connect(host, port)
        ftp.login(user=user, passwd=password or "")
        if target_dir:
            ftp.cwd(target_dir)

        if update:
            if remote_file:
                remote_filename = remote_file
                remote_cycle = parse_grib_filename(remote_filename)
            else:
                remote_files = [os.path.basename(f) for f in ftp.nlst()]
                latest_remote = find_latest_grib_file(remote_files)
                if not latest_remote:
                    click.echo("No timestamped GRIB files found on FTP server.")
                    return
                remote_cycle, remote_filename = latest_remote

            if remote_cycle is not None:
                latest_local = find_latest_local_grib(local_dir)
                if latest_local is not None:
                    local_cycle, local_filename = latest_local
                    if _to_naive_utc(remote_cycle) <= _to_naive_utc(local_cycle):
                        click.echo(
                            f"Already up to date: local file {local_filename} is as new or newer than remote {remote_filename}."
                        )
                        if keep > 0:
                            cleanup_local_grib_files(local_dir, keep=keep)
                        return

            if filepath is None:
                target_filepath = os.path.join(dest_dir, remote_filename) if dest_dir else remote_filename
            elif is_dir:
                target_filepath = os.path.join(dest_dir, remote_filename)
            else:
                target_filepath = filepath
        else:
            if remote_file:
                remote_filename = remote_file
            elif filepath and not is_dir:
                remote_filename = os.path.basename(filepath)
            else:
                remote_files = [os.path.basename(f) for f in ftp.nlst()]
                latest_remote = find_latest_grib_file(remote_files)
                if not latest_remote:
                    click.echo("No timestamped GRIB files found on FTP server.")
                    return
                _, remote_filename = latest_remote

            if filepath is None:
                target_filepath = os.path.join(dest_dir, remote_filename) if dest_dir else remote_filename
            elif is_dir:
                target_filepath = os.path.join(dest_dir, remote_filename)
            else:
                target_filepath = filepath

        target_dest_dir = os.path.dirname(target_filepath)
        if target_dest_dir:
            os.makedirs(target_dest_dir, exist_ok=True)

        source_display = f"{host}/{target_dir}/{remote_filename}" if target_dir else f"{host}/{remote_filename}"
        click.echo(f"Downloading {remote_filename} from ftp://{source_display} to {target_filepath}...")
        temp_file_path = f"{target_filepath}.tmp"
        try:
            with open(temp_file_path, "wb") as f:
                ftp.retrbinary(f"RETR {remote_filename}", f.write)
            os.replace(temp_file_path, target_filepath)
        except Exception:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise
        click.echo(f"✅ Successfully downloaded {remote_filename} to {target_filepath}")
        if keep > 0:
            cleanup_local_grib_files(local_dir, keep=keep)


def ftp_options(f):
    f = click.option('--ftp-host', envvar='FTP_HOST', default=DEFAULT_FTP_HOST, show_default=True, help='FTP host')(f)
    f = click.option('--ftp-user', envvar=['FTP_USER', 'FTP_USERNAME'], default=None, help='FTP username')(f)
    f = click.option('--ftp-password', envvar=['FTP_PASSWORD', 'FTP_PASS'], default=None, help='FTP password')(f)
    f = click.option('--ftp-dir', envvar='FTP_DIR', default=DEFAULT_FTP_DIR, show_default=True, help='Remote directory on FTP server')(f)
    f = click.option('--ftp-port', envvar='FTP_PORT', default=DEFAULT_FTP_PORT, show_default=True, type=int, help='FTP port')(f)
    return f


@cli.command()
@click.option('--skip-upload', is_flag=True, default=False, help='Skip FTP upload')
@click.option('--update', is_flag=True, default=False, help='Download and build only if there is a newer run cycle')
@click.option('--keep', default=DEFAULT_KEEP_FILES, show_default=True, type=int, help='Number of latest local GRIB files to keep')
@ftp_options
def makefinalgrib(
    skip_upload: bool,
    update: bool,
    keep: int,
    ftp_host: str,
    ftp_user: str | None,
    ftp_password: str | None,
    ftp_dir: str,
    ftp_port: int,
):
    run_cycle = _findlatest()
    if isinstance(run_cycle, datetime):
        run_cycle_hour = run_cycle.hour
        filename = get_grib_filename(run_cycle)
        run_cycle_date = run_cycle
    else:
        run_cycle_hour = int(run_cycle)
        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y%m%d')}{str(run_cycle_hour).zfill(2)}.grib2"
        run_cycle_date = datetime(year=now.year, month=now.month, day=now.day, hour=run_cycle_hour)

    if update:
        latest_local = find_latest_local_grib()
        if latest_local is not None:
            local_cycle, local_filename = latest_local
            if _to_naive_utc(run_cycle_date) <= _to_naive_utc(local_cycle):
                click.echo(
                    f"Already up to date: local file {local_filename} is as new or newer than latest run cycle {filename}."
                )
                if keep > 0:
                    cleanup_local_grib_files(".", keep=keep)
                return

    _download(run_cycle_hour)
    _postprocess(filename)
    if not skip_upload:
        _upload(
            filename,
            host=ftp_host,
            user=ftp_user,
            password=ftp_password,
            remote_dir=ftp_dir,
            port=ftp_port,
        )
    if keep > 0:
        cleanup_local_grib_files(".", keep=keep)


@cli.command()
@click.option('--file', 'filepath', default=None, help='GRIB file to upload')
@ftp_options
def upload(filepath: str | None, ftp_host: str, ftp_user: str | None, ftp_password: str | None, ftp_dir: str, ftp_port: int):
    """Upload GRIB file to FTP server."""
    _upload(
        filepath,
        host=ftp_host,
        user=ftp_user,
        password=ftp_password,
        remote_dir=ftp_dir,
        port=ftp_port,
    )


@cli.command(name="downloadftp")
@click.option('--file', '-o', '--output', 'filepath', default=None, help='GRIB file to download')
@click.option('--remote-file', default=None, help='Remote filename on FTP server (defaults to basename of --file)')
@click.option('--update', is_flag=True, default=False, help='Download only if there is a newer run cycle')
@click.option('--keep', default=DEFAULT_KEEP_FILES, show_default=True, type=int, help='Number of latest local GRIB files to keep')
@ftp_options
def downloadftp(
    filepath: str | None,
    remote_file: str | None,
    update: bool,
    keep: int,
    ftp_host: str,
    ftp_user: str | None,
    ftp_password: str | None,
    ftp_dir: str,
    ftp_port: int,
):
    """Download GRIB file from FTP server."""
    _download_ftp(
        filepath=filepath,
        host=ftp_host,
        user=ftp_user,
        password=ftp_password,
        remote_dir=ftp_dir,
        port=ftp_port,
        remote_file=remote_file,
        update=update,
        keep=keep,
    )


@cli.command()
@click.option('--directory', '-d', default='.', show_default=True, help='Directory containing GRIB files')
@click.option('--keep', default=DEFAULT_KEEP_FILES, show_default=True, type=int, help='Number of latest GRIB files to keep')
def cleanup(directory: str, keep: int):
    """Keep only the latest GRIB files in a directory, removing older ones."""
    deleted = cleanup_local_grib_files(directory, keep=keep)
    if not deleted:
        click.echo(f"No GRIB files needed cleanup (keeping {keep}).")
    else:
        click.echo(f"Cleanup completed: removed {len(deleted)} file(s), kept {keep}.")


cli.add_command(
    click.Command(
        "prune",
        callback=cleanup.callback,
        params=cleanup.params,
        help=cleanup.help,
        hidden=True,
    )
)


cli.add_command(
    click.Command(
        "download-ftp",
        callback=downloadftp.callback,
        params=downloadftp.params,
        help=downloadftp.help,
        hidden=True,
    )
)
cli.add_command(
    click.Command(
        "download_ftp",
        callback=downloadftp.callback,
        params=downloadftp.params,
        help=downloadftp.help,
        hidden=True,
    )
)


if __name__ == '__main__':
    cli()
