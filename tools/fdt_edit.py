#!/usr/bin/env python3
"""Generic FDT (dtb) structural editor: set/add properties, add/disable nodes.

Why: on Rockchip Android ports the vendor dtb inside boot.img is often missing
properties the *kernel* requires (e.g. vop port `rockchip,plane-mask` /
`rockchip,primary-plane`).  You cannot fix that with an equal-length in-place
byte patch, because the property simply does not exist yet -- the structure
block has to grow, which shifts the strings block and total size.

What it does: parse -> tree -> (mutate) -> re-serialize with a rebuilt strings
block, then fix header offsets.  Output is a valid dtb of a new length.

Usage
  fdt_edit.py in.dtb -o out.dtb \
      --set-u32  /vop@fe040000/ports/port@0:rockchip,plane-mask=0x0f \
      --set-u32  /vop@fe040000/ports/port@0:rockchip,primary-plane=0x02 \
      --set-str  /vop@fe040000/ports/port@2:status=disabled
  fdt_edit.py in.dtb --dump /vop@fe040000/ports/port@0     # print props
  fdt_edit.py in.dtb --self-test                           # round-trip check

DISCIPLINE
  Always run --self-test (or --dump) after editing: totalsize must equal the
  file length, the token walk must end with a single FDT_END at the right depth,
  and every pre-existing property must be byte-identical to the input.
"""

import struct
import sys

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


