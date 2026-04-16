# Security Policy

## Supported scope

AetherCloud is a self-hosted personal cloud workspace. Security fixes are relevant for:

- authentication and session handling
- file upload and download logic
- path handling for local storage
- deployment defaults and public exposure guidance

## Reporting

If you find a security issue, do not open a public issue with exploit details first.

Report it privately to the repository owner with:

- affected version or commit
- exact reproduction steps
- impact summary
- suggested mitigation if available

## Deployment guidance

- Always change `AETHER_SECRET_KEY` before public exposure.
- Put the app behind HTTPS when accessed outside the local network.
- Use a dedicated storage directory instead of the repo root in production.
- Run the production stack with `gunicorn` or Docker, not Flask debug mode.
