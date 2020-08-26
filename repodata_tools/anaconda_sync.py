import os
import sys
import tempfile
import subprocess
import time
import hmac
import hashlib

import tenacity
import click
import rapidjson as json
import requests
import tqdm
import github
import joblib

from .utils import chunk_iterable, compute_md5
from .shards import (
    make_repodata_shard_noretry,
    get_old_shard_path,
    get_shard_path,
    read_subdir_shards,
)
from .releases import (
    get_or_make_release,
    upload_asset
)


def _build_shard(subdir, pkg, label):
    subdir_pkg = os.path.join(subdir, pkg)
    if label == "main":
        url = (
            "https://conda.anaconda.org/conda-forge"
            f"/{subdir_pkg}"
        )
    else:
        url = (
            "https://conda.anaconda.org/conda-forge"
            f"/label/{label}/{subdir_pkg}"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        shard = make_repodata_shard_noretry(
            subdir,
            pkg,
            label,
            None,
            url,
            tmpdir,
        )

    return shard


def _write_shards(
    shards_to_write, all_shards, chunk_index, total_chunks, label, subdir
):
    for subdir_pkg in shards_to_write:
        pth = get_shard_path(*os.path.split(subdir_pkg))

        if subdir_pkg in all_shards:
            dir = os.path.dirname(pth)
            os.makedirs(dir, exist_ok=True)

            with open(pth, "w") as fp:
                json.dump(
                    all_shards[subdir_pkg], fp, sort_keys=True, indent=2
                )

        subprocess.run(f"git add {pth}", shell=True)

    cip1 = chunk_index + 1
    subprocess.run(
        "git commit -m "
        f"'chunk {cip1} of {total_chunks} {label}/{subdir} "
        "[ci skip]  [cf admin skip] ***NO_CI***'",
        shell=True,
        check=True,
    )


@tenacity.retry(
    wait=tenacity.wait_random_exponential(multiplier=0.1, max=10),
    stop=tenacity.stop_after_attempt(5),
    reraise=True,
)
def _push_repo():
    subprocess.run("git pull --rebase --no-edit", shell=True, check=True)
    subprocess.run("git push", shell=True, check=True)


def update_shards(labels, all_shards, rank, n_ranks, start_time, time_limit=3300):
    shards_to_write = set()
    for label in tqdm.tqdm(labels, desc="labels"):

        for loop_index, subdir in enumerate([
            "linux-64", "osx-64", "win-64", "noarch",
            "linux-aarch64", "linux-ppc64le", "osx-arm64"
        ]):
            if loop_index % n_ranks != rank:
                continue

            if label == "main":
                r = requests.get(
                    "https://conda.anaconda.org/conda-forge/"
                    f"{subdir}/repodata_from_packages.json"
                )
            else:
                r = requests.get(
                    "https://conda.anaconda.org/conda-forge/label/"
                    f"{label}/{subdir}/repodata.json"
                )

            if r.status_code != 200:
                continue

            rd = r.json()

            os.makedirs(f"shards/{subdir}", exist_ok=True)

            all_pkgs = sorted(list(rd["packages"]))

            total_chunks = len(rd["packages"]) // 64 + 1
            for chunk_index, pkg_chunk in tqdm.tqdm(
                enumerate(chunk_iterable(all_pkgs, 64)),
                desc=f"{label}/{subdir}",
                total=total_chunks,
            ):
                jobs = []
                max_bytes = 0
                for pkg in pkg_chunk:
                    subdir_pkg = os.path.join(subdir, pkg)

                    new_shard_pth = get_shard_path(subdir, pkg)

                    for old_shard_pth in [
                        get_old_shard_path(subdir, pkg),
                        get_shard_path(subdir, pkg, n_dirs=4),
                    ]:
                        if os.path.exists(old_shard_pth):
                            if not os.path.exists(new_shard_pth):
                                os.makedirs(
                                    os.path.dirname(new_shard_pth),
                                    exist_ok=True,
                                )
                                subprocess.run(
                                    "git mv %s %s" % (
                                        old_shard_pth, get_shard_path(subdir, pkg)
                                    ),
                                    shell=True,
                                    check=True,
                                )
                                shards_to_write.add(subdir_pkg)
                                with open(new_shard_pth, "r") as fp:
                                    all_shards[subdir_pkg] = json.load(fp)
                            else:
                                subprocess.run(
                                    "git rm -f %s" % old_shard_pth,
                                    shell=True,
                                    check=True,
                                )

                            break

                    if subdir_pkg not in all_shards:
                        max_bytes = max(max_bytes, rd["packages"][pkg]["size"])
                        jobs.append(joblib.delayed(_build_shard)(
                            subdir, pkg, label
                        ))
                    else:
                        if label not in all_shards[subdir_pkg]["labels"]:
                            all_shards[subdir_pkg]["labels"].append(label)
                            shards_to_write.add(subdir_pkg)

                        main_url = (
                            "https://conda.anaconda.org/conda-forge"
                            f"/{subdir_pkg}"
                        )
                        if (
                            label == "main"
                            and all_shards[subdir_pkg]["url"] != main_url
                            and "conda.anaconda.org" in all_shards[subdir_pkg]["url"]
                        ):
                            all_shards[subdir_pkg]["url"] = main_url
                            shards_to_write.add(subdir_pkg)

                if jobs:
                    max_gb = max_bytes / 1000**3
                    n_jobs = min(max(int(1.0 / max_gb), 1), 16)
                    print(
                        "using %d processes for %d jobs w/ max GB of %s" % (
                            n_jobs, len(jobs), max_gb
                        ),
                        flush=True,
                    )
                    shards = joblib.Parallel(n_jobs=n_jobs, verbose=0)(jobs)
                    for shard in shards:
                        subdir_pkg = os.path.join(shard["subdir"], shard["package"])
                        all_shards[subdir_pkg] = shard
                        shards_to_write.add(subdir_pkg)

                if len(shards_to_write) > 64 or time.time() - start_time > time_limit:
                    _write_shards(
                        shards_to_write,
                        all_shards,
                        chunk_index,
                        total_chunks,
                        label,
                        subdir
                    )
                    shards_to_write = set()

                    try:
                        _push_repo()
                    except Exception:
                        pass

                if time.time() - start_time > time_limit:
                    return True

    if shards_to_write:
        _write_shards(
            shards_to_write,
            all_shards,
            chunk_index,
            total_chunks,
            label,
            subdir
        )

        try:
            _push_repo()
        except Exception:
            pass

    return False


@tenacity.retry(
    wait=tenacity.wait_random_exponential(multiplier=0.1, max=10),
    stop=tenacity.stop_after_attempt(5),
    reraise=True,
)
def _download_package(tmpdir, subdir, pkg, url, md5_checksum):
    os.makedirs(f"{tmpdir}/{subdir}", exist_ok=True)
    subprocess.run(
        f"curl  --no-progress-meter -L {url} > {tmpdir}/{subdir}/{pkg}",
        shell=True,
        check=True,
    )

    if md5_checksum is not None:
        local_md5 = compute_md5(f"{tmpdir}/{subdir}/{pkg}")
        if not hmac.compare_digest(local_md5, md5_checksum):
            raise RuntimeError("md5 chechsum is incorrect! exiting!")


def _make_release(subdir, pkg, shard):
    gh = github.Github(os.environ["GITHUB_TOKEN"])
    repo = gh.get_repo("regro/releases")

    # make release and upload if shard does not exist
    with tempfile.TemporaryDirectory() as tmpdir:
        _download_package(
            tmpdir, subdir, pkg, shard["url"], shard["repodata"]["md5"]
        )
        rel = get_or_make_release(repo, subdir, pkg)

        ast = upload_asset(
            rel,
            f"{tmpdir}/{subdir}/{pkg}",
            content_type="application/x-bzip2",
        )

        shard["url"] = ast.browser_download_url
        with open(f"{tmpdir}/repodata_shard.json", "w") as fp:
            json.dump(shard, fp, sort_keys=True, indent=2)

        ast = upload_asset(
            rel,
            f"{tmpdir}/repodata_shard.json",
            content_type="application/json",
        )


def _write_shard(subdir_pkg, shard):
    pth = get_shard_path(*os.path.split(subdir_pkg))

    dir = os.path.dirname(pth)
    os.makedirs(dir, exist_ok=True)

    with open(pth, "w") as fp:
        json.dump(
            shard, fp, sort_keys=True, indent=2
        )

    subprocess.run(f"git add {pth}", shell=True)

    subprocess.run(
        "git commit -m "
        f"'release update {subdir_pkg} "
        "[ci skip]  [cf admin skip] ***NO_CI***'",
        shell=True,
        check=True,
    )


def upload_packages(all_shards, rank, n_ranks, max_write=200):
    num_written = 0
    for subdir_pkg, shard in all_shards.items():
        shard_index = hashlib.sha1(subdir_pkg.encode("utf-8")).digest()[0] % 4
        if shard_index % n_ranks != rank:
            continue
        subdir, pkg = os.path.split(subdir_pkg)

        if "conda.anaconda.org" in shard["url"]:
            _make_release(subdir, pkg, shard)
            _write_shard(subdir_pkg, shard)
            try:
                _push_repo()
            except Exception:
                pass
            num_written += 1

        if num_written >= max_write:
            break


@click.command()
@click.option(
    "--rank",
    default=0,
    type=int,
    help="The rank of the process. Should be in tha range [0, n_ranks-1]."
)
@click.option(
    "--n-ranks",
    default=1,
    type=int,
    help="The number of processes to split the sync over."
)
@click.option(
    "--time-limit",
    default=3000,
    type=int,
    help="The maximum time to run in seconds."
)
def main(rank, n_ranks, time_limit):
    """Sync anaconda repodata shards w/ a local copy and upload packages to
    GitHub.
    """
    start_time = time.time()

    all_shards = {}
    print("reading all shards", flush=True)
    for subdir in [
        "linux-64", "linux-aarch64", "linux-ppc64le",
        "osx-64", "win-64",
        "noarch", "osx-arm64"
    ]:
        read_subdir_shards(".", subdir, all_shards)
    print(" ", flush=True)

    print("getting labels", flush=True)
    label_info = requests.get(
        "https://api.anaconda.org/channels/conda-forge",
        headers={'Authorization': 'token {}'.format(os.environ["BINSTAR_TOKEN"])}
    ).json()

    labels = sorted(
        label
        for label in label_info
        if "/" not in label
    )
    counts = {label: label_info[label]["count"] for label in labels}
    labels = sorted(labels, key=lambda x: counts[x], reverse=True)
    for label in labels:
        print("%-32s %s" % (label, counts[label]), flush=True)
    print(" ", flush=True)

    print("updating shards", flush=True)
    quit = update_shards(
        labels,
        all_shards,
        rank,
        n_ranks,
        start_time,
        time_limit=time_limit,
    )
    print(" ", flush=True)

    if quit:
        sys.exit(0)

    # print("uploading releases", flush=True)
    # upload_packages(all_shards, rank, n_ranks, max_write=200)
    # print(" ", flush=True)
