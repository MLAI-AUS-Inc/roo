"""One clean browser profile per isolated sidecar; any child exit stops all."""
import asyncio
import os
from pathlib import Path
import signal


async def main():
    for filename in ("tls.crt", "tls.key", "operators.htpasswd"):
        path = Path("/run/roo-browser-auth") / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Protected operator access must be provisioned")
    Path("/tmp/runtime").mkdir(mode=0o700)
    commands = [
        ["Xvfb", ":99", "-screen", "0", "1440x1000x24", "-nolisten", "tcp", "-ac"],
        ["x11vnc", "-display", ":99", "-localhost", "-rfbport", "5900", "-nopw", "-forever", "-shared", "-noxdamage"],
        ["websockify", "--web=/usr/share/novnc/", "127.0.0.1:6080", "127.0.0.1:5900"],
        ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
         "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222", "--user-data-dir=/profile/chromium",
         "--proxy-server=http://hermes-web-egress:3128", "--proxy-bypass-list=<-loopback>",
         "--disable-features=EncryptedClientHello", "--disable-quic", "--disable-sync", "about:blank"],
        ["python", "-I", "/opt/roo-browser/gate.py"],
        ["nginx", "-c", "/opt/roo-browser/nginx.conf", "-g", "daemon off;"],
    ]
    processes = []
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    try:
        processes.append(await asyncio.create_subprocess_exec(*commands[0], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        for _ in range(50):
            if Path("/tmp/.X11-unix/X99").exists():
                break
            if processes[0].returncode is not None:
                raise RuntimeError("Display startup failed")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Display startup deadline")
        for command in commands[1:]:
            processes.append(await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        waits = [asyncio.create_task(p.wait()) for p in processes] + [asyncio.create_task(stopping.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        try:
            await asyncio.wait_for(asyncio.gather(*(p.wait() for p in processes)), 3)
        except TimeoutError:
            for process in processes:
                if process.returncode is None:
                    process.kill()
            await asyncio.gather(*(p.wait() for p in processes))


if __name__ == "__main__":
    asyncio.run(main())
