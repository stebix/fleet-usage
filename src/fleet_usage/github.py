"""Thin GitHub REST client for the private data repository.

Only the Contents API, the Git Trees API and two repository endpoints are
used, so that the tool needs nothing but a fine-grained token with
contents read and write.

Every failure mode of the transport is mapped onto one of the exception
classes defined here, and only those exceptions ever leave this module.
The mapping is deliberately narrow:

* ``401`` and ``403`` become :class:`AuthError`, except a ``403`` that
  carries rate limit headers, which becomes :class:`RateLimitError`.
* ``404`` becomes :class:`NotFoundError`.
* ``409`` and ``422`` become :class:`RemoteConflictError`.
* Transport level failures become :class:`NetworkError`.

:class:`RateLimitError` derives from :class:`AuthError` because GitHub
reports a secondary rate limit with the same status code as a permission
problem. Callers that treat the two differently must therefore test for
:class:`RateLimitError` first.
"""

import base64
import dataclasses
import datetime as dt
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from fleet_usage import __version__

__all__ = [
    'API_VERSION',
    'DEFAULT_TIMEOUT_SECONDS',
    'JSON_MEDIA_TYPE',
    'MAX_ATTEMPTS',
    'RAW_MEDIA_TYPE',
    'RETRY_BUDGET_SECONDS',
    'AccessReport',
    'AuthError',
    'GitHubClient',
    'GitHubError',
    'NetworkError',
    'NotFoundError',
    'RateLimitError',
    'RemoteConflictError',
    'check_access',
]

API_VERSION = '2022-11-28'
JSON_MEDIA_TYPE = 'application/vnd.github+json'
RAW_MEDIA_TYPE = 'application/vnd.github.raw+json'
DEFAULT_API_BASE_URL = 'https://api.github.com'
DEFAULT_TIMEOUT_SECONDS = 30.0

MAX_ATTEMPTS = 5
RETRY_BUDGET_SECONDS = 120.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0

_LOGGER = logging.getLogger(__name__)


class GitHubError(Exception):
    """Base class for every GitHub transport or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str | None = None,
    ) -> None:
        """Store the message and the context of the failure.

        Parameters
        ----------
        message : str
            Human readable description, including the message the API
            returned whenever there was one.
        status_code : int or None, optional
            HTTP status code, when the failure came from a response.
        path : str or None, optional
            Repository relative path the request was about.
        """
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class AuthError(GitHubError):
    """The token is missing, expired or lacks the required scope."""


class RateLimitError(AuthError):
    """A primary or secondary rate limit was hit.

    Attributes
    ----------
    retry_after : float or None
        Seconds to wait before retrying, as advertised by the API.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
        path: str | None = None,
    ) -> None:
        """Store the wait hint alongside the usual context.

        Parameters
        ----------
        message : str
            Human readable description.
        retry_after : float or None, optional
            Seconds to wait, derived from ``Retry-After`` or from the
            rate limit reset timestamp.
        status_code : int or None, optional
            HTTP status code.
        path : str or None, optional
            Repository relative path.
        """
        super().__init__(message, status_code=status_code, path=path)
        self.retry_after = retry_after


class NotFoundError(GitHubError):
    """The requested path or repository does not exist."""


class RemoteConflictError(GitHubError):
    """The remote state conflicts with the state being written."""


class NetworkError(GitHubError):
    """The request could not be completed after the bounded retries."""


@dataclasses.dataclass(frozen=True, slots=True)
class AccessReport:
    """What a token can do with the configured repository.

    Attributes
    ----------
    repository
        ``OWNER/NAME`` that was probed.
    exists
        Whether the repository is visible to the token.
    can_read
        Whether the metadata could be read at all.
    can_write
        Whether the repository reports push permission.
    detail
        Human readable summary for ``fleet-usage doctor``.
    """

    repository: str
    exists: bool
    can_read: bool
    can_write: bool
    detail: str


