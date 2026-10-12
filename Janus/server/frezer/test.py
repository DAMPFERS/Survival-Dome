from cnc_controller import CNCController
import time


with CNCController("192.168.4.1", gcode_dir="/home/user/gcode") as cnc:
    # Быстрый список (только по расширению)
    for f in cnc.list_gcode_files():
        print(f"{f.name:30s}  {f.size:>8d} B  {time.ctime(f.mtime)}")

    # С проверкой содержимого
    ready = [f for f in cnc.list_gcode_files(validate=True) if f.valid]
    if ready:
        cnc.start_milling(ready[0].path)