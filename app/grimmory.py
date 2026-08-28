"""Client for the BookLore/Grimmory v1 JSON API (see the GRIMMORY_* settings in
config.py).

Grimmory is a BookLore instance (Java Spring Boot backend) that serves the
home-lab Calibre ebook libraries.  Authentication is a JWT flow:

    POST /api/v1/auth/login   {"username", "password"}    (public endpoint)
       -> {"accessToken", "refreshToken", "expires", ...}

Every other /api/** endpoint requires `Authorization: Bearer <accessToken>`.
Endpoints used here (shapes confirmed from the source of the deployed fork):

    GET /api/v1/libraries               -> [{"id", "name", ...}, ...]
    GET /api/v1/libraries/{id}/book     -> [{"title", "libraryId", "libraryName",
                                             "metadata": {...}, ...}, ...]
    GET /api/v1/libraries/{id}/format-counts -> {"<format>": <bookCount>, ...}

The `metadata` object carries title, authors (list of strings), publisher,
publishedDate (ISO date string), isbn13, isbn10.  All fields are optional --
the API omits nulls -- so nothing here assumes any of them is present.
"""

import sys
from typing import Optional

import httpx

from .config import (
    GRIMMORY_LIBRARIES,
    GRIMMORY_PASSWORD,
    GRIMMORY_URL,
    GRIMMORY_USERNAME,
)


class GrimmoryError(RuntimeError):
    """Raised on failed login or a failed/invalid API response."""


def _base_url() -> str:
    return GRIMMORY_URL.rstrip("/")


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=timeout, follow_redirects=True)


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    timeout: float = 30.0,
) -> str:
    """POST /api/v1/auth/login and return the Bearer accessToken.

    Defaults to the GRIMMORY_USERNAME/GRIMMORY_PASSWORD env settings; a real
    password is only ever supplied at run time via the environment.
    """
    user = GRIMMORY_USERNAME if username is None else username
    pw = GRIMMORY_PASSWORD if password is None else password
    if not user or not pw:
        raise GrimmoryError(
            "missing Grimmory credentials: set GRIMMORY_USERNAME and "
            "GRIMMORY_PASSWORD environment variables"
        )
    try:
        with _client(timeout) as client:
            resp = client.post(
                "/api/v1/auth/login", json={"username": user, "password": pw}
            )
    except httpx.HTTPError as e:
        raise GrimmoryError(f"login request to {_base_url()} failed: {e}") from e
    if resp.status_code != 200:
        raise GrimmoryError(
            f"login failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not token:
        raise GrimmoryError(f"login response missing accessToken: {str(data)[:200]}")
    return token


def list_libraries(token: str, *, timeout: float = 30.0) -> list:
    """GET /api/v1/libraries -> list of library dicts (id, name, ...)."""
    try:
        with _client(timeout) as client:
            resp = client.get("/api/v1/libraries", headers=_auth_headers(token))
    except httpx.HTTPError as e:
        raise GrimmoryError(f"GET /api/v1/libraries failed: {e}") from e
    if resp.status_code == 401:
        raise GrimmoryError("authentication rejected (401): token invalid/expired")
    if resp.status_code != 200:
        raise GrimmoryError(
            f"GET /api/v1/libraries failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    libraries = resp.json()
    if not isinstance(libraries, list):
        raise GrimmoryError(f"expected a JSON list of libraries, got {type(libraries).__name__}")
    return libraries


def fetch_library_books(token: str, library_id, *, timeout: float = 120.0) -> list:
    """GET /api/v1/libraries/{id}/book -> list of raw Book dicts."""
    url = f"/api/v1/libraries/{library_id}/book"
    try:
        with _client(timeout) as client:
            resp = client.get(url, headers=_auth_headers(token))
    except httpx.HTTPError as e:
        raise GrimmoryError(f"GET {url} failed: {e}") from e
    if resp.status_code == 401:
        raise GrimmoryError("authentication rejected (401): token invalid/expired")
    if resp.status_code != 200:
        raise GrimmoryError(f"GET {url} failed (HTTP {resp.status_code}): {resp.text[:200]}")
    books = resp.json()
    if not isinstance(books, list):
        raise GrimmoryError(f"expected a JSON list of books, got {type(books).__name__}")
    return books


def library_format_counts(token: str, library_id, *, timeout: float = 30.0) -> int:
    """Sum of /api/v1/libraries/{id}/format-counts values (cheap book count).

    A book with N alternative file formats is counted N times, so this is a
    display-side approximation -- it is deliberately used for NON-target
    libraries in the probe, where a full book fetch would be a large, slow
    payload we have no use for.
    """
    url = f"/api/v1/libraries/{library_id}/format-counts"
    try:
        with _client(timeout) as client:
            resp = client.get(url, headers=_auth_headers(token))
    except httpx.HTTPError as e:
        raise GrimmoryError(f"GET {url} failed: {e}") from e
    if resp.status_code != 200:
        raise GrimmoryError(f"GET {url} failed (HTTP {resp.status_code}): {resp.text[:200]}")
    counts = resp.json()
    if not isinstance(counts, dict):
        raise GrimmoryError(f"expected a JSON object of format counts, got {type(counts).__name__}")
    return int(sum(counts.values()))


def books_to_rows(books, library_id, library_name) -> list:
    """Map raw Book dicts into flat DB-ready rows.

    Row keys: title, author, publisher, published_date, isbn, library_id,
    library_name.  Tolerates a null/absent `metadata` object (the API omits
    nulls) and falls back to the top-level book title.
    """
    rows = []
    for book in books:
        if not isinstance(book, dict):
            continue
        meta = book.get("metadata") if isinstance(book.get("metadata"), dict) else {}
        authors = meta.get("authors") or []
        title = book.get("title") or meta.get("title")
        published = meta.get("publishedDate")
        if published is not None and not isinstance(published, str):
            published = str(published)
        rows.append(
            {
                "library_id": library_id if library_id is not None else book.get("libraryId"),
                "library_name": library_name,
                "title": title,
                "author": ",".join(a for a in authors if a),
                "publisher": meta.get("publisher"),
                "published_date": published,
                "isbn": meta.get("isbn13") or meta.get("isbn10"),
            }
        )
    return rows


def target_library_names() -> list:
    """The comma-separated GRIMMORY_LIBRARIES names, stripped of whitespace."""
    return [name.strip() for name in GRIMMORY_LIBRARIES.split(",") if name.strip()]


def _probe() -> int:
    """__main__ entry point: log in, list every library with its book count,
    and exit non-zero if any target library name is missing.

    Target libraries get a real book fetch (that is the endpoint the DB build
    depends on, so the probe proves the full path).  Other libraries are
    counted via format-counts so we don't drag their (potentially huge)
    book payloads just to print a number.
    """
    targets = set(target_library_names())
    try:
        token = login()
        libraries = list_libraries(token)
    except GrimmoryError as e:
        print(f"GRIMMORY PROBE FAILED: {e}", file=sys.stderr)
        return 1

    seen_targets = set()
    for library in libraries:
        if not isinstance(library, dict):
            continue
        name = library.get("name") or "<unnamed>"
        lib_id = library.get("id")
        if name in targets:
            books = fetch_library_books(token, lib_id)
            count = len(books)
            seen_targets.add(name)
        else:
            count = library_format_counts(token, lib_id)
        print(f"{name!r} (id={lib_id}): {count} books")

    missing = targets - seen_targets
    if missing:
        print(
            "GRIMMORY PROBE FAILED: target library(ies) not found: "
            + ", ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_probe())
