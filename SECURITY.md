# Security Policy

## Reporting a Vulnerability

Please do **not** report security vulnerabilities in public issues.

Instead, open a private security advisory in GitHub for this repository.

When reporting, include:

- A clear description of the issue
- Reproduction steps
- Expected impact
- Any suggested mitigation

## Supported Versions

At this stage, only the latest release is considered supported for security fixes.

## Homebrew binary trust model

This app executes the local `brew` binary on the user's machine.

- Prefer Homebrew installed from official instructions: <https://brew.sh>
- If using a custom brew path in app settings, ensure it points to a trusted executable
- The app validates executability and basic command response, but supply-chain trust remains the user's responsibility