class Node:
    __slots__ = ("name", "props", "children")

    def __init__(self, name=""):
        self.name = name
        self.props = []      # list of [name(bytes), value(bytes)]
        self.children = []   # list of Node

    def child(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def get(self, pname):
        for p, v in self.props:
            if p == pname:
                return v
        return None

    def set(self, pname, value):
        for item in self.props:
            if item[0] == pname:
                item[1] = value
                return False
        # keep the property list sorted by name, like dtc does
        item = [pname, value]
        idx = len(self.props)
        for i, (p, _) in enumerate(self.props):
            if p > pname:
                idx = i
                break
        self.props.insert(idx, item)
        return True


def parse(data):
    magic, totalsize, off_struct, off_strings, off_rsv, version, lastcomp, cpuid, \
        size_strings, size_struct = struct.unpack_from(">10I", data, 0)
    if magic != FDT_MAGIC:
        raise SystemExit("bad FDT magic %#x" % magic)
    if totalsize > len(data):
        raise SystemExit("totalsize %d > file %d" % (totalsize, len(data)))

    # reserved memory map: copy raw up to the terminator (0,0)
    rsv = b""
    pos = off_rsv
    while True:
        a, b = struct.unpack_from(">QQ", data, pos)
        rsv += struct.pack(">QQ", a, b)
        pos += 16
        if a == 0 and b == 0:
            break

    root = None
    stack = []
    pos = off_struct
    while pos < off_struct + size_struct:
        tok = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if tok == FDT_BEGIN_NODE:
            e = data.index(b"\x00", pos)
            name = data[pos:e].decode("latin1")
            pos = (e + 4) & ~3
            n = Node(name)
            if root is None:
                root = n          # the first BEGIN_NODE *is* the root
            else:
                stack[-1].children.append(n)
            stack.append(n)
        elif tok == FDT_END_NODE:
            stack.pop()
        elif tok == FDT_PROP:
            ln, no = struct.unpack_from(">II", data, pos)
            pos += 8
            val = data[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
            e = data.index(b"\x00", off_strings + no)
            pname = data[off_strings + no:e]
            stack[-1].props.append([pname, val])
        elif tok == FDT_NOP:
            continue
        elif tok == FDT_END:
            break
        else:
            raise SystemExit("bad token %d at %#x" % (tok, pos - 4))
    return {
        "root": root,
        "rsv": rsv,
        "version": version,
        "last_comp": lastcomp,
        "cpuid": cpuid,
    }


def _walk(node, path, out):
    out.append((path, node))
    for c in node.children:
        _walk(c, path + "/" + c.name if path else "/" + c.name, out)


def find(root, path):
    if path in ("/", ""):
        return root
    cur = root
    for part in path.strip("/").split("/"):
        cur = cur.child(part)
        if cur is None:
            return None
    return cur


def serialize(tree):
    root = tree["root"]

    # pass 1: strings block
    strings = bytearray()
    noff = {}

    def add_str(name):
        if name in noff:
            return noff[name]
        off = len(strings)
        strings.extend(name)
        strings.append(0)
        noff[name] = off
        return off

    flat = []
    _walk(root, "", flat)
    for _, n in flat:
        for p, _v in n.props:
            add_str(p)

    # pass 2: structure block
    st = bytearray()

    def emit(node):
        st.extend(struct.pack(">I", FDT_BEGIN_NODE))
        st.extend(node.name.encode("latin1"))
        st.extend(b"\x00")
        while len(st) % 4:
            st.extend(b"\x00")
        for p, v in node.props:
            st.extend(struct.pack(">III", FDT_PROP, len(v), noff[p]))
            st.extend(v)
            while len(st) % 4:
                st.extend(b"\x00")
        for c in node.children:
            emit(c)
        st.extend(struct.pack(">I", FDT_END_NODE))

    emit(root)
    st.extend(struct.pack(">I", FDT_END))

    off_rsv = 40
    rsv = tree["rsv"]
    off_struct = off_rsv + len(rsv)
    off_strings = off_struct + len(st)
    total = off_strings + len(strings)

    hdr = struct.pack(">10I", FDT_MAGIC, total, off_struct, off_strings, off_rsv,
                      tree["version"], tree["last_comp"], tree["cpuid"],
                      len(strings), len(st))
    return hdr + rsv + bytes(st) + bytes(strings)


def dump(tree, path):
    n = find(tree["root"], path)
    if n is None:
        raise SystemExit("node not found: %s" % path)
    print("node %s  (%d props, %d children)" % (path, len(n.props), len(n.children)))
    for p, v in n.props:
        txt = ""
        if v.endswith(b"\x00") or (len(v) > 1 and b"\x00" in v and all(32 <= c < 127 or c == 0 for c in v)):
            txt = "  \"" + v.split(b"\x00")[0].decode("latin1") + "\""
        elif len(v) % 4 == 0 and len(v) <= 16:
            txt = "  [" + ",".join("0x%x" % x for x in struct.unpack(">%dI" % (len(v) // 4), v)) + "]"
        print("   %-34s %s%s" % (p.decode("latin1"), v[:32].hex(), txt))


def tree_signature(root):
    """path -> {prop: bytes} mapping for equality checks."""
    flat = []
    _walk(root, "", flat)
    return {p: {k.decode("latin1"): v for k, v in n.props} for p, n in flat}


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    src = args[0]
    data = open(src, "rb").read()
    tree = parse(data)

    if "--self-test" in args:
        again = serialize(tree)
        t2 = parse(again)
        a, b = tree_signature(tree["root"]), tree_signature(t2["root"])
        ok = a == b and len(again) == struct.unpack_from(">I", again, 4)[0]
        print("round-trip: %s  (%d -> %d bytes)" % ("OK" if ok else "FAIL", len(data), len(again)))
        if not ok:
            only_a = set(a) - set(b)
            only_b = set(b) - set(a)
            print("  nodes only in input:", list(only_a)[:5])
            print("  nodes only in output:", list(only_b)[:5])
            for k in a:
                if k in b and a[k] != b[k]:
                    d = [p for p in set(a[k]) | set(b[k]) if a[k].get(p) != b[k].get(p)]
                    print("  props differ in", k, d[:6])
            raise SystemExit(1)
        if "-o" not in args:
            return

    if "--dump" in args:
        dump(tree, args[args.index("--dump") + 1])
        return

    out = None
    if "-o" in args:
        out = args[args.index("-o") + 1]

    changed = 0
    i = 0
    while i < len(args):
        if args[i] in ("--set-u32", "--set-str", "--set-bytes"):
            spec = args[i + 1]
            mode = args[i]
            node_path, _, rest = spec.partition(":")
            pname, _, val = rest.partition("=")
            n = find(tree["root"], node_path)
            if n is None:
                raise SystemExit("node not found: %s" % node_path)
            if mode == "--set-u32":
                v = struct.pack(">I", int(val, 0))
            elif mode == "--set-str":
                v = val.encode("latin1") + b"\x00"
            else:
                v = bytes.fromhex(val)
            added = n.set(pname.encode("latin1"), v)
            changed += 1
            print("%s %s:%s = %s" % ("added" if added else "updated",
                                     node_path, pname, v.hex()))
            i += 2
        elif args[i] == "--del":
            spec = args[i + 1]
            node_path, _, pname = spec.partition(":")
            n = find(tree["root"], node_path)
            if n is None:
                raise SystemExit("node not found: %s" % node_path)
            before_n = len(n.props)
            n.props = [pr for pr in n.props if pr[0] != pname.encode("latin1")]
            if len(n.props) == before_n:
                print("MISSING %s:%s (nothing deleted)" % (node_path, pname))
            else:
                changed += 1
                print("deleted %s:%s" % (node_path, pname))
            i += 2
        else:
            i += 1

    if changed and out:
        blob = serialize(tree)
        open(out, "wb").write(blob)
        # verify what we actually wrote
        chk = parse(open(out, "rb").read())
        total = struct.unpack_from(">I", blob, 4)[0]
        print("wrote %s  %d bytes (was %d)  totalsize=%d %s"
              % (out, len(blob), len(data), total, "OK" if total == len(blob) else "MISMATCH"))
        before = tree_signature(parse(data)["root"])
        after = tree_signature(chk["root"])
        lost = [k for k in before if k not in after or
                {p: v for p, v in before[k].items()} != {p: v for p, v in after[k].items()
                                                         if p in before[k]}]
        print("nodes: %d -> %d" % (len(before), len(after)))


if __name__ == "__main__":
    main()
