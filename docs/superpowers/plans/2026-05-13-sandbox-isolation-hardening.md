# Sandbox Isolation Hardening

## Context

The latest relevant thread in the database was `t-10ac4416b619` (`Quick check`). In that thread, the assistant was able to infer host details from inside the Docker sandbox:

- The host project path leaked through `/proc/self/mountinfo` because `/workspace` was a bind mount from `data/sandboxes/<assistant_id>/workspace`.
- Tailscale DNS details leaked through `/etc/resolv.conf` because Docker copied host resolver configuration into the container.
- Generic host/kernel details remained visible through normal container interfaces such as `/proc/cpuinfo`, `/proc/meminfo`, and kernel metadata.

The product requirement is that the agent must still be able to install its own tools. That rules out the strictest sandbox hardening options for now, including a read-only root filesystem, disabled network, and non-root execution if we want `apt install` to keep working.

## Plan

1. Keep tool installation working.
   - Keep the container root filesystem writable.
   - Keep the container running as root for now.
   - Keep outbound network access enabled.

2. Remove the host workspace bind mount.
   - Use a Docker named volume for `/workspace` instead of a host bind mount.
   - This keeps per-assistant workspace persistence without exposing `/home/larry/...` through mount metadata.

3. Override sandbox DNS.
   - Set explicit public DNS servers on sandbox containers.
   - Set the DNS search path to `.` and clear DNS options so `/etc/resolv.conf` does not expose tailnet search domains or Tailscale DNS config.

4. Recreate stale containers automatically.
   - If an existing long-lived sandbox container still has the old bind mount or inherited DNS settings, remove and recreate it with the hardened settings.
   - Before replacing a stale container, export its current `/workspace` contents and restore them into the new named volume.

5. Drop dead Docker sandbox code.
   - Production uses `DockerWorkspaceProvider`, not the older `DockerSandbox` adapter.
   - Remove the old adapter and replace its integration tests with tests for the active provider path.

## Non-Goals

- Do not disable package installation.
- Do not disable network access.
- Do not try to fully hide generic host/kernel facts exposed by standard container namespaces.
- Do not switch runtimes to rootless Docker, gVisor, Firecracker, E2B, or Daytona in this patch.

## Remaining Risk

This reduces app-specific leakage and Tailscale DNS leakage, but it is still a Docker container with network access and root inside the container. Stronger isolation would need a runtime-level change or a more restrictive install workflow.
