# Shunda Finance

Shunda Finance is a public-source Django application with a DSM deployment path built from prebuilt GHCR images.

## Release Images

- Web image: `ghcr.io/s450586793/shunda-finance-web`
- Updater image: `ghcr.io/s450586793/shunda-finance-updater`
- Canonical release tags use `vX.Y.Z` only.
- Every release tag publishes immutable `web:vX.Y.Z` and `updater:vX.Y.Z`.
- After both immutable images are published successfully, the workflow promotes only `ghcr.io/s450586793/shunda-finance-web:stable`.
- The updater never receives `stable`, `latest`, or any other mutable tag. DSM operators must pin `SHUNDA_UPDATER_IMAGE_TAG=vX.Y.Z` for updater upgrades.

## Public Source Rules

- Never commit or publish `.env`, production data, attachments, backups, Token values, Cookie values, private keys, account passwords, or DSM credentials.
- Release automation uses the standard GitHub `GITHUB_TOKEN` package permissions only.
- Production runtime values stay on the deployment host and are injected during deployment, not stored in this repository or image metadata.

## DSM Deployment

- DSM deployments pull prebuilt images only; they do not rebuild the project on the NAS.
- Web promotion is handled through the `stable` tag after a full immutable release succeeds.
- Updater rollout is manual and version-pinned through immutable tags so that Web and updater lifecycles stay explicit.
