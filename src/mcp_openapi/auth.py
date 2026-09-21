"""Token acquisition: run a helper command, cache what it hands back.

There is no built-in login flow. A helper is any executable — the server runs
it, reads a token from stdout, and caches it until it expires. Whether a helper
refreshes with a stored refresh token or performs a full login is its own
business, kept in its own state.

The contract is specified in ``docs/token-protocol.md``; this module implements
it, and the section marks below point back at the clause they carry out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .config import ResolvedDeployment, TokenHelperConfig

logger = logging.getLogger(__name__)

#: A token this close to expiry is treated as already expired.
EXPIRY_BUFFER_SECONDS = 60.0

#: In-memory lifetime for helper output that carries no ``expires_at``.
BARE_TOKEN_LIFETIME_SECONDS = 300.0

#: How much of a failing helper's stderr is surfaced to the caller (§6.2).
STDERR_CAPTURE_BYTES = 4096

#: Most stdout a helper may write; anything beyond it is malformed (§6.5).
MAX_STDOUT_BYTES = 64 * 1024

#: How much of an error payload's ``message`` and ``action`` is surfaced.
DETAIL_LIMIT = 500

#: Grace between SIGTERM and SIGKILL when a helper overruns its timeout (§2.3).
KILL_GRACE_SECONDS = 2.0

#: How long to wait for another process to finish running the same helper.
DISK_LOCK_TIMEOUT = 60.0

#: Reserved for what the caller sets: every name in it is stripped from the
#: environment a helper inherits, so a helper only ever reads one we set (§3.1).
HELPER_ENV_PREFIX = "TOKEN_HELPER_"

#: Carries the resolved username to the helper (§3.2).
USERNAME_ENV = f"{HELPER_ENV_PREFIX}USERNAME"

#: An RFC 7230 token — the only shape a header name may take (§4.2).
HEADER_NAME_RE = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")

#: The two error codes a helper may report (§4.6); they mean exactly what exit
#: codes 2 and 1 mean.
NEEDS_HUMAN = "needs_human"
TRANSIENT = "transient"

#: Appended to whichever of the two forms says a human must act.
REAUTH_HINT = " -- this needs you to re-authenticate out of band"


class AuthError(Exception):
    """Raised when a token helper fails, times out, or returns unusable output.

    ``needs_action`` marks the two forms that say a human must act — exit code
    ``2`` and the ``needs_human`` error payload — which no retry can fix and
    which the user must resolve out of band, since a helper has no path back
    into the MCP session.
    """

    def __init__(self, message: str, *, needs_action: bool = False) -> None:
        super().__init__(message)
        self.needs_action = needs_action


@dataclass
class Token:
    token: str
    header: str = "Authorization"
    token_type: str = "Bearer"
    expires_at: float | None = None
    #: Identity the token was minted for (§4.5). ``None`` for the bare-token
    #: form, which carries no fields and so reports no identity.
    username: str | None = None

    @property
    def header_value(self) -> str:
        """``"<scheme> <token>"``, or the bare token when the scheme is empty."""
        return f"{self.token_type} {self.token}".lstrip()

    def fresh(self, now: float) -> bool:
        return self.expires_at is None or now + EXPIRY_BUFFER_SECONDS < self.expires_at


def cache_key(name: str, command: TokenHelperConfig, username: str | None) -> str:
    """Identity of a token: the declaration it came from, and who it is for.

    Commands run verbatim, so a declaration always yields the same token --
    except per user, which is the one thing the server varies. The argument set
    is part of the key and not just the profile name, because argument sets are
    what tell one declaration from another (§1.2): re-pointing a helper at a
    different login endpoint must not reuse what the old one minted. Hashed
    rather than used directly because a username is not guaranteed to be a safe
    filename.
    """
    payload = json.dumps(
        [name, command.command, command.args, username or ""], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_command(command: str) -> str:
    """Absolute path for ``command``.

    Goes through ``shutil.which`` so that tools installed as ``.cmd``/``.bat``
    shims -- ``uvx``, ``npx`` -- are found on Windows, where ``CreateProcess``
    cannot execute them directly.
    """
    found = shutil.which(command)
    if found is None:
        raise AuthError(f"token helper {command!r} not found on PATH")
    return found


def _build_env(username: str | None) -> dict[str, str]:
    """The server's environment, minus our namespace, plus the username (§3).

    Inherited wholesale: sourcing credentials is the helper's business, so it
    gets the environment it would have had if you ran it yourself. Everything
    under ``TOKEN_HELPER_`` is dropped first, so a value left in the server's
    own environment can never be mistaken for one this call resolved.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(HELPER_ENV_PREFIX)}
    if username:
        env[USERNAME_ENV] = username
    return env


