"""Stands in for idaw.exe in the tests. It reads the generated job.idc and acts out the operation.

The input's file name picks a behaviour: *hang* sleeps forever, *slow* sleeps 2 s, *crash* stops part-way,
*noscript* exits without running the script, *nodb* never writes the database.
"""

import re
import sys
import time
from pathlib import Path

args = sys.argv[1:]
script = next(a[2:] for a in args if a.startswith("-S"))
log = next((a[2:] for a in args if a.startswith("-L")), None)
target = Path(args[-1])
create = "-c" in args
text = Path(script).read_text(encoding="cp1252")
result = re.search(r'out = fopen\("([^"]+)"', text).group(1)
call = re.search(r"rc = op_(\w+)\(out(?:, )?(.*)\);", text)
op, raw_args = call.group(1), call.group(2)
saves = "SaveBase" in text
name = target.name.lower()

if log:
    Path(log).write_text(f"fake IDA: {op} on {target}\n")

if "hang" in name:
    time.sleep(3600)
if "slow" in name:
    time.sleep(2)
if "noscript" in name:
    sys.exit(1)

idb = target.with_suffix(".idb")
if create:
    if "nodb" not in name:
        idb.write_text("IDB\n")
elif not idb.exists():
    sys.exit(2)

lits = re.findall(r'"((?:[^"\\]|\\.)*)"|([^,\s]+)', raw_args)
vals = [a or b for a, b in lits]

with open(result, "w") as out:
    if "crash" in name:
        out.write("0x401000\t0x401010\t16\tpartial\t0x0\n")
        out.flush()
        sys.exit(139)
    rc = 0
    if op in ("info", "analyze"):
        out.write("#INFO\tinput_file\t%s\n#INFO\tstart_ea\t0x401000\n#INFO\tidb\t%s\n" % (target.name, (idb.read_text().strip().replace("\n", "|") if idb.exists() else "")))
    elif op == "list_functions":
        rows = [("0x401000", "0x401010", "16", "start", "0x0"), ("0x401010", "0x401020", "16", "Draw\\tthing", "0x0")]
        for r in rows:
            if vals[0] in r[3]:
                out.write("\t".join(r) + "\n")
        out.write("#TOTAL\t2\n")
    elif op == "export":
        Path(vals[1]).write_text("; fake asm\n" + "".join(f"line {i}\n" for i in range(30)))
        out.write("#INFO\tlines\t31\n")
    elif op == "rename":
        if vals[2] == "fail":
            out.write("#ERROR\tIDA refused the name (invalid or already used)\n")
            rc = 1
        else:
            idb.write_text(idb.read_text() + f"renamed {vals[2]}\n")
            out.write(f"#INFO\tnew_name\t{vals[2]}\n")
    elif op == "run_idc":
        out.write("hello from idc\n")
    else:
        out.write("#TOTAL\t0\n")
    out.write(f"#END\t{rc}\n")
sys.exit(rc)
