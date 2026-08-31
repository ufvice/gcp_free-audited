# Security model

This repository is a public, history-preserving duplicate of
`fatekey/gcp_free` audited from upstream commit
`f09a7316510494c59852a638a6a85af1e3fddc99`.

## Trust boundary

- Runtime root scripts come only from the checked-out audited repository and are
  streamed over SSH with an integrity check. They never follow an upstream branch.
- dae `v1.0.0`, its three supported x86_64 artifacts, and GeoIP data are pinned by
  version/commit and repository-reviewed SHA-256 values.
- Python packages are installed from `requirements.lock` with `--require-hashes`.
- A Git checkout is still executable code. Only run a reviewed commit or annotated
  audited tag; local modifications intentionally change what will run.

## Update procedure

1. Fetch upstream changes without merging them into the audited branch.
2. Review the complete diff, including every root shell path and downloaded artifact.
3. Recompute hashes independently from official release assets and review provenance.
4. Regenerate `requirements.lock` in a clean environment and inspect dependency changes.
5. Run static checks and tests, then merge as a new reviewed commit. Never replace a
   pinned URL with `master`, `main`, `latest`, a CDN, or a Worker mirror.

## Residual limitations

- SHA-256 verifies pinned bytes but is not a reproducible-build or publisher-signature proof.
- apt packages are supplied by the configured Debian/Ubuntu repositories and are not
  version-pinned here so security updates remain available.
- GCP hierarchical firewall policies or manually created priority-0 rules can override
  project VPC rules; review organization/folder policies separately.
- The menu-provided traffic protection is a guest-local vnStat trigger, not a Cloud
  Billing hard cap. Monitoring and shutdown can lag, and an attacker with root can
  disable it. Its default trigger is 100 GiB of monthly TX on the detected primary
  interface and it powers the VM off. Multi-interface VMs are outside its scope.
- The retained legacy `net_iptables.sh` only limits IPv4 host input. It is not exposed
  by the menu and must not be treated as outbound billing protection.
