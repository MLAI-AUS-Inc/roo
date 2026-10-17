"""Host-side lifecycle controller. Its Docker authority never enters Hermes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
from uuid import UUID, uuid4

from .config import WorkerConfig

SANDBOX_BRIDGES = {"roo-hermes-internal": "br-roo-hermes", "roo-hermes-web": "br-roo-web"}


@dataclass(frozen=True)
class TaskMachine:
    name: str
    root: Path
    workspace: Path
    state: Path


class DockerTaskLauncher:
    def __init__(self, config: WorkerConfig, *, executable="docker"):
        self.config = config
        self.executable = executable

    async def _command(self, *args):
        process = await asyncio.create_subprocess_exec(
            self.executable, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Docker lifecycle operation failed")
        return output

    async def preflight(self):
        if os.getuid() != self.config.runtime_uid or os.getgid() != self.config.runtime_gid:
            raise ValueError("Run the controller as the configured dedicated runtime UID/GID")
        if (self.config.network, self.config.web_network) != tuple(SANDBOX_BRIDGES):
            raise ValueError("Sandbox network names must match the installed host firewall policy")
        networks = [self.config.network, self.config.web_network]
        for name in networks:
            network = json.loads(await self._command("network", "inspect", name))
            if len(network) != 1 or network[0].get("Internal") is not True:
                raise ValueError("Hermes requires an internal Docker network")
            if network[0].get("Options", {}).get("com.docker.network.bridge.name") != SANDBOX_BRIDGES[name]:
                raise ValueError("Sandbox bridge does not match the installed host firewall policy")
            for container_id in network[0].get("Containers", {}):
                peer = json.loads(await self._command("container", "inspect", container_id))
                labels = peer[0].get("Config", {}).get("Labels") or {}
                infrastructure = (labels.get("com.docker.compose.project") == "roo-hermes-infrastructure"
                                  and labels.get("com.docker.compose.service") in (
                                      {"hermes-broker", "hermes-web-egress"} if name == self.config.web_network else {"hermes-broker"}))
                own_orphan = (labels.get("au.mlai.hermes.managed") == "true"
                              and labels.get("au.mlai.hermes.worker") == self.config.worker_id)
                if not infrastructure and not own_orphan:
                    raise ValueError("An unrelated container shares the task network")
        boundary = await self._privileged_check("/usr/local/sbin/roo-hermes-network", "check")
        if boundary.get("networks") != SANDBOX_BRIDGES or boundary.get("host_input_drop") is not True:
            raise ValueError("Host network isolation could not be verified")
        await self._command("image", "inspect", self.config.image)
        root = self.config.state_root
        if root.is_symlink():
            raise ValueError("State directory must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve() != root or root.stat().st_uid != os.getuid():
            raise ValueError("State directory must be canonical and owned by the controller")
        if root.stat().st_mode & 0o077:
            raise ValueError("State directory must be private (0700)")
        quota = await self._quota("check")
        if quota.get("task_root") != str(root):
            raise ValueError("Quota helper is configured for a different task root")

    async def _quota(self, *args):
        return await self._privileged_check("/usr/local/sbin/roo-hermes-quota", *args)

    async def _privileged_check(self, executable, *args):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo", "-n", executable, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode or len(output) > 65536:
            raise RuntimeError("Privileged sandbox boundary could not be verified")
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Privileged sandbox boundary could not be verified")
        return result

    async def limit(self, machine):
        if machine.root.parent != self.config.state_root:
            raise ValueError("Foreign task machine")
        result = await self._quota("prepare", machine.root.name)
        if result.get("attempt") != machine.root.name or result.get("task_root") != str(self.config.state_root):
            raise RuntimeError("Task filesystem quota identity mismatch")

    def prepare(self, task_id: str) -> TaskMachine:
        task_id = str(UUID(task_id))
        attempt_id = uuid4().hex
        root = self.config.state_root / f"{task_id}-{attempt_id}"
        root.mkdir(mode=0o700)
        workspace, state = root / "workspace", root / "state"
        try:
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
        except BaseException:
            shutil.rmtree(root)
            raise
        return TaskMachine(f"roo-hermes-{attempt_id}", root, workspace, state)

    def command(self, machine: TaskMachine, *, public_web=False) -> tuple[str, ...]:
        config = self.config
        # All paths are generated by prepare(), never taken from a task payload.
        if machine.root.parent != config.state_root or machine.root.is_symlink():
            raise ValueError("Task machine is outside the controller workspace")
        if public_web and not config.public_web_enabled:
            raise ValueError("Public web execution is disabled on this controller")
        browser_environment = (
            "--env", "AGENT_BROWSER_ENGINE=chrome",
            "--env", "AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage,--proxy-server=http://hermes-web-egress:3128,--proxy-bypass-list=<-loopback>,--disable-features=EncryptedClientHello,--disable-quic",
            "--env", "HTTPS_PROXY=http://hermes-web-egress:3128",
            "--env", "HTTP_PROXY=http://hermes-web-egress:3128",
            "--env", "NO_PROXY=hermes-broker,127.0.0.1,localhost",
            "--env", "no_proxy=hermes-broker,127.0.0.1,localhost",
        ) if public_web else ()
        return (
            self.executable, "run", "--rm", "--interactive", "--init",
            "--log-driver", "none",
            "--name", machine.name, "--label", "au.mlai.hermes.managed=true",
            "--label", f"au.mlai.hermes.worker={config.worker_id}",
            "--network", config.web_network if public_web else config.network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--user", f"{config.runtime_uid}:{config.runtime_gid}",
            "--memory", f"{config.memory_mb}m", "--memory-swap", f"{config.memory_mb}m",
            "--cpus", str(config.cpus), "--pids-limit", str(config.pids_limit),
            "--tmpfs", "/tmp:rw,nosuid,nodev," + ("" if public_web else "noexec,") + "size=268435456,mode=1777",
            "--mount", f"type=bind,src={machine.workspace},dst=/workspace",
            "--mount", f"type=bind,src={machine.state},dst=/state",
            "--mount", f"type=bind,src={machine.state / 'config.yaml'},dst=/state/config.yaml,readonly",
            *browser_environment, config.image,
        )

    async def stop(self, machine: TaskMachine):
        # Removing a missing container is harmless; inspect afterward detects a
        # daemon failure that could leave code running after lease revocation.
        try:
            await self._command("rm", "--force", machine.name)
        except RuntimeError:
            remaining = await self._command("ps", "--all", "--filter", f"name=^/{machine.name}$", "--format", "{{.ID}}")
            if remaining.strip():
                raise RuntimeError("Hermes container could not be stopped")

    async def recover(self, *, allow_pending=False):
        output = await self._command(
            "ps", "--all", "--filter", "label=au.mlai.hermes.managed=true",
            "--filter", f"label=au.mlai.hermes.worker={self.config.worker_id}",
            "--format", "{{.ID}}",
        )
        for container_id in output.decode("ascii").splitlines():
            if container_id:
                await self._command("rm", "--force", container_id)
        # A lost completion acknowledgement needs reconciliation before another
        # task can run. Keep the immutable result and artifact snapshots intact.
        if not allow_pending and any(path.is_dir() and (path / "completion-pending.json").exists()
               for path in self.config.state_root.iterdir()):
            raise RuntimeError("An earlier task completion requires reconciliation")
        if not self.config.retain_failed_workspaces:
            for path in self.config.state_root.iterdir():
                if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{32}", path.name):
                    if (path / "completion-pending.json").exists():
                        continue
                    if path.is_symlink() or not path.is_dir():
                        raise ValueError("Unsafe orphan task directory")
                    shutil.rmtree(path)

    def discard(self, machine: TaskMachine):
        if machine.root.parent != self.config.state_root or machine.root.is_symlink():
            raise ValueError("Refusing to remove a foreign workspace")
        shutil.rmtree(machine.root)
