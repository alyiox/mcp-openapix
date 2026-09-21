# Token Helper Protocol v1

A helper is any executable. This document specifies the **input** a helper receives and
the **output** it must produce.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as in
RFC 2119 and RFC 8174.

---

## 1. Declaration

A helper is declared as a stdio command:

```json
{
  "command": "acme-token",
  "args": ["--login-url", "https://id.example.com/login"],
  "timeout": 30
}
```

| Field | Required | Default | Constraint |
|---|---|---|---|
| `command` | yes | — | Non-empty. Resolved on `PATH` |
| `args` | no | `[]` | Passed verbatim |
| `timeout` | no | `60` | Seconds. Number, `> 0` and `<= 300`; fractional values allowed |

1.1. The declaration MUST NOT carry credentials. A helper MUST source its own — from
the environment it inherits, a keyring, or its own state.

1.2. A helper that mints differently scoped tokens MUST be declared once per scope.
Argument sets, not runtime state, distinguish one declaration from another. Identity is
resolved per call (§3.2) and never belongs in a declaration.

## 2. Invocation

Process semantics in this section are POSIX. The caller MUST run the helper:

2.1. with **stdin closed** (`DEVNULL`) and **stdout and stderr piped**.

2.2. **without a shell**.

2.3. in **its own process group**, terminated when `timeout` elapses: `SIGTERM`, then
`SIGKILL` after a 2 second grace. Helpers that spawn children (`uvx`) MUST tolerate
group termination.

2.4. with the environment defined in §3.

A helper MUST write nothing but its outcome payload to stdout, and MUST exit within
`timeout`.

## 3. Environment

3.1. The helper inherits the caller's environment with every `TOKEN_HELPER_*` name
removed, plus the variable in §3.2.

3.2. `TOKEN_HELPER_USERNAME` is set when the call resolved a username, and is absent
otherwise. A helper that authenticates per user MUST mint for that identity when set,
and MUST tolerate its absence, falling back to the default identity in its own
configuration or state. A helper that does not authenticate per user MUST ignore it.
Either way, the identity minted for is reported in `username` (§4.5).

3.3. The `TOKEN_HELPER_*` namespace is reserved for variables the caller sets. A
helper MUST NOT export names in that space to its own children.

3.4. Every configured helper sees the same environment. A helper requiring isolation
SHOULD read its own credential store rather than an exported variable.

## 4. Outcome — exit `0`

Exit `0` means the helper ran to completion; stdout carries what it concluded, a token
or an error. Failures that prevent it running are §5.

4.1. The caller MUST discriminate by stripping leading and trailing ASCII whitespace
from stdout, including the trailing newline, and parsing the result as JSON:

- Empty after stripping — **malformed**.
- Parses as a JSON **object** — structured. It MUST contain exactly one of
  `access_token` or `error`; an object with neither, or both, is **malformed**.
- Anything else, including a parse failure — the stripped stdout **is** the token.

A JWT is not valid JSON and a bare number is not an object, so the forms do not collide.

4.2. A token payload:

```json
{
  "access_token": "eyJhbGciOi...",
  "expires_at": 1758400000,
  "token_type": "Bearer",
  "header": "Authorization",
  "username": "ada@example.com"
}
```

| Field | Required | Default | Constraint |
|---|---|---|---|
| `access_token` | yes | — | Non-empty string, no control characters |
| `expires_at` | no | — | Number. **Absolute** Unix seconds. Advisory (§4.9) |
| `token_type` | no | `"Bearer"` | `"Bearer"`, or empty to send the raw token with no scheme |
| `header` | no | `"Authorization"` | RFC 7230 token |
| `username` | yes | — | Non-empty string (§4.5) |

4.3. The caller MUST send `{header}: {token_type} {access_token}`, collapsing the
separating space when `token_type` is empty, and replacing any header of that name
already on the request.

4.4. `expires_at` MUST be absolute, not a lifetime: the helper may itself have spent
seconds obtaining the token, and only the helper knows when the token was minted.

