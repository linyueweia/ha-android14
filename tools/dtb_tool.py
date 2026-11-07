#!/usr/bin/env python3
"""Minimal FDT reader + RSCE/boot.img helpers (stdlib only, Windows friendly).

Usage:
  python dtb_tool.py scan <file>          # list FDT blobs inside a file (RSCE resource etc.)
  python dtb_tool.py info <dtb>           # print model/compatible/bootargs + node status summary
  python dtb_tool.py extract <file> <n> <out>   # extract nth FDT blob inside <file>
"""
import struct
import sys

FDT_MAGIC = b"\xd0\x0d\xfe\xed"


def fdt_strings_map(buf, off_struct, size_struct, off_strings):
    """Yield (path, {prop: value}) for every node."""
    pos = off_struct
    end = off_struct + size_struct
    path = []
    out = []
    cur = None
    while pos < end:
        tok = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        if tok == 1:  # FDT_BEGIN_NODE
            name = b""
            while buf[pos] != 0:
                name += buf[pos : pos + 1]
                pos += 1
            pos += 1
            pos = (pos + 3) & ~3
            path.append(name.decode("utf-8", "replace"))
            cur = {"path": "/" + "/".join(p for p in path if p), "props": {}}
            out.append(cur)
        elif tok == 2:  # FDT_END_NODE
            if path:
                path.pop()
        elif tok == 3:  # FDT_PROP
            ln, nameoff = struct.unpack_from(">II", buf, pos)
            pos += 8
            val = buf[pos : pos + ln]
            pos += ln
            pos = (pos + 3) & ~3
            name = buf[off_strings + nameoff : buf.index(b"\0", off_strings + nameoff)].decode(
                "utf-8", "replace"
            )
            if cur is not None:
                cur["props"][name] = val
        elif tok == 4:  # FDT_NOP
            continue
        elif tok == 9:  # FDT_END
            break
        else:
            raise SystemExit(f"bad fdt token {tok} at {pos-4}")
    return out


def parse(buf, base=0):
    magic, totalsize, off_struct, off_strings, off_rsv, version, last_comp, boot_cpuid, size_strings, size_struct = (
        struct.unpack_from(">10I", buf, base)
    )
    assert struct.pack(">I", magic) == FDT_MAGIC
    nodes = fdt_strings_map(
        buf[base : base + totalsize], off_struct, size_struct, off_strings
    )
    return totalsize, nodes


def show(buf, base=0, label=""):
    total, nodes = parse(buf, base)
    root = nodes[0]["props"]
    model = root.get("model", b"").rstrip(b"\0").decode("utf-8", "replace")
    comp = root.get("compatible", b"").replace(b"\0", b"|").decode("utf-8", "replace")
    print(f"{label} totalsize={total} model={model!r} compatible={comp!r}")
    for n in nodes:
        if n["path"] == "/chosen":
            ba = n["props"].get("bootargs", b"").rstrip(b"\0").decode("utf-8", "replace")
            print(f"  bootargs: {ba}")
    dis = []
    for n in nodes:
        st = n["props"].get("status")
        if st is not None:
            s = st.rstrip(b"\0").decode("utf-8", "replace")
            if s not in ("okay", "ok"):
                dis.append(n["path"])
    print(f"  non-okay nodes ({len(dis)}): {', '.join(dis[:40])}{' ...' if len(dis)>40 else ''}")
    return nodes


def scan(path):
    data = open(path, "rb").read()
    hits = []
    i = data.find(FDT_MAGIC)
    while i != -1:
        if i + 40 <= len(data):
            try:
                total = struct.unpack_from(">I", data, i + 4)[0]
                if 1024 < total < 4 << 20 and i + total <= len(data):
                    hits.append((i, total))
            except struct.error:
                pass
        i = data.find(FDT_MAGIC, i + 1)
    print(f"{path}: {len(hits)} FDT blob(s)")
    for idx, (o, t) in enumerate(hits):
        print(f"--- blob {idx} at {o} size {t}")
        try:
            show(data[o : o + t], 0, label=f"    [{idx}]")
        except Exception as e:  # noqa: BLE001
            print("    parse error:", e)
    return hits


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "scan":
        scan(sys.argv[2])
    elif cmd == "info":
        data = open(sys.argv[2], "rb").read()
        show(data)
    elif cmd == "extract":
        data = open(sys.argv[2], "rb").read()
        n = int(sys.argv[3])
        hits = []
        i = data.find(FDT_MAGIC)
        while i != -1:
            total = struct.unpack_from(">I", data, i + 4)[0]
            if 1024 < total < 4 << 20 and i + total <= len(data):
                hits.append((i, total))
            i = data.find(FDT_MAGIC, i + 1)
        o, t = hits[n]
        open(sys.argv[4], "wb").write(data[o : o + t])
        print("wrote", sys.argv[4], t, "bytes")
    else:
        raise SystemExit(__doc__)
