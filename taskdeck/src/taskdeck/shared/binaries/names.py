"""Readable names for C++ exports: the qualified name of a mangled symbol, without its parameter types.

    ?Draw@Window@@QAEXH@Z      (Microsoft)  ->  Window::Draw
    ??0Window@@QAE@XZ          (Microsoft)  ->  Window::Window
    @Window@Draw$qi            (Borland)    ->  Window::Draw
    @Window@$bdtr$qv           (Borland)    ->  Window::~Window

Only the name is decoded: decoding the parameter and return types (``(int)``, ``const``...) is a much
larger job, left for later. Anything not recognised gives "" rather than a guess, so the agent never
reads a wrong name.
"""

from __future__ import annotations

MSVC_SPECIAL = {
    "2": "operator new", "3": "operator delete", "4": "operator=", "5": "operator>>", "6": "operator<<",
    "7": "operator!", "8": "operator==", "9": "operator!=", "A": "operator[]", "B": "operator <conversion>",
    "C": "operator->", "D": "operator*", "E": "operator++", "F": "operator--", "G": "operator-", "H": "operator+",
    "I": "operator&", "J": "operator->*", "K": "operator/", "L": "operator%", "M": "operator<", "N": "operator<=",
    "O": "operator>", "P": "operator>=", "Q": "operator,", "R": "operator()", "S": "operator~", "T": "operator^",
    "U": "operator|", "V": "operator&&", "W": "operator||", "X": "operator*=", "Y": "operator+=",
    "Z": "operator-=", "_0": "operator/=", "_1": "operator%=", "_2": "operator>>=", "_3": "operator<<=",
    "_4": "operator&=", "_5": "operator|=", "_6": "operator^=", "_7": "`vftable'", "_8": "`vbtable'",
    "_U": "operator new[]", "_V": "operator delete[]",
}
BORLAND_SPECIAL = {
    "ctr": None, "dtr": None,  # constructor, destructor: the class name
    "new": "operator new", "dele": "operator delete", "nwa": "operator new[]", "dla": "operator delete[]",
    "asg": "operator=", "add": "operator+", "sub": "operator-", "mul": "operator*", "div": "operator/",
    "mod": "operator%", "eql": "operator==", "neq": "operator!=", "lss": "operator<", "gtr": "operator>",
    "leq": "operator<=", "geq": "operator>=", "inc": "operator++", "dec": "operator--", "not": "operator!",
    "ind": "operator*", "adr": "operator&", "subs": "operator[]", "call": "operator()", "arow": "operator->",
    "lsh": "operator<<", "rsh": "operator>>", "and": "operator&", "or": "operator|", "xor": "operator^",
    "land": "operator&&", "lor": "operator||", "rplu": "operator+=", "rmin": "operator-=",
    "rmul": "operator*=", "rdiv": "operator/=", "rmod": "operator%=", "rand": "operator&=", "ror": "operator|=",
    "rxor": "operator^=", "rlsh": "operator<<=", "rrsh": "operator>>=",
}


def readable(symbol: str) -> str:
    """The qualified name of a mangled C++ symbol ("Window::Draw"), or "" if it isn't one we can read."""
    try:
        if symbol.startswith("?"):
            return _msvc(symbol)
        if symbol.startswith("@") and "$" in symbol:
            return _borland(symbol)
    except (IndexError, ValueError):
        pass
    return ""


def _msvc(s: str) -> str:
    i, special = 1, None
    if s[i] == "?":  # a special name: constructor, destructor, operator
        if s[i + 1] == "$":
            return ""  # a template: not decoded
        code = s[i + 1:i + 3] if s[i + 1] == "_" else s[i + 1]
        if code in ("0", "1"):
            special = code
        elif code in MSVC_SPECIAL:
            special = MSVC_SPECIAL[code]
        else:
            return ""
        i += 1 + len(code)
    names: list[str] = []
    seen: list[str] = []  # back-references: a digit stands for the n-th name seen so far
    while True:
        if s[i] == "@":  # the end of the qualified name
            break
        if s[i].isdigit():
            names.append(seen[int(s[i])])
            i += 1
            continue
        if s[i] in "?$":
            return ""  # templates, nested special names: not decoded
        end = s.index("@", i)
        name = s[i:end]
        names.append(name)
        seen.append(name)
        i = end + 1
    if special is None and not names:
        return ""
    scopes = names[::-1]  # innermost last: Window, Draw
    if special in ("0", "1"):
        if not scopes:
            return ""
        cls = scopes[-1]
        return "::".join(scopes + [("~" if special == "1" else "") + cls])
    if special:
        return "::".join(scopes + [special])
    return "::".join(scopes)


def _borland(s: str) -> str:
    head = s[1:]
    if "%" in head:
        return ""  # templates: not decoded
    if "$b" in head:  # @Class@$bctr$qv: a special member
        before, _, rest = head.partition("$b")
        code = rest.split("$", 1)[0]
        if code not in BORLAND_SPECIAL:
            return ""
        scopes = [p for p in before.split("@") if p]
        if not scopes:
            return ""
        if code in ("ctr", "dtr"):
            return "::".join(scopes + [("~" if code == "dtr" else "") + scopes[-1]])
        return "::".join(scopes + [BORLAND_SPECIAL[code]])
    name = head.split("$", 1)[0]
    parts = [p for p in name.split("@") if p]
    return "::".join(parts) if parts else ""
