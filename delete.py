from agent_sandbox import SandboxVM

with SandboxVM() as vm:
    # Mousepad starten (entkoppelt von SSH-Session)
    vm.run("setsid -f mousepad >/dev/null 2>&1", env={"DISPLAY": ":1"})

    # Kurz warten bis Fenster da ist
    import time
    time.sleep(2)

    vm.screenshot().save("before.png")

    # In die Mitte klicken (sollte ins Mousepad-Textfeld treffen falls maximiert)
    vm.click(640, 400)
    vm.type_text("Hallo aus dem Agenten")
    vm.key("Return")
    vm.type_text("Zweite Zeile")

    vm.screenshot().save("after.png")