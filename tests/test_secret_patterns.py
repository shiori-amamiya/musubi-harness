"""SECRET_RE refuses captures carrying real credential formats, and nothing else.

A match refuses the whole turn (``TurnEnvelope.validate``), so a false positive
costs a real capture: precision is tested as hard as recall. Samples are
assembled at runtime so no credential-shaped literal lives in this file (this
repo has secret-scanning push protection, and fixtures should not train anyone
to bypass it).
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from musubi_harness import TurnEnvelope
from musubi_harness.core import SECRET_RE, ContractError

ALNUM = string.ascii_letters + string.digits


def body(n: int, alphabet: str = ALNUM) -> str:
    return (alphabet * (n // len(alphabet) + 1))[:n]


def join(*parts: str) -> str:
    return "".join(parts)


# (name, sample, caught by the pre-1.1.0 pattern)
FORMATS = [
    ("GitHub classic PAT", join("gh", "p_", body(36)), True),
    ("GitHub fine-grained PAT", join("github", "_pat_", body(22), "_", body(59)), True),
    ("GitHub OAuth token", join("gh", "o_", body(36)), False),
    ("GitHub user-to-server", join("gh", "u_", body(36)), False),
    ("GitHub server-to-server", join("gh", "s_", body(36)), False),
    ("GitHub refresh token", join("gh", "r_", body(76)), False),
    ("OpenAI project key", join("sk", "-proj-", body(48)), False),
    ("OpenAI legacy key", join("sk", "-", body(48)), False),
    ("Anthropic API key", join("sk", "-ant-api03-", body(93, ALNUM + "-_")), False),
    ("Stripe secret key", join("sk", "_live_", body(24)), True),
    ("AWS access key id", join("AK", "IA", body(16, string.ascii_uppercase + string.digits)), False),
    ("AWS temporary key id", join("AS", "IA", body(16, string.ascii_uppercase + string.digits)), False),
    ("Slack bot token", join("xo", "xb-", "123456789012-1234567890123-", body(24)), False),
    ("Google API key", join("AI", "za", body(35, ALNUM + "-_")), False),
    ("JWT (signed)", join("ey", "J", body(20), ".", "ey", "J", body(30), ".", body(43)), False),
    ("JWT (unsigned)", join("ey", "J", body(20), ".", "ey", "J", body(30), "."), False),
    ("Bearer header", join("Authorization: Bear", "er ", body(32)), True),
    ("Bearer, letters only", join("Authorization: Bear", "er ", "AbCdEfGhIjKlMnOpQrStUvWx"), True),
    # Mixed case joined by - or _ is a credential shape, not an identifier.
    ("Bearer, mixed case with hyphen", join("Bear", "er ", "AbCdEfGhIjKl-MnOpQrStUvWxYz"), True),
    ("Bearer, mixed case with underscore", join("Bear", "er ", "Abcd_Efgh_Ijkl_Mnop"), True),
    ("PEM RSA key", join("-----BEGIN RSA PRIV", "ATE KEY-----"), True),
    ("PEM PKCS#8 key", join("-----BEGIN PRIV", "ATE KEY-----"), True),
    ("PEM encrypted key", join("-----BEGIN ENCRYPTED PRIV", "ATE KEY-----"), False),
    ("PGP private key block", join("-----BEGIN PGP PRIV", "ATE KEY BLOCK-----"), False),
]

# Text that must NOT refuse a turn: near misses of every pattern above, plus
# the everyday identifiers a coding session is full of.
NEAR_MISSES = [
    "we use sk-learn and scikit-learn for the baseline",
    "the task-management board and a risk-assessment doc",
    "gho_st of a variable name, ghost_mode, ghs_tats",
    "AKIA is also a name; akia and Akia appear in prose",
    "AWS region us-east-1, account 123456789012, arn:aws:iam::123456789012:role/x",
    "xoxo, see you tomorrow",
    "AIza by itself, or AIzaShort",
    "eyJ appears in base64 of '{\"' and eyJhbGciOiJIUzI1NiJ9 alone is a header",
    "commit 71be14f015cbe1aaa33d58657352aac642bb29e6 and uuid 3f2b8c1e-4d5a-4b6c-9e7f-0a1b2c3d4e5f",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "-----BEGIN PUBLIC KEY----- and -----BEGIN CERTIFICATE-----",
    "the bearer of bad news; Bearer tokens are described in RFC 6750",
    # Previously refused (false positives of the old Bearer rule, found in the
    # musubi and comfyui-immich repos): identifiers, not credentials.
    "authorization: type: Bear" + "er credentials_file: /etc/token",
    "send Authorization: Bear" + "er not-a-real-token and expect a 401",
    "op://Harem World/github-shiori-classic/credential is a reference, not a secret",
]

OLD_SECRET_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
    re.IGNORECASE,
)


@pytest.mark.parametrize(("name", "sample", "was_caught"), FORMATS, ids=[f[0] for f in FORMATS])
def test_every_listed_credential_format_is_caught(name: str, sample: str, was_caught: bool) -> None:
    assert SECRET_RE.search(f"here it is: {sample} (please keep it)"), name
    # The table in the PR comes from this column; keep it honest.
    assert bool(OLD_SECRET_RE.search(sample)) is was_caught, name


@pytest.mark.parametrize("text", NEAR_MISSES)
def test_near_misses_do_not_refuse_a_turn(text: str) -> None:
    assert SECRET_RE.search(text) is None, SECRET_RE.search(text)


def test_a_matching_turn_is_refused_before_it_is_stored() -> None:
    # from_mapping validates, so the refusal happens at construction.
    with pytest.raises(ContractError, match="secret-like"):
        TurnEnvelope.from_mapping(
            {
                "event_id": "e1",
                "actor": "alice",
                "presence": "alice/laptop",
                "plane": "episodic",
                "context": "primary",
                "source": "claude-code",
                "zone": "home",
                "user_text": "my key is " + join("sk", "-ant-api03-", body(93, ALNUM + "-_")),
                "assistant_text": "noted",
                "captured_at": "2026-09-26T20:00:00Z",
                "metadata": {},
            }
        )


def test_this_repository_text_has_no_false_positives() -> None:
    """Precision on real, non-private text: every source, test and doc file here."""
    root = Path(__file__).resolve().parents[1]
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt"}
        and not any(
            part.startswith(".") or part in {"dist", "build"} or part.endswith(".egg-info") for part in p.relative_to(root).parts[:-1]
        )
        and p.name != Path(__file__).name
    ]
    assert len(files) > 10
    hits = [(str(p.relative_to(root)), m.group(0)[:12]) for p in files for m in SECRET_RE.finditer(p.read_text(errors="replace"))]
    assert hits == []
