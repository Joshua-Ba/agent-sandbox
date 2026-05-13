# VM-Setup im Detail

Erklärt was in `setup.sh`, `start-vm.sh`, `stop-vm.sh` passiert und
warum die Einzelteile so gewählt sind.

## Komponenten

### Basis-Image: Debian Generic Cloud Image (ARM64)

Wir laden das offizielle Debian-Cloud-Image (`bookworm/latest`). "Generic"
heißt: hat `cloud-init` vorinstalliert und akzeptiert eine NoCloud-Datasource.
Damit ist die VM ohne manuelle Erstkonfiguration nutzbar.

### Overlay-Disk

Statt das geladene Image direkt zu mounten, legen wir mit
`qemu-img create -f qcow2 -F qcow2 -b debian-arm64.qcow2 disk.qcow2 20G`
ein Overlay an. Schreibvorgänge des Gasts landen im Overlay, das Basis-Image
bleibt unverändert.

Konsequenzen:

- **Reset** = Overlay löschen und neu anlegen. Eine Sekunde.
- **Multiple VMs** ab gleicher Basis sind möglich (z.B. eine pro Agent),
  jede mit eigenem Overlay, ohne das Basis-Image zu duplizieren.
- **Sparse**: das 20G ist eine Obergrenze, das File belegt nur was tatsächlich
  geschrieben wird (anfangs ~1MB).

### cloud-init Seed-ISO

cloud-init braucht eine Datasource. Im NoCloud-Modus erwartet es eine ISO
mit Volume-Label `cidata`, die `user-data` und `meta-data` enthält.
`setup.sh` baut diese aus `cloud-init/user-data` (Pubkey wird eingesetzt)
und `cloud-init/meta-data` mit `mkisofs` (oder `xorriso` als Fallback).

In `user-data` definieren wir:

- User `agent` mit Sudo, ohne Passwort, nur SSH-Key
- `ssh_pwauth: false` und `disable_root: true` – keine Passwort-Logins
- Pakete für spätere Schritte (Python, curl, wget)
- `runcmd`-Marker (`/var/log/sandbox-ready`) damit wir testen können, ob
  cloud-init durch ist

### UEFI

ARM64-Gäste in QEMU brauchen UEFI (kein BIOS). Wir kopieren die EDK2-Files
aus dem Homebrew-QEMU-Paket:

- `edk2-aarch64-code.fd` – Firmware, read-only, geteilt
- `edk2-arm-vars.fd` – NVRAM für UEFI-Variablen, pro VM kopiert (beschreibbar)

### Netzwerk

`-netdev user` ist QEMUs User-Mode-Networking (SLIRP). Vorteile:

- Keine Root-Rechte nötig
- Kein Bridge-Setup auf macOS
- Gast hat Internet über NAT

Nachteile (für uns irrelevant): kein Inbound-Traffic von außen, ICMP
eingeschränkt. Wir forwarden Ports explizit:

- `127.0.0.1:2222 → guest:22` für SSH
- `127.0.0.1:5900 → guest:5900` für VNC (kommt mit GUI-Schritt)

`-device virtio-net-pci` als NIC für Performance.

### Beschleunigung

`-accel hvf -cpu host`:

- HVF = Apple Hypervisor.framework, nativ auf Apple Silicon
- `cpu host` reicht die CPU-Features 1:1 durch (nur in Kombination mit HVF
  sinnvoll)

Ohne HVF würde QEMU TCG-Emulation nutzen → Größenordnungen langsamer.

### Steuerung

`-monitor unix:vm/qemu-monitor.sock,server,nowait` öffnet den QEMU-Monitor
als Unix-Socket. Damit kann `stop-vm.sh` graceful Shutdown senden
(`system_powerdown`, simuliert ACPI-Power-Button).

`-pidfile vm/qemu.pid` lässt QEMU seine PID schreiben, damit wir den Prozess
finden.

Im headless-Modus wird zusätzlich `-daemonize` gesetzt: QEMU forkt in den
Hintergrund, Skript kehrt sofort zurück.

## Boot-Phasen

1. **UEFI** (Sekundenbruchteile, EDK2 lädt aus pflash)
2. **Kernel + initramfs** (~5s)
3. **systemd Userspace** (~3s)
4. **cloud-init** – beim ersten Boot 60-90s (Paketinstallation),
   danach ~5s (nur Re-Check)
5. **sshd ready** – ab hier ist `ssh-vm.sh` erfolgreich

`verify.sh` pollt SSH alle 3 Sekunden bis Timeout (default 180s).

## Reset auf sauberen Zustand

```bash
./stop-vm.sh
rm vm/disk.qcow2
qemu-img create -f qcow2 -F qcow2 -b debian-arm64.qcow2 vm/disk.qcow2 20G
./start-vm.sh
./verify.sh
```

Wird später als Methode auf der `SandboxVM`-Klasse exponiert.

## Snapshots (geplant)

qcow2 unterstützt interne Snapshots. Geplante API:

```python
vm.snapshot("after-setup")        # qemu-img snapshot -c
vm.restore("after-setup")         # qemu-img snapshot -a
vm.list_snapshots()
```

Alternativ "external snapshots" als neue Overlay-Layer, die auf den letzten
Stand zeigen – schneller, aber komplexerer State.

## Bekannte Stolpersteine

**EDK2-Pfade**: Homebrew installiert die Firmware unter
`$(brew --prefix qemu)/share/qemu/`. `setup.sh` hat einen `find`-Fallback,
falls der Pfad anders ist.

**Erster Boot lang**: `package_update: true` in cloud-init zieht das Paket-Cache
neu. Lässt sich abschalten, kostet aber dann beim ersten `apt install`
trotzdem Zeit. Wir akzeptieren das beim ersten Boot.

**SSH-Hostkey ändert sich**: Bei Reset (`rm vm/disk.qcow2`) generiert der Gast
einen neuen Hostkey. `ssh-vm.sh` schreibt in `vm/known_hosts` mit
`StrictHostKeyChecking=accept-new`, also kein Prompt. Nach Reset einmal
`rm vm/known_hosts` falls Klagen kommen.

**Speicher**: Default 4GB. Falls du gleichzeitig ein 32B-Modell auf dem Host
fahren willst, runtersetzen via `SANDBOX_MEM_MB=2048 ./start-vm.sh`.

## Konfiguration über Env-Variablen

| Variable | Default | Wirkung |
|---|---|---|
| `SANDBOX_MODE` | `headless` | `headless` / `console` / `vnc` |
| `SANDBOX_MEM_MB` | `4096` | RAM in MB |
| `SANDBOX_CPUS` | `2` | vCPUs |
| `SANDBOX_SSH_PORT` | `2222` | Host-Port für SSH |
| `SANDBOX_VNC_PORT` | `5900` | Host-Port für VNC |
| `SANDBOX_VERIFY_TIMEOUT` | `180` | Sekunden, die `verify.sh` wartet |