def _has_control(text: str) -> bool:
    return any(ch < "\x20" or ch == "\x7f" for ch in text)


def _malformed(command: str, detail: str) -> AuthError:
    """Malformed output (§4.10).

    Surfaced rather than retried: a second run produces the same bytes. The
    detail never quotes stdout, which is not logged or echoed at any level
    (§6.4) -- only fields parsed out of a well-formed payload may be.
    """
    return AuthError(f"token helper {command!r} returned malformed output: {detail}")


def _parse_stdout(raw: bytes, command: str, username: str | None) -> Token:
    """Read a helper's stdout as a bare token, a token payload, or an error.

    Discriminated by a JSON parse (§4.1): an object carries either
    ``access_token`` or ``error`` and never both, and anything else -- a JWT is
    not valid JSON, a bare number is not an object -- is the token itself.
    """
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        raise _malformed(command, "exit 0 with nothing on stdout")
    if _has_control(text):
        raise _malformed(command, "a control character in the output")

    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = None

    if not isinstance(payload, dict):
        # The bare form carries no fields, and so reports no identity (§4.5).
        return Token(token=text)

    has_token = "access_token" in payload
    has_error = "error" in payload
    if has_token == has_error:
        raise _malformed(
            command, "an object carrying neither 'access_token' nor 'error', or both"
        )
    if has_error:
        raise _helper_error(command, payload["error"])
    return _token_payload(command, payload, username)


def _helper_error(command: str, error: Any) -> AuthError:
    """Turn a well-formed error payload into the exception it stands for (§4.6)."""
    if not isinstance(error, dict):
        return _malformed(command, "an 'error' that is not an object")
    code = error.get("code")
    if code not in (NEEDS_HUMAN, TRANSIENT):
        # Not echoed back: an unrecognized code is not a well-formed field.
        return _malformed(command, "an unrecognized 'error.code'")

    # Only message and action are surfaced, and only out of a payload that
    # parsed (§6.4).
    detail = ": ".join(
        part[:DETAIL_LIMIT]
        for key in ("message", "action")
        if isinstance(part := error.get(key), str) and part
    )
    needs_action = code == NEEDS_HUMAN
    suffix = f": {detail}" if detail else ""
    logger.warning("token helper %r reported %s%s", command, code, suffix)
    hint = REAUTH_HINT if needs_action else ""
    return AuthError(
        f"token helper {command!r} reported {code}{hint}{suffix}",
        needs_action=needs_action,
    )


def _token_payload(
    command: str, payload: dict[str, Any], username: str | None
) -> Token:
    """Validate a token payload and take the five fields it may carry (§4.2)."""
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise _malformed(command, "an empty or non-string 'access_token'")
    if _has_control(token):
        raise _malformed(command, "a control character in 'access_token'")

    # A payload always knows who it minted for, and a mismatch means the token
    # is not the one this call asked for (§4.5).
    minted_for = payload.get("username")
    if not isinstance(minted_for, str) or not minted_for:
        raise _malformed(command, "a missing, empty or non-string 'username'")
    if username is not None and minted_for != username:
        raise _malformed(
            command,
            f"a token minted for {minted_for!r}, not the requested {username!r}",
        )

    expires_at = payload.get("expires_at")
    if expires_at is not None and (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(expires_at)
    ):
        raise _malformed(command, "a non-numeric 'expires_at'")

    token_type = payload.get("token_type", "Bearer")
    if token_type not in ("Bearer", ""):
        raise _malformed(command, "a 'token_type' that is neither 'Bearer' nor empty")

    header = payload.get("header", "Authorization")
    if not isinstance(header, str) or not HEADER_NAME_RE.match(header):
        raise _malformed(command, "a 'header' that is not an RFC 7230 token")

    # Unknown keys are ignored on purpose: that is the forward-compatibility
    # story, and why the protocol carries no version field (§4.8).
    return Token(
        token=token,
        header=header,
        token_type=token_type,
        expires_at=float(expires_at) if expires_at is not None else None,
        username=minted_for,
    )


