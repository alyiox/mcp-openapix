"""Tests for the token helper protocol: invocation, stdout contract, caching, failures.

Helpers are real subprocesses rather than stubs, so the environment the child
receives, the stdout contract and the exit-code handling are all exercised.
Section marks refer to ``docs/token-protocol.md``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from mcp_openapi.auth import (
    HELPER_ENV_PREFIX,
    MAX_STDOUT_BYTES,
    USERNAME_ENV,
    AuthError,
    TokenProvider,
    cache_key,
)
from mcp_openapi.config import TokenHelperConfig

COUNTER = "counter.txt"

#: The identity a helper reports: the one it was asked for, or its own default
#: when the call resolved none (§4.5).
IDENTITY = f"os.environ.get({USERNAME_ENV!r}, 'default')"

JSON_TOKEN = (
    "print(json.dumps({'access_token': 'k', 'username': " + IDENTITY + ","
    " 'expires_at': time.time() + 3600}))"
)


@pytest.fixture
def provider(tmp_path: Path) -> TokenProvider:
    return TokenProvider(tmp_path / "cache")


def helper(tmp_path: Path, body: str, **kwargs) -> TokenHelperConfig:
    """A token helper whose source is ``body``, counting its own invocations."""
    script = tmp_path / f"helper_{abs(hash(body)) % 10**8}.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        f"c = pathlib.Path({str(tmp_path / COUNTER)!r})\n"
        "c.write_text(str(int(c.read_text()) + 1) if c.exists() else '1')\n" + body,
        encoding="utf-8",
    )
    return TokenHelperConfig(command=sys.executable, args=[str(script)], **kwargs)


def runs(tmp_path: Path) -> int:
    path = tmp_path / COUNTER
    return int(path.read_text()) if path.exists() else 0


async def test_bare_stdout_is_the_token(provider, tmp_path) -> None:
    token = await provider.token("p", helper(tmp_path, "print('raw-token')"))
    assert token.token == "raw-token"
    assert token.header_value == "Bearer raw-token"
    # The bare form carries no fields, and so reports no identity (§4.5).
    assert token.username is None


async def test_json_stdout_carries_expiry_and_scheme(provider, tmp_path) -> None:
    cmd = helper(
        tmp_path,
        "print(json.dumps({'access_token': 'k', 'username': 'ada@example.com',"
        " 'expires_at': time.time() + 3600, 'token_type': '',"
        " 'header': 'X-Api-Key', 'refresh_token': 'ignored'}))",
    )
    token = await provider.token("p", cmd)
    assert token.header == "X-Api-Key"
    # An empty token_type sends the raw token; unknown keys are ignored, which
    # is the forward-compatibility story (§4.8).
    assert token.header_value == "k"
    assert token.expires_at is not None
    assert token.username == "ada@example.com"


async def test_args_are_passed_verbatim(provider, tmp_path) -> None:
    """No substitution: what the config says is what runs."""
    cmd = helper(tmp_path, "print(sys.argv[1])")
    cmd = cmd.model_copy(update={"args": [*cmd.args, "{service}"]})
    assert (await provider.token("p", cmd)).token == "{service}"


async def test_token_is_cached_in_memory(provider, tmp_path) -> None:
    cmd = helper(tmp_path, JSON_TOKEN)
    await provider.token("p", cmd)
    await provider.token("p", cmd)
    assert runs(tmp_path) == 1


async def test_disk_cache_is_shared_across_processes(tmp_path) -> None:
    """A second server process reuses the token rather than re-running the helper."""
    cmd = helper(tmp_path, JSON_TOKEN)
    root = tmp_path / "cache"
    assert (await TokenProvider(root).token("p", cmd)).token == "k"
    assert (await TokenProvider(root).token("p", cmd)).token == "k"
    assert runs(tmp_path) == 1


async def test_bare_token_is_never_written_to_disk(tmp_path) -> None:
    """Nothing says when it goes stale, so there is no moment to invalidate it."""
    cmd = helper(tmp_path, "print('raw')")
    root = tmp_path / "cache"
    await TokenProvider(root).token("p", cmd)
    assert list((root / "tokens").glob("*.json")) == []
    await TokenProvider(root).token("p", cmd)
    assert runs(tmp_path) == 2


async def test_expired_token_is_refetched(provider, tmp_path) -> None:
    soon = (
        "print(json.dumps({'access_token':'k','username':'u',"
        "'expires_at':time.time()+5}))"
    )
    cmd = helper(tmp_path, soon)
    await provider.token("p", cmd)
    # Inside EXPIRY_BUFFER_SECONDS of expiry, so it counts as already expired.
    await provider.token("p", cmd)
    assert runs(tmp_path) == 2


async def test_username_reaches_the_helper(provider, tmp_path) -> None:
    cmd = helper(tmp_path, f"print({IDENTITY})")
    assert (await provider.token("p", cmd, "alice")).token == "alice"


async def test_username_is_absent_when_unset(provider, tmp_path, monkeypatch) -> None:
    """A helper that does not authenticate per user must see no stale value."""
    monkeypatch.setenv(USERNAME_ENV, "leftover")
    cmd = helper(tmp_path, f"print({IDENTITY})")
    assert (await provider.token("p", cmd)).token == "default"


async def test_reserved_namespace_is_scrubbed(provider, tmp_path, monkeypatch) -> None:
    """The whole namespace is the caller's, not the environment's (§3.1)."""
    monkeypatch.setenv(f"{HELPER_ENV_PREFIX}SCOPE", "admin")
    cmd = helper(
        tmp_path, f"print(os.environ.get({HELPER_ENV_PREFIX + 'SCOPE'!r}, '-'))"
    )
    assert (await provider.token("p", cmd)).token == "-"


async def test_tokens_are_not_shared_between_users(provider, tmp_path) -> None:
    cmd = helper(tmp_path, JSON_TOKEN)
    await provider.token("p", cmd, "alice")
    await provider.token("p", cmd, "bob")
    await provider.token("p", cmd, "alice")
    assert runs(tmp_path) == 2


def test_cache_key_varies_by_declaration_and_user() -> None:
    cmd = TokenHelperConfig(command="acme-token", args=["--login-url", "https://a"])
    other_args = cmd.model_copy(update={"args": ["--login-url", "https://b"]})
    assert cache_key("p", cmd, "alice") == cache_key("p", cmd, "alice")
    assert cache_key("p", cmd, "alice") != cache_key("p", cmd, "bob")
    assert cache_key("p", cmd, None) != cache_key("q", cmd, None)
    # Argument sets, not names alone, tell one declaration from another (§1.2).
    assert cache_key("p", cmd, None) != cache_key("p", other_args, None)


async def test_helper_inherits_the_server_environment(
    provider, tmp_path, monkeypatch
) -> None:
    """Sourcing credentials is the helper's business, so it gets the real env."""
    monkeypatch.setenv("ACME_PASSWORD", "s3cret")
    cmd = helper(tmp_path, "print(os.environ['ACME_PASSWORD'])")
    assert (await provider.token("p", cmd)).token == "s3cret"


