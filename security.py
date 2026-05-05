"""
Security layer — startup validation, secrets scrubbing, and draft integrity.

Call security.startup_check() as the first thing in scheduler.main().
All other functions are available for use throughout the codebase.
"""

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Patterns that look like secrets — redact from any logged text
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(AKIA[0-9A-Z]{16})"), "[AWS_KEY_REDACTED]"),
    (re.compile(r"(?i)([0-9a-z]{32,40})(?=.*secret|.*token)", re.IGNORECASE), "[TOKEN_REDACTED]"),
    (re.compile(r"(?i)(sk-[a-zA-Z0-9]{32,})"), "[API_KEY_REDACTED]"),
    (re.compile(r"(?i)(AC[a-f0-9]{32})"), "[TWILIO_SID_REDACTED]"),
    (re.compile(r"(?i)(EAA[a-zA-Z0-9]+)"), "[FB_TOKEN_REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)(Bearer\s+[A-Za-z0-9\-._~+/]+=*)"), "Bearer [TOKEN_REDACTED]"),
    # AWS secret keys (40 char base64-ish)
    (re.compile(r"(?<![A-Z0-9])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"), "[SECRET_REDACTED]"),
]

REQUIRED_VARS = {
    "CRITICAL": [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "BEDROCK_MODEL_ID",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL",
    ],
    "OPTIONAL": [
        "TWITTER_BEARER_TOKEN",
        "TWITTER_API_KEY",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_SECRET",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_TO_NUMBER",
        "SMS_KILL_SWITCH",
        "SMS_RATE_LIMIT_PER_HOUR",
        "SMS_MIN_PRIORITY",
        "APPROVAL_MODE",
        "SLACK_WEBHOOK_URL",
    ],
}

VALID_APPROVAL_MODES = {"file", "slack", "email"}

_INTEGRITY_STORE = Path(__file__).parent / ".draft_integrity.json"


def scrub(text: str) -> str:
    """Redact known secret patterns from a string before logging."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def validate_environment() -> tuple[bool, list[str]]:
    """
    Check all required and optional env vars.
    Returns (all_critical_present, list_of_warnings).
    """
    warnings = []
    missing_critical = []

    for var in REQUIRED_VARS["CRITICAL"]:
        val = os.getenv(var)
        if not val or val.startswith("{{"):
            missing_critical.append(var)

    for var in REQUIRED_VARS["OPTIONAL"]:
        val = os.getenv(var)
        if not val or val.startswith("{{"):
            warnings.append(f"{var} not set — related features will be disabled")

    approval_mode = os.getenv("APPROVAL_MODE", "file").lower()
    if approval_mode not in VALID_APPROVAL_MODES:
        warnings.append(
            f"APPROVAL_MODE='{approval_mode}' is invalid — must be one of {VALID_APPROVAL_MODES}. Defaulting to 'file'."
        )

    if os.getenv("ALPACA_BASE_URL", "").strip("/").endswith("api.alpaca.markets") and \
       "paper" not in os.getenv("ALPACA_BASE_URL", ""):
        warnings.append(
            "ALPACA_BASE_URL points to LIVE trading endpoint. Ensure this is intentional."
        )

    return len(missing_critical) == 0, missing_critical + warnings


def check_dotenv_not_committed() -> bool:
    """Warn if .env file exists and .gitignore doesn't exclude it."""
    env_path = Path(__file__).parent / ".env"
    gitignore_path = Path(__file__).parent / ".gitignore"

    if not env_path.exists():
        return True  # No .env = no risk

    if not gitignore_path.exists():
        logger.warning("SECURITY: .env file found but no .gitignore — credentials may be committed")
        return False

    gitignore_content = gitignore_path.read_text()
    if ".env" not in gitignore_content:
        logger.warning("SECURITY: .env is not in .gitignore — credentials may be committed to git")
        return False

    return True


def check_log_dir_permissions() -> bool:
    """Ensure the logs directory isn't world-readable."""
    logs_dir = Path(__file__).parent / "logs"
    if not logs_dir.exists():
        return True
    try:
        mode = logs_dir.stat().st_mode
        world_readable = mode & 0o004
        if world_readable:
            logger.warning("SECURITY: logs/ directory is world-readable — contains decision logs with portfolio data")
            return False
        return True
    except OSError:
        return True