def _decode_json(response: httpx.Response) -> Any:
    """Parse a response body as JSON.

    Parameters
    ----------
    response : httpx.Response
        The response to decode.

    Returns
    -------
    Any
        The decoded document.

    Raises
    ------
    GitHubError
        If the body is not valid JSON.
    """
    try:
        return response.json()
    except ValueError as exc:
        msg = f'GitHub returned a non-JSON body: {exc}'
        raise GitHubError(msg, status_code=response.status_code) from exc


def _api_message(response: httpx.Response) -> str:
    """Extract the ``message`` field of an error response.

    Parameters
    ----------
    response : httpx.Response
        The response to inspect.

    Returns
    -------
    str
        The reported message, or the raw text when the body is not the
        usual JSON envelope.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if isinstance(payload, dict):
        message = payload.get('message')
        if isinstance(message, str):
            errors = payload.get('errors')
            if isinstance(errors, list) and errors:
                return f'{message} ({json.dumps(errors)})'
            return message
    return response.text.strip()[:200]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Derive how long to wait from the rate limit headers.

    Parameters
    ----------
    response : httpx.Response
        A ``403`` or ``429`` response.

    Returns
    -------
    float or None
        Seconds to wait, or ``None`` when the response carries no hint.
    """
    raw = response.headers.get('retry-after')
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    reset = response.headers.get('x-ratelimit-reset')
    if reset:
        try:
            target = float(reset)
        except ValueError:
            return None
        now = dt.datetime.now(dt.UTC).timestamp()
        return max(0.0, target - now)
    return None


def _is_rate_limited(response: httpx.Response) -> bool:
    """Whether a ``403`` response is really a rate limit.

    Parameters
    ----------
    response : httpx.Response
        The response to classify.

    Returns
    -------
    bool
        ``True`` when the response carries rate limit headers or the
        usual secondary rate limit wording.
    """
    headers = response.headers
    if headers.get('retry-after'):
        return True
    if headers.get('x-ratelimit-remaining') == '0':
        return True
    message = _api_message(response).lower()
    return 'rate limit' in message or 'abuse detection' in message