async def test_nonzero_exit_surfaces_stderr(provider, tmp_path) -> None:
    cmd = helper(tmp_path, "sys.stderr.write('vault sealed'); sys.exit(1)")
    with pytest.raises(AuthError, match="vault sealed") as excinfo:
        await provider.token("p", cmd)
    assert excinfo.value.needs_action is False


async def test_exit_two_means_human_action(provider, tmp_path) -> None:
    cmd = helper(tmp_path, "sys.stderr.write('run login'); sys.exit(2)")
    with pytest.raises(AuthError, match="re-authenticate") as excinfo:
        await provider.token("p", cmd)
    assert excinfo.value.needs_action is True


async def test_nonzero_exit_beats_a_well_formed_token(provider, tmp_path) -> None:
    """The exit code decides, however good stdout looks (§5.3)."""
    cmd = helper(tmp_path, JSON_TOKEN + "\nsys.exit(1)")
    with pytest.raises(AuthError, match="exited 1"):
        await provider.token("p", cmd)


async def test_needs_human_payload_carries_message_and_action(
    provider, tmp_path
) -> None:
    cmd = helper(
        tmp_path,
        "print(json.dumps({'error': {'code': 'needs_human',"
        " 'message': 'refresh token revoked', 'action': 'run acme-token login'}}))",
    )
    with pytest.raises(AuthError, match="revoked: run acme-token login") as excinfo:
        await provider.token("p", cmd)
    assert excinfo.value.needs_action is True