async def _drain(stream: asyncio.StreamReader, keep: int) -> tuple[bytes, bool]:
    """Read a pipe to EOF, retaining its first ``keep`` bytes.

    Read to the end rather than stopping at ``keep``: a helper whose pipe fills
    blocks on the write, and a blocked helper only ever ends in a timeout kill.
    Returns what was kept, and whether more than that arrived.
    """
    buf = bytearray()
    overflowed = False
    while chunk := await stream.read(65536):
        room = keep - len(buf)
        if room > 0:
            buf += chunk[:room]
        overflowed = overflowed or len(chunk) > max(room, 0)
    return bytes(buf), overflowed


async def _collect(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bool, bytes, int | None]:
    assert proc.stdout is not None and proc.stderr is not None
    (stdout, oversize), (stderr, _) = await asyncio.gather(
        _drain(proc.stdout, MAX_STDOUT_BYTES),
        _drain(proc.stderr, STDERR_CAPTURE_BYTES),
    )
    return stdout, oversize, stderr, await proc.wait()


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the helper's whole process group -- ``uvx`` and friends spawn children."""
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
    except TimeoutError:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def run_helper(
    argv: list[str], env: dict[str, str], timeout: float, username: str | None = None
) -> Token:
    """Run one token helper and parse its result."""
    spawn: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - platform-specific
        spawn["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        spawn["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            # The server speaks MCP over stdio, so its own stdin/stdout are the
            # JSON-RPC channel. A child that inherited stdout would corrupt that
            # stream with a single stray print.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **spawn,
        )
    except OSError as e:
        raise AuthError(f"could not start token helper {argv[0]!r}: {e}") from e

    try:
        stdout, oversize, stderr, returncode = await asyncio.wait_for(
            _collect(proc), timeout
        )
    except TimeoutError:
        # A kill is transient, never needs_action: only the helper knows that,
        # and it never got to say so (§5.2).
        await _terminate(proc)
        raise AuthError(
            f"token helper {argv[0]!r} timed out after {timeout:g}s"
        ) from None

    if returncode != 0:
        # The exit code decides, however well-formed stdout looks (§5.3).
        detail = stderr.decode("utf-8", "replace").strip()
        needs_action = returncode == 2
        logger.warning(
            "token helper %r exited %s%s",
            argv[0],
            returncode,
            f": {detail}" if detail else "",
        )
        hint = REAUTH_HINT if needs_action else ""
        raise AuthError(
            f"token helper {argv[0]!r} exited {returncode}{hint}"
            + (f": {detail}" if detail else ""),
            needs_action=needs_action,
        )

    # stderr is discarded on success and never logged at any level (§6.3): it is
    # the first place a token leaks, the day someone debugs their helper with a
    # stray print.
    if oversize:
        raise _malformed(argv[0], f"more than {MAX_STDOUT_BYTES // 1024} KiB on stdout")
    return _parse_stdout(stdout, argv[0], username)


