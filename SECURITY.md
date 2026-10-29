# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately:

- GitHub: **Security > Report a vulnerability** on this repository (private vulnerability reporting), or
- email [david.kennewell@hydrata.com](mailto:david.kennewell@hydrata.com).

Please do not open a public issue or pull request for a suspected vulnerability, and do not include working exploit detail in a public place.

## Scope

This repository is the MCP server in front of the Hydrata REST API (`https://hydrata.com/mcp/`). It checks the shape of the caller's credential and forwards it; the credential is verified, and the account's project permissions are applied, by Hydrata (Django). A weakness in how Hydrata verifies credentials or authorises requests is also in scope: report it the same way and we will route it.

What the server does and does not do, with a citation to code or a test for each statement, is in [docs/security-model.md](docs/security-model.md).

## Supported versions

Only the latest release and `main` receive fixes.