class GitHubClient:
    """Minimal client around the endpoints the tool needs.

    All constructor parameters are keyword only, so that the order of
    ``token`` and ``repository`` can never be confused at a call site.
    """

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        branch: str = 'main',
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        retry_budget: float = RETRY_BUDGET_SECONDS,
    ) -> None:
        """Create the underlying HTTP client.

        Parameters
        ----------
        token : str
            GitHub token with contents read and write.
        repository : str
            ``OWNER/NAME`` of the data repository.
        branch : str, optional
            Branch to read from and write to.
        api_base_url : str, optional
            API base URL, without a trailing slash.
        timeout : float, optional
            Per request timeout in seconds.
        transport : httpx.BaseTransport or None, optional
            Transport override, used by the tests.
        sleep : callable or None, optional
            Injected sleep, used by the tests to keep retries instant.
            Defaults to :func:`time.sleep`, looked up when it is
            called so that a test can replace it.
        max_attempts : int, optional
            Number of attempts per request, including the first.
        retry_budget : float, optional
            Upper bound on the total time spent sleeping between the
            attempts of a single request.

        Raises
        ------
        ValueError
            If ``repository`` is not of the form ``OWNER/NAME``.
        """
        owner, _, name = repository.partition('/')
        if not owner or not name or '/' in name:
            msg = f"expected 'OWNER/NAME', got {repository!r}"
            raise ValueError(msg)
        self.repository = repository
        self.owner = owner
        self.name = name
        self.branch = branch
        self.api_base_url = api_base_url.rstrip('/')
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._retry_budget = retry_budget
        self._client = httpx.Client(
            base_url=self.api_base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': JSON_MEDIA_TYPE,
                'X-GitHub-Api-Version': API_VERSION,
                'User-Agent': f'fleet-usage/{__version__}',
            },
        )

    def __enter__(self) -> 'GitHubClient':
        """Enter a context that closes the client on exit.

        Returns
        -------
        GitHubClient
            This client.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client.

        Parameters
        ----------
        *exc_info : object
            Ignored exception information.
        """
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    # -- transport ---------------------------------------------------

    def _backoff(self, attempt: int, hint: float | None) -> float:
        """Compute the delay before the next attempt.

        Parameters
        ----------
        attempt : int
            Zero based index of the attempt that just failed.
        hint : float or None
            Delay advertised by the API, if any.

        Returns
        -------
        float
            Seconds to sleep, exponential with full jitter and capped.
        """
        if hint is not None:
            return min(hint, BACKOFF_CAP_SECONDS)
        window: float = min(
            BACKOFF_BASE_SECONDS * 2.0**attempt, BACKOFF_CAP_SECONDS
        )
        return window / 2 + random.random() * (window / 2)

    def _classify(self, response: httpx.Response, path: str) -> GitHubError:
        """Turn an error response into the matching exception.

        Parameters
        ----------
        response : httpx.Response
            A response with a status of 400 or above.
        path : str
            Repository relative path, for the error message.

        Returns
        -------
        GitHubError
            The exception to raise; never raised here so that the retry
            loop can decide whether the failure is transient.
        """
        status = response.status_code
        message = _api_message(response)
        detail = (
            f'{status} for {path}: {message}'
            if path
            else f'{status}: {message}'
        )
        if status == 429 or (status == 403 and _is_rate_limited(response)):
            return RateLimitError(
                f'rate limited by GitHub ({detail})',
                retry_after=_retry_after_seconds(response),
                status_code=status,
                path=path or None,
            )
        if status in (401, 403):
            return AuthError(
                f'GitHub rejected the token ({detail})',
                status_code=status,
                path=path or None,
            )
        if status == 404:
            return NotFoundError(
                f'not found ({detail})',
                status_code=status,
                path=path or None,
            )
        if status in (409, 422):
            return RemoteConflictError(
                f'remote conflict ({detail})',
                status_code=status,
                path=path or None,
            )
        return GitHubError(
            f'unexpected GitHub response ({detail})',
            status_code=status,
            path=path or None,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        path: str = '',
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        accept: str = JSON_MEDIA_TYPE,
    ) -> httpx.Response:
        """Perform one API request with bounded retries.

        Network failures, ``5xx`` responses and rate limits are retried
        with exponential backoff and full jitter; an advertised
        ``Retry-After`` always wins over the computed delay. Permission
        failures are never retried.

        Parameters
        ----------
        method : str
            HTTP method.
        url : str
            Path below the API base URL.
        path : str, optional
            Repository relative path, used in error messages only.
        params : dict or None, optional
            Query parameters.
        json_body : dict or None, optional
            JSON request body.
        accept : str, optional
            Media type to request.

        Returns
        -------
        httpx.Response
            A response with a status below 400.

        Raises
        ------
        GitHubError
            If the request keeps failing, or fails in a way that is not
            worth retrying.
        """
        spent = 0.0
        last: GitHubError | None = None
        for attempt in range(self._max_attempts):
            hint: float | None = None
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={'Accept': accept},
                )
            except httpx.HTTPError as exc:
                last = NetworkError(
                    f'{method} {url} failed: {exc}', path=path or None
                )
            else:
                if response.status_code < 400:
                    return response
                error = self._classify(response, path)
                if isinstance(error, RateLimitError):
                    hint = error.retry_after
                elif not (500 <= response.status_code < 600):
                    raise error
                last = error
            if attempt + 1 >= self._max_attempts:
                break
            delay = self._backoff(attempt, hint)
            if spent + delay > self._retry_budget:
                break
            _LOGGER.info(
                'retrying %s %s in %.1fs (%s)', method, url, delay, last
            )
            if self._sleep is None:
                time.sleep(delay)
            else:
                self._sleep(delay)
            spent += delay
        assert last is not None
        raise last

    # -- contents ----------------------------------------------------

    def _contents_url(self, path: str) -> str:
        """Build the Contents API URL of ``path``.

        Parameters
        ----------
        path : str
            Repository relative path.

        Returns
        -------
        str
            The URL below the API base.
        """
        cleaned = path.strip('/')
        return f'/repos/{self.repository}/contents/{cleaned}'

    def get_file(self, path: str) -> tuple[bytes, str]:
        """Fetch a file and its blob SHA.

        Files above one megabyte are returned by the API without their
        content; those are fetched a second time with the raw media
        type, which streams the blob itself.

        Parameters
        ----------
        path : str
            Repository relative path.

        Returns
        -------
        tuple of (bytes, str)
            File content and blob SHA.

        Raises
        ------
        NotFoundError
            If the path does not exist on the configured branch.
        GitHubError
            On any other transport or protocol failure.
        """
        response = self._request(
            'GET',
            self._contents_url(path),
            path=path,
            params={'ref': self.branch},
        )
        payload = _decode_json(response)
        if isinstance(payload, list):
            msg = f'{path} is a directory, not a file'
            raise GitHubError(msg, path=path)
        if not isinstance(payload, dict):
            msg = f'unexpected contents payload for {path}'
            raise GitHubError(msg, path=path)
        sha = str(payload.get('sha', ''))
        encoding = payload.get('encoding')
        content = payload.get('content')
        if encoding == 'base64' and isinstance(content, str) and content:
            try:
                return base64.b64decode(content), sha
            except (ValueError, TypeError) as exc:
                msg = f'{path} has an undecodable base64 body: {exc}'
                raise GitHubError(msg, path=path) from exc
        raw = self._request(
            'GET',
            self._contents_url(path),
            path=path,
            params={'ref': self.branch},
            accept=RAW_MEDIA_TYPE,
        )
        return raw.content, sha

    def put_file(
        self,
        path: str,
        content: bytes,
        message: str,
        sha: str | None = None,
    ) -> str:
        """Create or update a file.

        Parameters
        ----------
        path : str
            Repository relative path.
        content : bytes
            New file content.
        message : str
            Commit message.
        sha : str or None, optional
            Blob SHA of the version being replaced. Omit it to create a
            file; GitHub then answers ``422`` when it already exists.

        Returns
        -------
        str
            The new blob SHA.

        Raises
        ------
        AuthError
            If the token may not write.
        RemoteConflictError
            If the remote moved on, or the file already exists.
        NotFoundError
            If the repository or the branch does not exist.
        GitHubError
            On any other transport or protocol failure.
        """
        body: dict[str, Any] = {
            'message': message,
            'content': base64.b64encode(content).decode('ascii'),
            'branch': self.branch,
        }
        if sha is not None:
            body['sha'] = sha
        response = self._request(
            'PUT', self._contents_url(path), path=path, json_body=body
        )
        payload = _decode_json(response)
        if isinstance(payload, dict):
            blob = payload.get('content')
            if isinstance(blob, dict) and isinstance(blob.get('sha'), str):
                return str(blob['sha'])
        msg = f'PUT {path} returned no blob SHA'
        raise GitHubError(msg, path=path)

    # -- trees -------------------------------------------------------

    def _tree(self, ref: str, *, recursive: bool) -> dict[str, Any]:
        """Fetch one tree object.

        Parameters
        ----------
        ref : str
            Branch name or tree SHA.
        recursive : bool
            Whether to ask for the whole subtree.

        Returns
        -------
        dict
            The decoded tree object.

        Raises
        ------
        GitHubError
            If the payload is not a tree object.
        """
        params = {'recursive': '1'} if recursive else None
        response = self._request(
            'GET',
            f'/repos/{self.repository}/git/trees/{ref}',
            path=ref,
            params=params,
        )
        payload = _decode_json(response)
        if not isinstance(payload, dict):
            msg = f'unexpected tree payload for {ref}'
            raise GitHubError(msg, path=ref)
        return payload

    def _subtree_sha(self, prefix: str) -> str | None:
        """Resolve the tree SHA of a directory by walking the root.

        Parameters
        ----------
        prefix : str
            Repository relative directory.

        Returns
        -------
        str or None
            The tree SHA, or ``None`` when the directory is absent.
        """
        ref = self.branch
        for part in prefix.strip('/').split('/'):
            entries = self._tree(ref, recursive=False).get('tree', [])
            found = None
            for entry in entries:
                if entry.get('path') == part and entry.get('type') == 'tree':
                    found = str(entry.get('sha'))
                    break
            if found is None:
                return None
            ref = found
        return ref

    def _list_contents(self, path: str) -> list[str]:
        """List blobs below ``path`` with the Contents API.

        This is the fallback for repositories whose tree is too large
        for the Git Trees API even below the prefix.

        Parameters
        ----------
        path : str
            Repository relative directory.

        Returns
        -------
        list of str
            Blob paths, unsorted.
        """
        try:
            response = self._request(
                'GET',
                self._contents_url(path),
                path=path,
                params={'ref': self.branch},
            )
        except NotFoundError:
            return []
        payload = _decode_json(response)
        if not isinstance(payload, list):
            return []
        blobs: list[str] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            entry_path = str(entry.get('path', ''))
            if entry.get('type') == 'file':
                blobs.append(entry_path)
            elif entry.get('type') == 'dir':
                blobs.extend(self._list_contents(entry_path))
        return blobs

    def list_tree(self, prefix: str) -> list[str]:
        """List every blob path below ``prefix``.

        The recursive Git Trees call is tried first because it costs a
        single request. When GitHub truncates the answer the same call
        is repeated on the subtree of ``prefix``, and when even that is
        truncated the Contents API is walked directory by directory.

        Parameters
        ----------
        prefix : str
            Repository relative directory.

        Returns
        -------
        list of str
            Sorted blob paths below ``prefix``; empty when the prefix,
            the branch or the repository content does not exist.

        Raises
        ------
        AuthError
            If the token may not read the repository.
        GitHubError
            On any other transport or protocol failure.
        """
        cleaned = prefix.strip('/')
        marker = f'{cleaned}/' if cleaned else ''
        try:
            payload = self._tree(self.branch, recursive=True)
        except NotFoundError:
            return []
        except RemoteConflictError:
            # An empty repository has no commit and therefore no tree.
            return []
        if not payload.get('truncated'):
            return sorted(
                str(entry['path'])
                for entry in payload.get('tree', [])
                if entry.get('type') == 'blob'
                and str(entry.get('path', '')).startswith(marker)
            )
        return sorted(self._list_truncated(cleaned, marker))

    def _list_truncated(self, cleaned: str, marker: str) -> list[str]:
        """Handle a truncated root tree.

        Parameters
        ----------
        cleaned : str
            Prefix without surrounding slashes.
        marker : str
            Prefix with a trailing slash, or the empty string.

        Returns
        -------
        list of str
            Blob paths below the prefix, unsorted.
        """
        if not cleaned:
            return self._list_contents('')
        try:
            sub_sha = self._subtree_sha(cleaned)
        except NotFoundError:
            return []
        if sub_sha is None:
            return []
        subtree = self._tree(sub_sha, recursive=True)
        if subtree.get('truncated'):
            return self._list_contents(cleaned)
        return [
            f'{marker}{entry["path"]}'
            for entry in subtree.get('tree', [])
            if entry.get('type') == 'blob'
        ]

    # -- repository --------------------------------------------------

    def get_repo(self) -> dict[str, Any]:
        """Fetch the repository object.

        Returns
        -------
        dict
            The API response, including the ``permissions`` object when
            the token may see it.

        Raises
        ------
        NotFoundError
            If the repository does not exist or is invisible.
        GitHubError
            On any other transport or protocol failure.
        """
        response = self._request(
            'GET', f'/repos/{self.repository}', path=self.repository
        )
        payload = _decode_json(response)
        if not isinstance(payload, dict):
            msg = f'unexpected repository payload for {self.repository}'
            raise GitHubError(msg)
        return payload

    def repo_exists(self) -> bool:
        """Whether the repository is visible to the token.

        Returns
        -------
        bool
            ``True`` when the repository object could be fetched.
        """
        try:
            self.get_repo()
        except NotFoundError:
            return False
        return True

    def authenticated_login(self) -> str:
        """Return the login of the authenticated user.

        Returns
        -------
        str
            The ``login`` field of ``GET /user``.

        Raises
        ------
        GitHubError
            If the endpoint answers without a login, which happens for
            tokens that do not represent a user.
        """
        response = self._request('GET', '/user', path='user')
        payload = _decode_json(response)
        if isinstance(payload, dict) and isinstance(payload.get('login'), str):
            return str(payload['login'])
        msg = 'GET /user returned no login'
        raise GitHubError(msg)

    def create_repo(self, private: bool = True) -> dict[str, Any]:
        """Create the data repository.

        The repository is created for the authenticated user when the
        owner in the configured ``OWNER/NAME`` is that user, and inside
        the organisation otherwise. It is initialised with a commit so
        that the branch exists and the Contents API can be used right
        away.

        Parameters
        ----------
        private : bool, optional
            Whether the repository is private.

        Returns
        -------
        dict
            The API response.

        Raises
        ------
        RemoteConflictError
            If a repository of that name already exists.
        GitHubError
            On any other transport or protocol failure.
        """
        body: dict[str, Any] = {
            'name': self.name,
            'private': private,
            'auto_init': True,
            'description': 'fleet-usage data repository',
        }
        login = self.authenticated_login()
        url = (
            '/user/repos'
            if login.lower() == self.owner.lower()
            else f'/orgs/{self.owner}/repos'
        )
        response = self._request(
            'POST', url, path=self.repository, json_body=body
        )
        payload = _decode_json(response)
        if not isinstance(payload, dict):
            msg = f'unexpected create payload for {self.repository}'
            raise GitHubError(msg)
        return payload