4.5. `username` is the identity the token was actually minted for, and a token payload
MUST carry it. A helper always knows that identity: `TOKEN_HELPER_USERNAME` when it was
set, and the default resolved from its own configuration or state when it was not — a
helper with no default cannot mint, and says so (§4.7). The caller MUST treat a
`username` differing from a `TOKEN_HELPER_USERNAME` it set as an error, and otherwise
learns from the field which identity it was given. The bare-token form (§4.1) carries
no fields, and so reports no identity.

4.6. An error payload:

```json
{
  "error": {
    "code": "needs_human",
    "message": "refresh token revoked",
    "action": "run `acme-token login`"
  }
}
```

| Field | Required | Default | Constraint |
|---|---|---|---|
| `code` | yes | — | `"needs_human"` or `"transient"`. Any other value is malformed |
| `message` | no | — | String. What went wrong |
| `action` | no | — | String. What a human must do |

4.7. `needs_human` and `transient` mean exactly what exit `2` and exit `1` mean in §5.
Both forms remain valid; the structured one adds `message` and `action`. A helper MUST
use `needs_human`, and only `needs_human`, when no retry can succeed until a human acts
out of band.

4.8. The caller MUST ignore unknown keys. This is the forward-compatibility mechanism,
and why the protocol carries no version field: only additions are permitted, and
changing what an existing key means is an unversioned break. `expires_in`,
`refresh_token` and `scope` are unknown keys — a helper that holds a refresh token
refreshes on its own.

4.9. The caller MAY reuse a token across calls, never across declarations and never
across identities. A `401` from the upstream API retires it, whatever `expires_at`
claimed. `expires_at` is therefore advisory: it lets the caller re-mint before a call
fails. How the caller caches and retries is outside this protocol.

4.10. Malformed output is an error: empty stdout, an object carrying neither or both
discriminants, a non-string or empty `access_token`, a non-numeric `expires_at`, a
`token_type` that is neither `"Bearer"` nor empty, a `header` that is not an RFC 7230
token, a control character anywhere in the output, an unrecognized `error.code`, a
missing, non-string or empty `username`, or a `username` contradicting
`TOKEN_HELPER_USERNAME`. It is surfaced to the caller and MUST NOT be retried; a second
run produces the same bytes.

## 5. Failure — non-zero exit

| Code | Meaning |
|---|---|
| `1` | Transient or unspecified failure |
| `2` | Human action required — re-login, expired MFA, a revoked grant |
| other | Treated as `1` |

5.1. A helper MUST exit `2`, and only `2`, when no retry can succeed until a human acts
out of band. The caller surfaces that distinction; only the helper knows which occurred.

5.2. A helper killed by `timeout` or by any signal is a transient failure, never `2`.

5.3. A non-zero exit takes precedence over stdout. The caller MUST NOT parse stdout for
an outcome in that case, however well-formed it looks.

5.4. Interactive login MUST NOT be attempted; a helper has no interactive channel to
the user. `needs_human` — as exit `2` or as an error payload — exists to say so.

## 6. Output handling

6.1. stderr is always captured.

6.2. On non-zero exit stderr is logged and attached to the surfaced error, truncated to
4096 bytes.

6.3. On exit `0` stderr MUST be discarded and MUST NOT be logged at any level.

6.4. Raw stdout MUST NOT be logged at any level, on any exit. Only `code`, `message`,
`action` and `username`, parsed out of a well-formed payload, may be surfaced.

6.5. The caller MUST read at most 64 KiB from stdout. More than that is malformed.

## 7. Examples

```json
{ "command": "gcloud", "args": ["auth", "print-access-token"] }

{ "command": "acme-token", "args": ["--login-url", "https://id.example.com/login"] }

{ "command": "uvx", "args": ["vendor-token-helper"], "timeout": 90 }
```

The first exercises the bare-token path: `gcloud` prints a raw token and nothing else,
and works unmodified. The second is declared per login endpoint (§1.2) and reports
`expires_at` and `needs_human`. The third spawns children, so it must survive group
termination (§2.3), and its longer `timeout` covers a cold dependency install.
