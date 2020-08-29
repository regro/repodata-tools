import hashlib
import os
import datetime


def split_pkg(pkg):
    """code due to isuruf and CJ-
    """
    if not pkg.endswith(".tar.bz2"):
        raise RuntimeError("Can only process packages that end in .tar.bz2")
    pkg = pkg[:-8]
    plat, pkg_name = pkg.split(os.path.sep)
    name_ver, build = pkg_name.rsplit('-', 1)
    name, ver = name_ver.rsplit('-', 1)
    return plat, name, ver, build


def compute_md5(pth):
    with open(pth, "rb") as f:
        file_hash = hashlib.md5()
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            file_hash.update(chunk)
    return file_hash.hexdigest()


def chunk_iterable(iterable, chunk_size):
    """Generate sequences of `chunk_size` elements from `iterable`.

    https://stackoverflow.com/a/12797249/1745538
    """
    chunk_size = max(chunk_size, 1)

    iterable = iter(iterable)
    while True:
        chunk = []
        try:
            for _ in range(chunk_size):
                chunk.append(next(iterable))
            yield chunk
        except StopIteration:
            if chunk:
                yield chunk
            break


def print_github_api_limits(gh):
    # modified from the webservices repo
    remaining = gh.get_rate_limit().core.remaining
    total = gh.get_rate_limit().core.limit
    reset_time = gh.get_rate_limit().core.reset
    reset_time -= datetime.utcnow()

    print("===================================================", flush=True)
    print("===================================================", flush=True)
    print("remaining requests: %d of %d" % (remaining, total), flush=True)
    print("reset time: %s" % reset_time, flush=True)
    print("===================================================", flush=True)
    print("===================================================", flush=True)
