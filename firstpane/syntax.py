"""Token roles for one line of code in a diff or review excerpt: keyword, string, number,
comment and called name.

A lexical hint, not a parser: the language is guessed from the file name, each line is read
on its own (a string or comment opened on an earlier line is not known), and a file type it
does not know is left plain.
"""

import re

ROLE_SGR = {
    "string": "38;5;180",
    "comment": "38;5;245",
    "number": "38;5;141",
    "keyword": "38;5;111",
    "call": "38;5;153",
}
KEYWORDS = frozenset(
    """
    and as async await break case catch class const continue def default defer del do elif else
    enum except export extends false final finally fn for from func function go if impl import in
    interface is lambda let match mod mut new nil none not null or package pass private protected
    pub public raise return self static struct super switch this throw throws true try type typeof
    undefined use val var void when where while with yield None True False
    """.split()
)
HASH = frozenset("py pyi sh bash zsh fish rb pl r yml yaml toml cfg conf ini mk cmake nix tf ex exs".split())
SLASH = frozenset(
    "js jsx mjs cjs ts tsx go rs c h cc cpp hpp cs java kt kts swift scala php dart css scss less proto zig"
    " vue svelte jsonc".split()
)
DASH = frozenset("sql lua hs elm".split())
UNMARKED = frozenset("json html xml ml".split())
NAMES = {"Makefile": "#", "Dockerfile": "#", "Justfile": "#", "Rakefile": "#", "Gemfile": "#"}
_TOKEN = re.compile(
    r"""(?P<string>"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`(?:[^`\\]|\\.)*`?)"""
    r"""|(?P<number>\b\d[\d_]*(?:\.\d+)?\b)"""
    r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?=(?P<call>\s*\())?"""
)


def comment_marker(path):
    """The line-comment marker of the file's language, "" when it has none, None for plain text."""
    name = (path or "").rsplit("/", 1)[-1]
    if name in NAMES:
        return NAMES[name]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in HASH:
        return "#"
    if extension in SLASH:
        return "//"
    if extension in DASH:
        return "--"
    if extension in UNMARKED:
        return ""
    return None


def tokens(text, path):
    """[(segment, role)] covering `text` exactly; role is a ROLE_SGR key or None."""
    marker = comment_marker(path)
    if marker is None:
        return [(text, None)] if text else []
    out = []

    def add(segment, role):
        if out and out[-1][1] == role:
            out[-1] = (out[-1][0] + segment, role)
        else:
            out.append((segment, role))

    position = 0
    while position < len(text):
        if marker and text.startswith(marker, position):
            add(text[position:], "comment")
            return out
        match = _TOKEN.search(text, position)
        stop = match.start() if match else len(text)
        if marker:
            found = text.find(marker, position, stop)
            if found != -1:
                stop = found
                match = None
        if stop > position:
            add(text[position:stop], None)
        if match is None:
            position = stop
            continue
        kind = match.lastgroup if match.group("word") is None else "word"
        word = match.group(kind)
        if kind == "word":
            role = "keyword" if word in KEYWORDS else ("call" if match.group("call") is not None else None)
        else:
            role = kind
        add(word, role)
        position = match.start() + len(word)
    return out
