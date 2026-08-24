# Security

Ocura OSS runs trusted local commands directly on your machine. It does not sandbox commands, restrict network access, or isolate child processes.

## Reporting a vulnerability

Please do not open a public issue with exploitable details. Use [GitHub private vulnerability reporting](https://github.com/BlakeMasters/ocura-oss/security/advisories/new) when it is available.

If private reporting is unavailable, open a minimal issue asking for a private contact channel. Do not include the vulnerability details in that issue.

A useful report includes the affected version, operating system, Python version, expected security boundary, observed behavior, and a minimal reproduction that contains no credentials or private data.

## Scope

Reports about record or log path escapes, verification bypasses, command-summary disclosure, unsafe process handling, or package integrity are in scope.

Direct command execution, inherited network access, and the absence of hostile-code containment are documented boundaries rather than sandbox vulnerabilities.