async def test_transient_payload_is_not_a_human_problem(provider, tmp_path) -> None:
    cmd = helper(
        tmp_path,
        "print(json.dumps({'error': {'code': 'transient', 'message': 'idp 503'}}))",
    )
    with pytest.raises(AuthError, match="transient: idp 503") as excinfo:
        await provider.token("p", cmd)
    assert excinfo.value.needs_action is False


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("pass", "nothing on stdout"),
        ("print('tok\\x01en')", "control character"),
        ("print(json.dumps({'expires_at': 1}))", "neither"),
        (
            "print(json.dumps({'access_token': 'k', 'username': 'u',"
            " 'error': {'code': 'transient'}}))",
            "neither",
        ),
        ("print(json.dumps({'error': {'code': 'kaput'}}))", "unrecognized"),
        ("print(json.dumps({'error': 'kaput'}))", "not an object"),
        ("print(json.dumps({'access_token': '', 'username': 'u'}))", "access_token"),
        ("print(json.dumps({'access_token': 'k'}))", "username"),
        (
            "print(json.dumps({'access_token': 'k', 'username': 'u',"
            " 'expires_at': 'soon'}))",
            "expires_at",
        ),
        (
            "print(json.dumps({'access_token': 'k', 'username': 'u',"
            " 'token_type': 'MAC'}))",
            "token_type",
        ),
        (
            "print(json.dumps({'access_token': 'k', 'username': 'u',"
            " 'header': 'X Api Key'}))",
            "header",
        ),
    ],
)
async def test_malformed_output_is_rejected(provider, tmp_path, body, reason) -> None:
    with pytest.raises(AuthError, match=reason):
        await provider.token("p", helper(tmp_path, body))


async def test_a_token_for_another_identity_is_rejected(provider, tmp_path) -> None:
    """A helper that mints for someone else did not answer this call (§4.5)."""
    cmd = helper(
        tmp_path,
        "print(json.dumps({'access_token': 'k', 'username': 'bob'}))",
    )
    with pytest.raises(AuthError, match="minted for 'bob', not the requested 'alice'"):
        await provider.token("p", cmd, "alice")


async def test_oversized_stdout_is_rejected(provider, tmp_path) -> None:
    """A bounded read, so a runaway helper cannot be buffered whole (§6.5)."""
    cmd = helper(tmp_path, f"print('x' * {MAX_STDOUT_BYTES + 1})")
    with pytest.raises(AuthError, match="KiB on stdout"):
        await provider.token("p", cmd)


async def test_chatty_stderr_does_not_block_a_successful_helper(
    provider, tmp_path
) -> None:
    """stderr is drained, not merely sampled: a full pipe would deadlock."""
    cmd = helper(tmp_path, "sys.stderr.write('noise\\n' * 40000)\nprint('raw')")
    assert (await provider.token("p", cmd)).token == "raw"


async def test_timeout_kills_the_helper(provider, tmp_path) -> None:
    cmd = helper(tmp_path, "time.sleep(30)").model_copy(update={"timeout": 0.5})
    started = time.time()
    with pytest.raises(AuthError, match="timed out") as excinfo:
        await provider.token("p", cmd)
    assert time.time() - started < 10
    # A kill is transient, never needs_action: only the helper knows that, and
    # it never got to say so (§5.2).
    assert excinfo.value.needs_action is False


async def test_missing_command_is_reported(provider) -> None:
    with pytest.raises(AuthError, match="not found on PATH"):
        await provider.token("p", TokenHelperConfig(command="not-a-real-binary-xyz"))


async def test_purge_removes_cached_tokens(tmp_path) -> None:
    cmd = helper(tmp_path, JSON_TOKEN)
    root = tmp_path / "cache"
    provider = TokenProvider(root)
    await provider.token("p", cmd)
    assert provider.purge() == 1
    await TokenProvider(root).token("p", cmd)
    assert runs(tmp_path) == 2
