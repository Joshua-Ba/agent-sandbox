"""Demo: was kann man mit der SandboxVM-API machen.

Voraussetzung: VM läuft (./start-vm.sh).

Aufruf:
  python examples/demo.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_sandbox import SandboxConfig, SandboxVM


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Default-Config reicht meistens. wait_for_cloud_init=True nur beim ersten
    # Boot interessant – danach ist der Marker längst geschrieben.
    config = SandboxConfig(wait_for_cloud_init=False)

    with SandboxVM(config) as vm:
        print("\n=== Basic commands ===")
        result = vm.run("uname -a")
        print("uname:", result.stdout.strip())
        print(f"  ({result.duration_s*1000:.0f} ms)")

        result = vm.run("cat /etc/os-release | head -2")
        print("os-release:")
        print(result.stdout)

        print("=== cwd and env ===")
        result = vm.run("pwd && echo greeting=$GREETING", cwd="/tmp", env={"GREETING": "hello"})
        print(result.stdout)

        print("=== file transfer ===")
        with TemporaryDirectory() as tmp:
            local_in = Path(tmp) / "msg.txt"
            local_in.write_text("hi from host\n")

            vm.put_file(local_in, "/tmp/from_host.txt")
            print("put_file ok")

            # Im Gast manipulieren
            vm.run("sed -i 's/host/sandbox/' /tmp/from_host.txt", check=True)

            local_out = Path(tmp) / "result.txt"
            vm.get_file("/tmp/from_host.txt", local_out)
            print("get_file ok:", local_out.read_text().strip())

            vm.run("rm /tmp/from_host.txt")

        print("=== write_text / read_text shortcuts ===")
        script = """\
#!/bin/sh
echo "Im running as $(whoami) on $(uname -m)"
"""
        vm.write_text("/tmp/hello.sh", script, mode=0o755)
        result = vm.run("/tmp/hello.sh", check=True)
        print(result.stdout.strip())
        vm.run("rm /tmp/hello.sh")

        print("\n=== alles ok ===")


if __name__ == "__main__":
    main()