def seal_draft(draft_path: Path) -> str:
    """
    Compute and store a SHA-256 integrity seal for a draft file.
    Returns the hex digest.
    """
    content = draft_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()

    store = {}
    if _INTEGRITY_STORE.exists():
        try:
            with open(_INTEGRITY_STORE) as f:
                store = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    store[str(draft_path.name)] = {
        "sha256": digest,
        "sealed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }

    with open(_INTEGRITY_STORE, "w") as f:
        json.dump(store, f, indent=2)

    return digest


def verify_draft(draft_path: Path) -> tuple[bool, str]:
    """
    Verify a draft file against its integrity seal.
    Returns (valid, reason).
    Call before posting to prevent posting a hand-tampered file.
    """
    if not _INTEGRITY_STORE.exists():
        return False, "no integrity store found — draft was never sealed"

    try:
        with open(_INTEGRITY_STORE) as f:
            store = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"could not read integrity store: {e}"

    name = draft_path.name
    if name not in store:
        return False, f"no seal on record for {name}"

    recorded_digest = store[name]["sha256"]
    current_digest = hashlib.sha256(draft_path.read_bytes()).hexdigest()

    if current_digest != recorded_digest:
        return False, f"INTEGRITY FAILURE: {name} has been modified since sealing (expected {recorded_digest[:12]}..., got {current_digest[:12]}...)"

    return True, f"integrity verified (sha256={recorded_digest[:12]}...)"


def validate_draft_content(draft: dict) -> tuple[bool, list[str]]:
    """
    Validate a draft's content before it can be approved for posting.
    Returns (valid, list_of_issues).
    """
    issues = []

    required_keys = ["status", "created_at", "content", "event"]
    for key in required_keys:
        if key not in draft:
            issues.append(f"missing required key: {key}")

    content = draft.get("content", {})
    if not content.get("content"):
        issues.append("content.content is empty")

    content_type = content.get("content_type", "")
    if content_type not in ("tweet", "thread", "internal"):
        issues.append(f"invalid content_type: {content_type}")

    tweet_text = content.get("content", "")
    if content_type == "tweet" and len(tweet_text) > 280:
        issues.append(f"tweet exceeds 280 chars ({len(tweet_text)} chars)")

    status = draft.get("status", "")
    if status not in ("pending", "approved", "rejected", "posted"):
        issues.append(f"invalid status: {status}")

    if any(pattern in tweet_text.lower() for pattern in ["<script", "javascript:", "onclick"]):
        issues.append("SECURITY: content contains suspicious HTML/JS patterns")

    return len(issues) == 0, issues


def startup_check(halt_on_critical: bool = True) -> bool:
    """
    Run all security checks on startup.
    Returns True if system is safe to run, False otherwise.
    If halt_on_critical=True, calls sys.exit(1) on critical failures.
    """
    print("\n[SECURITY CHECK]")
    all_ok = True

    ok, issues = validate_environment()
    if not ok:
        critical_missing = [i for i in issues if not i.endswith("disabled")]
        logger.error("Missing critical env vars: %s", critical_missing)
        print(f"  [FAIL] Missing critical credentials: {critical_missing}")
        all_ok = False
    else:
        print("  [PASS] All critical env vars present")

    warnings = [i for i in issues if i not in (issues[:len(issues) - len(issues)])]
    opt_warnings = [i for i in issues if "disabled" in i or "not set" in i or "invalid" in i or "LIVE" in i]
    for w in opt_warnings:
        print(f"  [WARN] {w}")
        logger.warning("startup: %s", w)

    if not check_dotenv_not_committed():
        print("  [WARN] .env file may not be protected from git commits")
    else:
        print("  [PASS] .env gitignore protection OK")

    check_log_dir_permissions()

    logs_in_gitignore = False
    gitignore_path = Path(__file__).parent / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        logs_in_gitignore = "logs/" in content or "logs" in content
    if not logs_in_gitignore:
        print("  [WARN] logs/ may not be excluded from git — contains portfolio data")
    else:
        print("  [PASS] logs/ excluded from git")

    if all_ok:
        print("  Security check passed.\n")
    else:
        print("  Security check FAILED.\n")
        if halt_on_critical:
            logger.critical("Halting startup due to security check failure")
            sys.exit(1)

    return all_ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    startup_check(halt_on_critical=False)
