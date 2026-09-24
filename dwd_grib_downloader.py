import click
from datetime import datetime
from util import run_command
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

RUN_CYCLES = [0,3,6,9,12,15,18,21]
BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-d2/grib/"
SINGLE_FIELDS = ["u_10m","v_10m","vmax_10m","tot_prec","t_2m"]
FILE_PREFIX = "icon-d2_germany_regular-lat-lon_single-level"
FILE_SUFFIX = "grib2.bz2"
OUTPUT_FOLDER = "./downloaded_files"


def _download_files_with_prefix(url, prefix, suffix, output_folder):
    # Ensure the output directory exists
    os.makedirs(output_folder, exist_ok=True)

    print(f"Connecting to {url}...")
    try:
        response = requests.get(url)
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

            print(f"Downloading: {filename}...")
            try:
                # Stream the download so it handles large files smoothly without using too much RAM
                with requests.get(file_url, stream=True) as file_response:
                    file_response.raise_for_status()
                    with open(local_file_path, "wb") as f:
                        for chunk in file_response.iter_content(chunk_size=8192):
                            f.write(chunk)
                download_count += 1
            except requests.exceptions.RequestException as e:
                print(f"Failed to download {filename}: {e}")

    print(f"\n✅ Task completed! Downloaded {download_count} file(s) to '{output_folder}'.")


def _postprocess() -> None:
    click.echo("Uncompressing files...")
    run_command("bunzip2 *.bz2", cwd=OUTPUT_FOLDER)
    click.echo("Done. Merging all gribfiles into one ...")
    run_command("cdo merge *.grib2 combined.grib2", cwd=OUTPUT_FOLDER)
    click.echo("Done. Separate file for southern Sweden ...")
    run_command("cdo sellonlatbox,10,14,54,57 combined.grib2 ../combined2.grib2", cwd=OUTPUT_FOLDER)
    click.echo("Done. Deleting all intermediate files ...")
    run_command("rm *.grib2", cwd=OUTPUT_FOLDER)
    click.echo("\n✅DONE")


@click.group()
def cli():
    pass

def run_cycle_is_up_to_date(run_cycle_date:datetime) -> bool:
    """
    Check if a run cycle has completed. All files need to have date which corresponds to the recent run cycle.
    Otherwise, it is not ready and it will return False.
    """

    run_cycle_str = str(run_cycle_date.hour).zfill(2)
    for sf in SINGLE_FIELDS:
        url = f"{BASE_URL}{run_cycle_str}/{sf}/"
        try:
            response = requests.get(url)
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
        click.echo(f"{url} {run_cycle_str} PASSED")
    return True

def _findlatest():
    now = datetime.now()
    h = now.hour

    run_cycle_hour = int(h/3)*3
    run_cycle_date = datetime(year=now.year, month=now.month, day=now.day, hour=run_cycle_hour)
    stop_condition = False
    while not stop_condition:
        stop_condition = run_cycle_is_up_to_date(run_cycle_date)
        if not stop_condition:
            run_cycle_hour -= 3
            if run_cycle_hour < 0:
                run_cycle_hour = 21
            run_cycle_date = datetime(year=now.year, month=now.month, day=now.day, hour=run_cycle_hour)
    click.echo(f"{run_cycle_date} is the most recent complete run cycle")
    return run_cycle_hour

@cli.command()
def findlatest():
    click.echo('Searching for the most recent valid run cycle')
    _findlatest()

def _download(run_cycle_hour:int):
    run_cycle_str = str(run_cycle_hour).zfill(2)
    for sf in SINGLE_FIELDS:
        _download_files_with_prefix(
            f"https://opendata.dwd.de/weather/nwp/icon-d2/grib/{run_cycle_str}/{sf}/",
            FILE_PREFIX,
            FILE_SUFFIX,
            OUTPUT_FOLDER)

@cli.command()
@click.option('--run_cycle_hour', type=int, help='Run cycle hour')
def download(run_cycle_hour:int):
    _download(run_cycle_hour)

@cli.command()
def makefinalgrib():
    run_cycle_hour = _findlatest()
    _download(run_cycle_hour)
    _postprocess()

if __name__ == '__main__':
    cli()
