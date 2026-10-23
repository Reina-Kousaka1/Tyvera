# Security policy

## Reporting a vulnerability

Please do not report an exploitable vulnerability in a public issue or pull
request. Do not include tokens, credentials, personal data, or the contents of
`.tyvera.secrets` in any report.

If GitHub's **Report a vulnerability** option is available in this repository's
Security tab, use its private reporting flow. Otherwise, contact the repository
maintainer through a private contact method available on the maintainer's
GitHub profile. No security email address is maintained by this policy. If a
secret may have been exposed, revoke or rotate it immediately; removing it in
a later commit does not erase it from repository history.

Include only the information needed to reproduce and assess the issue. Share
exploit details and proof-of-concept material privately with maintainers, not
in public issue templates. The repository's
[Code of Conduct](CODE_OF_CONDUCT.md) covers non-security conduct reports.

## Secret handling in contributions

`.tyvera.secrets` is ignored and must never be committed. CI checks that it is
not tracked and that `.gitignore` continues to ignore it. The placeholder-only
`.tyvera.secrets.example` file is allowed. Tests and examples must not require
real tokens or external production services.
