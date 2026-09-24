# Security

This repository is a public demonstration and must remain free of credentials and customer data.

## Secrets

Do not commit API keys, access tokens, passwords, client secrets, private keys, SAP destinations, tenant-specific endpoints, or deployment identifiers. Supply `TYPESAFE_API_KEY` / `JEV_API_KEY` only through the runtime's secret or environment configuration.

If a real credential is ever committed or shared, treat it as exposed: revoke or rotate it at the provider and remove it from Git history before publication. Deleting it only from the latest commit is not sufficient.

## Data

The bundled `mcp-mock.json` contains synthetic demonstration data. Do not replace it with customer, vendor, employee, invoice, PO, or authentication data in a public fork.

## Reporting

Do not include secrets or customer data in public issues. Use your organization's approved private security-reporting channel for sensitive findings.