def check_access(client: GitHubClient) -> AccessReport:
    """Probe what ``client`` may do with its repository.

    Wire this into ``fleet-usage doctor`` for the ``github
    reachability`` check: it never raises, so a diagnostic can render
    the report unconditionally.

    Parameters
    ----------
    client : GitHubClient
        A client built from the settings and the resolved token.

    Returns
    -------
    AccessReport
        Existence, read access and inferred write access.
    """
    repository = client.repository
    try:
        payload = client.get_repo()
    except NotFoundError:
        return AccessReport(
            repository=repository,
            exists=False,
            can_read=False,
            can_write=False,
            detail=f'{repository} does not exist or the token cannot see it',
        )
    except AuthError as exc:
        return AccessReport(
            repository=repository,
            exists=False,
            can_read=False,
            can_write=False,
            detail=str(exc),
        )
    except GitHubError as exc:
        return AccessReport(
            repository=repository,
            exists=False,
            can_read=False,
            can_write=False,
            detail=str(exc),
        )
    permissions = payload.get('permissions')
    perms = permissions if isinstance(permissions, dict) else {}
    can_write = bool(
        perms.get('push') or perms.get('maintain') or perms.get('admin')
    )
    visibility = 'private' if payload.get('private') else 'public'
    detail = (
        f'{repository} ({visibility}), '
        f'{"read and write" if can_write else "read only"}'
    )
    return AccessReport(
        repository=repository,
        exists=True,
        can_read=True,
        can_write=can_write,
        detail=detail,
    )