class TokenProvider:
    """Runs token helpers and caches their results in memory and on disk.

    The disk tier is not optional. Every client session runs its own server
    process, so without it five open windows mean five helper spawns, and a
    ``uvx`` cold start turns that into seconds of latency on first call.
    """

    def __init__(self, cache_root: Path) -> None:
        self._dir = cache_root / "tokens"
        self._memory: dict[str, Token] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def header_for(
        self, deployment: ResolvedDeployment, username: str | None = None
    ) -> tuple[str, str] | None:
        """Return the ``(header, value)`` a call should carry, or ``None``.

        ``None`` means no token helper resolved for the deployment, so it sends
        no auth header rather than an empty one.
        """
        if deployment.token_helper is None or deployment.token_helper_name is None:
            return None
        token = await self.token(
            deployment.token_helper_name, deployment.token_helper, username
        )
        return token.header, token.header_value

    async def token(
        self, name: str, command: TokenHelperConfig, username: str | None = None
    ) -> Token:
        key = cache_key(name, command, username)

        now = time.time()
        cached = self._memory.get(key)
        if cached is not None and cached.fresh(now):
            return cached

        async with self._lock_for(key):
            cached = self._memory.get(key)
            if cached is not None and cached.fresh(time.time()):
                return cached
            return await self._fetch(key, name, command, username)

    def retire(
        self, deployment: ResolvedDeployment, username: str | None = None
    ) -> None:
        """Drop the cached token for a deployment.

        A ``401`` retires a token whatever its ``expires_at`` claimed (§4.9),
        so the next call mints a fresh one instead of replaying a credential the
        upstream API has already refused.
        """
        if deployment.token_helper is None or deployment.token_helper_name is None:
            return
        key = cache_key(deployment.token_helper_name, deployment.token_helper, username)
        self._memory.pop(key, None)
        (self._dir / f"{key}.json").unlink(missing_ok=True)

    async def _fetch(
        self,
        key: str,
        name: str,
        command: TokenHelperConfig,
        username: str | None,
    ) -> Token:
        path = self._dir / f"{key}.json"
        # thread_local=False because the acquire happens on a worker thread
        # (it blocks) while the release happens back on the event loop; with
        # filelock's default the release would find a zero counter on this
        # thread and leave the OS lock held forever.
        lock = FileLock(str(path.with_suffix(".lock")), thread_local=False)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            _harden_dir(self._dir)
            await asyncio.to_thread(lock.acquire, DISK_LOCK_TIMEOUT)
        except Timeout:
            raise AuthError(
                f"timed out waiting for another process to run token helper {name!r}"
            ) from None
        except OSError as e:
            raise AuthError(f"token cache at {self._dir} is unusable: {e}") from e

        try:
            # Re-read under the lock: without this, concurrent sessions all
            # stampede the helper the first time a token is needed.
            on_disk = _read_disk(path)
            if on_disk is not None and on_disk.fresh(time.time()):
                self._memory[key] = on_disk
                return on_disk

            resolved = _resolve_command(command.command)
            token = await run_helper(
                [resolved, *command.args],
                _build_env(username),
                command.timeout,
                username,
            )
            if token.username is not None:
                logger.debug("token helper %r minted for %s", name, token.username)

            if token.expires_at is not None:
                _write_disk(path, token)
            else:
                # Nothing says when it goes stale, so there is no defensible
                # moment to invalidate it on disk -- memory only, briefly.
                token.expires_at = time.time() + BARE_TOKEN_LIFETIME_SECONDS
            self._memory[key] = token
            return token
        finally:
            lock.release()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def purge(self) -> int:
        """Delete every cached token. Returns how many files were removed."""
        self._memory.clear()
        if not self._dir.is_dir():
            return 0
        removed = 0
        for path in self._dir.iterdir():
            if path.suffix in {".json", ".lock"}:
                path.unlink(missing_ok=True)
                removed += path.suffix == ".json"
        return removed


def _harden_dir(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - best effort
        pass


def _read_disk(path: Path) -> Token | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("access_token"), str):
        return None
    expires_at = raw.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return None
    username = raw.get("username")
    return Token(
        token=raw["access_token"],
        header=str(raw.get("header", "Authorization")),
        token_type=str(raw.get("token_type", "Bearer")),
        expires_at=float(expires_at),
        username=username if isinstance(username, str) else None,
    )


def _write_disk(path: Path, token: Token) -> None:
    body = json.dumps(
        {
            "access_token": token.token,
            "expires_at": token.expires_at,
            "token_type": token.token_type,
            "header": token.header,
            "username": token.username,
            "written_at": time.time(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    # Staged through a scratch file unique to this writer, then installed
    # atomically, so a reader never observes a half-written token.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except OSError as e:  # pragma: no cover - the disk tier is best effort
        tmp.unlink(missing_ok=True)
        logger.warning("could not write token cache at %s: %s", path, e)
