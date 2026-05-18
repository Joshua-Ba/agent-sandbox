# agent-sandbox

> A local, isolated execution environment for LLM agents.
> Model runs on the host, code runs in the VM.

`agent-sandbox` is a self-hosted sandbox where one or more LLM agents can
execute arbitrary code, manipulate files, and interact with a desktop –
without putting the host system at risk. The language model runs on the
local machine (e.g. via `llama.cpp` or MLX on Apple Silicon); agents
orchestrate actions through a defined interface to the VM.

## Motivation

Existing "computer use" setups tend to have one of two drawbacks: they
either run in the cloud (data leaves the machine, latency, cost), or they
use containers, whose isolation for arbitrary code is thinner than commonly
assumed. This project tries to address both:

- **Local**: model inference on your own hardware, no outbound API calls.
- **Properly isolated**: hardware virtualization (HVF on Apple Silicon).
  A malicious command in the guest can at worst damage the VM disk, not
  the host system.
- **One model, multiple agents**: several specialized agents share a single
  in-memory model instance.

## Architecture

```
┌─────────────────── Host (macOS) ──────────────────┐
│                                                    │
│  ┌──────────────┐         ┌─────────────────────┐ │
│  │ LLM (local)  │◀───────▶│ Agent orchestrator  │ │
│  │  llama.cpp   │         │      (Python)       │ │
│  │  / MLX       │         └──────────┬──────────┘ │
│  └──────────────┘                    │            │
│                                      ▼            │
│                            ┌──────────────────┐   │
│                            │ SandboxVM API    │   │
│                            │ - run_command()  │   │
│                            │ - put_file()     │   │
│                            │ - get_file()     │   │
│                            │ - screenshot()   │   │
│                            │ - click/type     │   │
│                            └────┬─────────┬───┘   │
│                                 │ SSH     │ VNC   │
│                                 ▼         ▼       │
│  ┌────────────────────────────────────────────┐  │
│  │ QEMU (HVF-accelerated)                     │  │
│  │ ┌────────────────────────────────────────┐ │  │
│  │ │ Debian ARM64 guest                     │ │  │
│  │ │  - sshd                                │ │  │
│  │ │  - XFCE + x11vnc  (planned)            │ │  │
│  │ │  - guest-agent    (planned)            │ │  │
│  │ └────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

**Inbound to guest**: files and commands over SSH/SFTP.
**Outbound from guest**: terminal output over SSH, screenshots over VNC.

## Status

Early stage. Current state:

- [x] **Step 1**: VM setup – QEMU/HVF, Debian ARM64, cloud-init,
      overlay disks, start/stop/verify scripts
- [x] **Step 2**: Python wrapper `SandboxVM` with
      `run_command` / `put_file` / `get_file`
- [x] **Step 3**: GUI in the guest (XFCE + x11vnc)
- [x] **Step 4**: `screenshot()` over VNC, later `click()` / `type()`
- [ ] **Step 5**: snapshot/restore – clean reset between agent tasks
- [ ] **Step 6**: agent orchestrator and tool definitions for the LLM
- [ ] **Step 7**: integration with a local model (llama.cpp / MLX)

## Quickstart

VM layer only, for now. Requirements:

- macOS on Apple Silicon (M1/M2/M3/M4)
- [Homebrew](https://brew.sh/)

```bash
brew install qemu cdrtools wget

git clone <repo-url> agent-sandbox
cd agent-sandbox

./setup.sh        # one-off: fetch image, build cloud-init (~400 MB download)
./start-vm.sh     # start VM headless in the background
./verify.sh       # waits for SSH, prints guest info
./ssh-vm.sh       # interactive login as 'agent'
./stop-vm.sh      # graceful shutdown
```

First boot takes 1–2 minutes due to cloud-init.
Subsequent boots are under 15 seconds.

Detailed documentation for the VM layer: [`docs/vm-setup.md`](docs/vm-setup.md).

## Python API
 
After the VM is running, the Python wrapper lets you drive it programmatically:
 
```bash
pip install -e ".[dev]"
```
 
```python
from agent_sandbox import SandboxVM
 
with SandboxVM() as vm:
    result = vm.run("uname -a")
    print(result.stdout, result.exit_code, result.duration_s)
 
    vm.put_file("local.txt", "/home/agent/remote.txt")
    vm.get_file("/etc/os-release", "os-release")
 
    vm.write_text("/tmp/script.sh", "#!/bin/sh\necho hi\n", mode=0o755)
    print(vm.run("/tmp/script.sh", check=True).stdout)
```
 
Run the demo: `python examples/demo.py`
 
Tests:
 
```bash
pytest                       # unit tests only (fast)
pytest -m integration        # against a running VM
```

## Design decisions

**Why QEMU + HVF, not Docker?**
Containers share the host kernel. For code coming from an LLM, which may
make unpredictable system calls, a VM boundary is the more robust isolation.
HVF gives near-native speed on Apple Silicon; the overhead is acceptable.

**Why Debian ARM64, not x86_64?**
ARM64 runs natively under HVF on Apple Silicon. x86_64 would require TCG
emulation and be orders of magnitude slower.

**Why cloud-init?**
Reproducible setup with no manual steps. Anyone cloning the repo gets the
same VM. Configuration is declarative in `cloud-init/user-data` and therefore
version-controlled.

**Why overlay disks?**
`disk.qcow2` is a thin overlay on top of the read-only base image. Resetting
to a clean state takes a second. The later snapshot feature builds on this.

**Why SSH instead of a custom protocol?**
SSH is battle-tested, cleanly accessible from Python via `paramiko` or
`asyncssh`, and gives us file transfer (SFTP) and command execution in one
package. A custom protocol would be premature optimization.

## Non-goals

- **Not for production hosting**. Single user, single host. No multi-tenancy.
- **Not hardened against an active attacker with kernel exploits**. Isolation
  is sufficient for accidentally destructive agent actions, not for targeted
  VM escapes.
- **No cloud integration**. If you want that,
  [Anthropic Computer Use](https://docs.claude.com/en/docs/build-with-claude/computer-use)
  or [E2B](https://e2b.dev/) are probably a better fit.
- **Not a generic agent framework**. The focus is on the sandbox layer. What
  runs on top (LangGraph, a custom loop, anything else) is left open.

## Repository layout

```
agent-sandbox/
├── README.md           # this file
├── cloud-init/         # declarative VM configuration
├── setup.sh            # one-off setup
├── start-vm.sh         # start the VM
├── stop-vm.sh          # stop the VM
├── verify.sh           # check SSH reachability
├── ssh-vm.sh           # interactive SSH login
└── vm/                 # generated, not checked in
    ├── debian-arm64.qcow2
    ├── disk.qcow2
    ├── seed.iso
    └── ssh_key{,.pub}
```
