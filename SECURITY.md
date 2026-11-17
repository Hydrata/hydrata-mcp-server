# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately:

- GitHub: **Security > Report a vulnerability** on this repository (private vulnerability reporting), or
- email [david.kennewell@hydrata.com](mailto:david.kennewell@hydrata.com).

Please do not open a public issue or pull request for a suspected vulnerability, and do not include working exploit detail in a public place.

## Scope

This repository is the MCP server in front of the Hydrata REST API (`https://hydrata.com/mcp/`). It is an OAuth resource server: it accepts only a Hydrata agent key or a connect-flow token issued for this MCP resource, checks its shape, and sends it with each call it makes to the REST API, where Hydrata (Django) verifies it on every request and applies the account's project permissions, the grant's level and spend limit, and the person-only acts (input review, consent, publishing). A weakness in how Hydrata verifies credentials or authorises requests is also in scope: report it the same way and we will route it.

What the server does and does not do, with a citation to code or a test for each statement, is in [docs/security-model.md](docs/security-model.md).

## Supported versions

Only the latest release and `main` receive fixes.
