"""S0.4 lint rule: no source line logs/prints a secret-bearing value (TECH_SPEC §9).

Static guard (the "lint rule" half of "lint rule + log redaction"). Scans app/ for
print()/logging calls that reference plaintext/secret/ciphertext on the same statement.
Runs in CI alongside the runtime redactor in app.secrets.vault.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
# A logging or print call that also names a secret-bearing identifier.
# Best-effort tripwire (the runtime redactor in app.secrets.vault is the real defence);
# single-line by design — multiline/AST analysis is overkill for a single-owner tool.
SENSITIVE = r"plaintext|secret|ciphertext|token|password|api_key|vault_key"
LEAK = re.compile(rf"(print|log(?:ger)?\.\w+|logging\.\w+)\s*\([^)]*\b({SENSITIVE})\b")


def test_no_plaintext_logging():
    offenders = []
    for path in APP.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if LEAK.search(line):
                offenders.append(f"{path.relative_to(APP)}:{n}: {line.strip()}")
    assert not offenders, "secret-bearing values must never be logged:\n" + "\n".join(offenders)
