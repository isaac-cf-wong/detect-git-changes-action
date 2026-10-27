"""Packaging-aware inspection of a Rust (Cargo) project for the ``rust`` preset.

Usage::

    rust_packaging.py material <base-sha> <head-sha> <path/to/Cargo.toml | Cargo.lock>
        Exit 0 if that manifest differs between the revisions in a table that
        reaches the built crate -- or, for Cargo.lock, if a package the
        published crates build with changed -- 1 if it does not, 2 if either
        revision cannot be read or parsed (the caller treats that as material).

    rust_packaging.py package-paths <base-sha> <head-sha>
        Print one git pathspec per line for the files that go into the built
        crates in either revision: each published crate's sources, the files
        its code and build script read, the local crates it builds against,
        its manifest, readme and license files, and -- for a crate with a
        binary -- Cargo.lock. Exit 0 when the sources were located, or 3 when
        they could not be (fully) located, with the reasons on stderr; the
        paths printed are then incomplete.

Manifests and sources are read, never built. The tree reading, pathspec
collection and symlink following are shared with python_packaging.py. Only the
standard library is used; ``tomllib`` needs Python 3.11 or later.
"""

import bisect
import fnmatch
import os
import posixpath
import re
import subprocess
import sys
import tomllib

# Found beside this file, however the interpreter was started (python3 -I drops it).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from python_packaging import DEFAULT_LICENSE_GLOBS, UNRESOLVED, Collector, Tree, clean, git_show, table

MANIFEST = "Cargo.toml"

# Manifest keys that only shape development: what tests, benches and examples
# build against, lint levels, and metadata for other tools. Everything else
# reaches the built crate or its published page.
DEV_ONLY_KEYS = (
    ("dev-dependencies",),
    ("lints",),
    ("badges",),
    ("bench",),
    ("test",),
    ("example",),
    ("package", "metadata"),
    ("workspace", "metadata"),
    ("workspace", "lints"),
)

DEPENDENCY_TABLES = ("dependencies", "build-dependencies")

# Cargo looks for these when `readme` is not set.
DEFAULT_READMES = ("README.md", "README.txt", "README")

# Files that only change how a binary is built: a library's dependents resolve
# their own versions and use their own build configuration.
BINARY_BUILD_FILES = ("Cargo.lock", ".cargo/config.toml", ".cargo/config")


# ── Reading the tree ────────────────────────────────────────────────


def join(base, path):
    return clean(posixpath.join(base, path) if base else path)


def manifest_path(crate):
    return join(crate, MANIFEST)


def load_manifest(sha, crate):
    """A crate's parsed Cargo.toml; None when it does not exist there."""
    text = git_show(sha, manifest_path(crate))
    return None if text is None else tomllib.loads(text)


def read_files(sha, paths):
    """The text of several files at a revision, read in one ``git cat-file`` call."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="".join(f"{sha}:{p}\n" for p in paths).encode(),
        capture_output=True,
        check=True,
    ).stdout
    texts, pos = {}, 0
    for path in paths:
        header_end = batch.index(b"\n", pos)
        header = batch[pos:header_end].split()
        if header[-1] == b"missing":
            pos = header_end + 1
            continue
        size = int(header[2])
        texts[path] = batch[header_end + 1 : header_end + 1 + size].decode("utf-8", "replace")
        pos = header_end + 1 + size + 1
    return texts


def match_glob(pattern, path):
    """Whether a workspace ``members`` glob matches a directory: ``*`` within a component."""
    pattern, path = pattern.strip("/").split("/"), path.split("/")

    def walk(i, j):
        if i == len(pattern):
            return j == len(path)
        if pattern[i] == "**":
            return walk(i + 1, j) or (j < len(path) and walk(i, j + 1))
        return j < len(path) and fnmatch.fnmatchcase(path[j], pattern[i]) and walk(i + 1, j + 1)

    return walk(0, 0)


# ── Scanning Rust sources ───────────────────────────────────────────

STRING = r'"(?P<s>(?:[^"\\]|\\.)*)"|r(?P<h>#*)"(?P<r>.*?)"(?P=h)'
INCLUDE = re.compile(r"\b(?P<macro>include_str|include_bytes|include)!\s*\(\s*")
LITERAL_ARG = re.compile(rf"(?:{STRING})\s*,?\s*\)", re.S)
CONCAT_ARG = re.compile(r"concat!\s*\(")
CONCAT_PART = re.compile(rf'\s*(?:(?:{STRING})|(?P<var>\$\w+)|(?P<env>env!\s*\(\s*"(?P<name>\w+)"\s*\)))\s*(?P<end>,\s*\)|,|\))', re.S)
PATH_ATTR = re.compile(rf"#\[\s*(?:cfg_attr\s*\([^\]]*?,\s*)?path\s*=\s*(?:{STRING})", re.S)
MOD_DECL = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?:r#)?(\w+)\s*;", re.M)
INLINE_MOD = re.compile(r"\bmod\s+(?:r#)?(\w+)\s*\{")
CHAR = re.compile(r"'(?:\\u\{[0-9a-fA-F]{1,6}\}|\\.|[^\\'\n])'")
RERUN = re.compile(r"cargo::?rerun-if-changed=")
# Stands for the crate's directory in a path: `$CARGO_MANIFEST_DIR` or `env!("CARGO_MANIFEST_DIR")`.
CRATE_ROOT = "\0crate"
# A string passed to a macro or set in an attribute: `name!("x")`, `#[folder = "x"]`.
MACRO_OR_ATTRIBUTE_ARG = re.compile(r"(?:!\s*[(\[{]|#\[[^\]]*=)\s*$")
RERUN_ENV = re.compile(r"cargo::?rerun-if-env-changed=")


RAW_STRING_START = re.compile(r'b?r(#*)"')


def strip_comments(text):
    """Rust source without its comments, keeping line breaks so line numbers hold.

    String literals, raw strings included, are copied whole, so a ``//`` inside
    one is kept. Char literals are blanked: their contents are never paths, and
    a quote in one (``'"'``) would otherwise read as the start of a string. A
    lone ``'`` (a lifetime or a label) is copied as it is.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        raw = RAW_STRING_START.match(text, i) if text[i] in "br" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) else None
        char = CHAR.match(text, i) if text[i] == "'" else None
        if raw:
            end = text.find('"' + raw.group(1), raw.end())
            end = n if end < 0 else end + 1 + len(raw.group(1))
            out.append(text[i:end])
            i = end
        elif char:
            out.append("'" + " " * (char.end() - i - 2) + "'")
            i = char.end()
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append("\n" * text.count("\n", i, j))
            i = j
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def literal(match):
    return match.group("s") if match.group("s") is not None else match.group("r")


def concat_pattern(text):
    """The path a `concat!(...)` argument names, as a glob, or why it cannot be read.

    String literals are kept, a macro variable such as `$lang` becomes `*`, and an
    `env!("OUT_DIR")` part means a file the build script generates. Returns
    (pattern, None), (None, "OUT_DIR"), or (None, None) when it cannot be read.
    """
    pos, parts = 0, []
    while True:
        m = CONCAT_PART.match(text, pos)
        if not m:
            return None, None
        if m.group("env"):
            if m.group("name") == "OUT_DIR":
                return None, "OUT_DIR"
            if m.group("name") != "CARGO_MANIFEST_DIR" or parts:
                return None, None
            parts.append(CRATE_ROOT)
        else:
            parts.append("*" if m.group("var") else literal(m))
        pos = m.end()
        if m.group("end").endswith(")"):
            return "".join(parts), None


def submodules(sha):
    """The paths of the git submodules (gitlinks) at a revision."""
    out = subprocess.run(["git", "ls-tree", "-r", "-z", sha], capture_output=True, check=True).stdout.decode("utf-8")
    return {e.split("\t", 1)[1] for e in out.split("\0") if e.startswith("160000 ")}


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def blank_literals(text):
    """Text with the insides of string and char literals blanked, so braces in them don't count."""
    blank = lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1]
    return CHAR.sub(blank, re.sub(STRING, blank, text, flags=re.S))


def inline_modules(text):
    """A function from a position to the inline modules (`mod name { ... }`) enclosing it."""
    blanked = blank_literals(text)
    opens = {m.end() - 1: m.group(1) for m in INLINE_MOD.finditer(blanked)}
    stack, starts, contexts = [], [0], [()]
    for i, ch in enumerate(blanked):
        if ch == "{":
            stack.append(opens.get(i))
        elif ch == "}" and stack:
            stack.pop()
        else:
            continue
        starts.append(i + 1)
        contexts.append(tuple(n for n in stack if n))
    return lambda pos: contexts[bisect.bisect_right(starts, pos) - 1]


def closing_paren(text, start):
    """The index just past the parenthesis that closes the one before ``start``."""
    depth, i, n = 1, start, len(text)
    while i < n and depth:
        if text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return i


class Scanner:
    """Follows what Rust sources pull in: included files, `#[path]` and out-of-tree modules."""

    def __init__(self, c, sha):
        self.c = c
        self.sha = sha
        self.seen = set()
        self.submodules = submodules(sha)
        self.crate = ""  # the crate whose sources are being scanned
        self.roots = set()  # crate root files: their modules sit beside them, as for mod.rs

    def scan(self, paths, follow_mods=False):
        """Scan .rs files; ``follow_mods`` also tracks their `mod x;` files (for files outside src/)."""
        pending = [(p, follow_mods) for p in paths if p.endswith(".rs")]
        while pending:
            batch = [(p, f) for p, f in pending if p not in self.seen]
            pending = []
            self.seen.update(p for p, _ in batch)
            texts = read_files(self.sha, [p for p, _ in batch])
            for path, follow in batch:
                if path in texts:
                    pending += self.scan_one(path, strip_comments(texts[path]), follow)

    def scan_one(self, path, text, follow_mods):
        c, here, more = self.c, posixpath.dirname(path), []
        # Where this file's child modules live: beside a mod-rs file (a crate
        # root or mod.rs), in a directory named after any other file; inside an
        # inline module, one directory further per module.
        stem = posixpath.splitext(posixpath.basename(path))[0]
        mod_rs = stem == "mod" or path in self.roots or follow_mods == "root"
        module_dir = here if mod_rs else join(here, stem)
        context = inline_modules(text)

        def modules_at(pos):
            inline = context(pos)
            return join(module_dir, "/".join(inline)) if inline else module_dir

        def resolve(target, base=here):
            if target.startswith("$CARGO_MANIFEST_DIR"):
                target = CRATE_ROOT + target[len("$CARGO_MANIFEST_DIR") :]
            if target.startswith(CRATE_ROOT):
                rest = target[len(CRATE_ROOT) :].lstrip("/")
                return join(self.crate, rest) if rest else self.crate
            return join(base, target) if not target.startswith("/") else None

        def whole_repository(target, pos):
            c.unresolved(
                f"{path}:{line_of(text, pos)}: {target.replace(CRATE_ROOT, '$CARGO_MANIFEST_DIR')!r} names the whole"
                " repository; add the files involved to 'paths'"
            )

        def submodule_of(resolved):
            return next((m for m in self.submodules if resolved and resolved.startswith(f"{m}/")), None)

        def pull_in(target, what, pos, follow, base=here):
            resolved = resolve(target, base)
            if resolved == "":
                whole_repository(target, pos)
                return
            # A file inside a submodule changes with the submodule's commit.
            module = submodule_of(resolved)
            if module:
                c.add(module)
                return
            if resolved is None or not (c.tree.is_file(resolved) or c.tree.is_dir(resolved)):
                c.unresolved(
                    f"{path}:{line_of(text, pos)}: {what} names {target.replace(CRATE_ROOT, '$CARGO_MANIFEST_DIR')!r},"
                    " which is not a tracked file;"
                    " add the files involved to 'paths'"
                )
                return
            c.add(resolved)
            if resolved.endswith(".rs"):
                if what == "#[path]":
                    # rustc treats a file loaded through #[path] like a mod.rs.
                    self.roots.add(resolved)
                more.append((resolved, follow))

        handled = []  # spans of include_*! calls, whose strings are not read again below
        for m in INCLUDE.finditer(text):
            handled.append((m.start(), closing_paren(text, m.end())))
            rest = text[m.end() :]
            arg = LITERAL_ARG.match(rest)
            concat = CONCAT_ARG.match(rest)
            pattern, generated = concat_pattern(rest[concat.end() :]) if concat else (None, None)
            if arg:
                pull_in(literal(arg), f"{m.group('macro')}!", m.start(), False)
            elif generated:
                # OUT_DIR is written by the build script, whose inputs are tracked.
                continue
            elif pattern and "*" in pattern:
                # A path built in a macro from its arguments: every file it can name.
                glob = resolve(pattern)
                matched = glob and [f for f in c.tree.files if fnmatch.fnmatchcase(f, glob) and "/" not in f[len(posixpath.dirname(glob)) + 1 :]]
                if matched:
                    c.add_glob(glob)
                else:
                    c.unresolved(
                        f"{path}:{line_of(text, m.start())}: {m.group('macro')}! names {pattern!r}, which matches"
                        " no tracked file; add the files involved to 'paths'"
                    )
            elif pattern:
                pull_in(pattern, f"{m.group('macro')}!", m.start(), False)
            else:
                c.unresolved(
                    f"{path}:{line_of(text, m.start())}: {m.group('macro')}! with a path that is not a string"
                    " literal cannot be followed; add the files involved to 'paths'"
                )
        path_attrs = [m.start() for m in PATH_ATTR.finditer(text)]
        for m in PATH_ATTR.finditer(text):
            # At the top of a file, relative to its directory; inside inline
            # modules, to where those modules' children live.
            inline = context(m.start())
            pull_in(literal(m), "#[path]", m.start(), True, base=modules_at(m.start()) if inline else here)
        # Proc macros read files the compiler never sees named -- include_dir!,
        # rust-embed's #[folder], sqlx::migrate!, pest's #[grammar] -- so any
        # path-like string, or one passed to a macro or attribute, that names a
        # tracked file or directory is tracked too.
        for m in re.finditer(STRING, text, re.S):
            if any(start <= m.start() < end for start, end in handled):
                continue
            value = literal(m)
            if not value or "\n" in value or len(value) > 512 or value.startswith(("http:", "https:")):
                continue
            if not ("/" in value or "." in value or MACRO_OR_ATTRIBUTE_ARG.search(text, max(0, m.start() - 40), m.start())):
                continue
            target = resolve(value)
            candidates = [target] if value.startswith("$CARGO_MANIFEST_DIR") else [target, join(self.crate, value), join(self.crate, f"src/{value}")]
            for candidate in candidates:
                if candidate == "":
                    whole_repository(value, m.start())
                    break
                module = submodule_of(candidate)
                if module:
                    c.add(module)
                    break
                if candidate and (c.tree.is_file(candidate) or c.tree.is_dir(candidate)):
                    c.add(candidate)
                    break
        if follow_mods:
            for m in MOD_DECL.finditer(text):
                # A declaration carrying #[path] was followed above.
                if any(p < m.start() and not re.search(r"[;{}]", text[p : m.start()]) for p in path_attrs):
                    continue
                base = modules_at(m.start())
                candidates = (join(base, f"{m.group(1)}.rs"), join(base, f"{m.group(1)}/mod.rs"))
                found = next((f for f in candidates if f and c.tree.is_file(f)), None)
                if found is None:
                    c.unresolved(
                        f"{path}:{line_of(text, m.start())}: module {m.group(1)!r} is in none of"
                        f" {', '.join(f for f in candidates if f)}; add the files involved to 'paths'"
                    )
                    continue
                c.add(found)
                c.add(posixpath.dirname(found) if found.endswith("/mod.rs") else found[:-3])
                more.append((found, True))
        return more


# ── Locating crates ─────────────────────────────────────────────────


def inherited(value, workspace_package, key):
    """A package field, resolved through `key.workspace = true`; (value, from_workspace)."""
    if isinstance(value, dict) and value.get("workspace") is True:
        return workspace_package.get(key), True
    return value, False


class Project:
    def __init__(self, sha):
        self.sha = sha
        self.c = Collector(Tree(sha), sha)
        self.scanner = Scanner(self.c, sha)
        self.root = None
        self.tracked = set()
        self.binaries = set()  # tracked crates that have a binary target
        # package name -> the packages it builds with (normal and build
        # dependencies), for each crate tracked: the roots of the lock closure.
        self.build_deps = {}

    @property
    def workspace(self):
        return table(self.root, "workspace")

    def members(self):
        """The workspace's member crates, from `members` minus `exclude`."""
        ws = self.workspace
        crate_dirs = sorted(posixpath.dirname(f) for f in self.c.tree.files if posixpath.basename(f) == MANIFEST)
        members = []
        for pattern in ws.get("members", []) or []:
            if not isinstance(pattern, str):
                continue
            matched = [d for d in crate_dirs if d and match_glob(pattern, d)]
            if not matched:
                self.c.unresolved(f"Cargo.toml: workspace member {pattern!r} matches no crate")
            members += [d for d in matched if d not in members]
        excluded = [p for p in ws.get("exclude", []) or [] if isinstance(p, str)]
        return [m for m in members if not any(match_glob(p, m) or m.startswith(f"{p.strip('/')}/") for p in excluded)]

    def locate(self):
        c = self.c
        try:
            self.root = load_manifest(self.sha, "")
        except tomllib.TOMLDecodeError as exc:
            c.unresolved(f"Cargo.toml at {self.sha[:12]} does not parse: {exc}")
            return c
        if self.root is None:
            c.unresolved(f"no Cargo.toml at the repository root at {self.sha[:12]}")
            return c
        c.add(MANIFEST)
        crates = [""] if "package" in self.root else []
        manifests = {"": self.root}
        members = self.members() if self.workspace else []
        for member in members:
            if member == "":
                continue
            try:
                manifest = load_manifest(self.sha, member)
            except tomllib.TOMLDecodeError as exc:
                c.unresolved(f"{manifest_path(member)} does not parse: {exc}")
                continue
            if manifest is None:
                c.unresolved(f"workspace member {member} has no Cargo.toml")
                continue
            manifests[member] = manifest
            # A member that is never published is a development tool (xtask,
            # benches, fuzzing); it counts only as a dependency of one that is.
            if table(manifest, "package").get("publish", True) is not False:
                crates.append(member)
        if not crates:
            crates = [m for m in manifests if m]
        if not crates:
            c.unresolved("Cargo.toml has neither a [package] nor workspace members")
            return c
        # Path entries the workspace shares with its members.
        for target in self.path_entries("", [table(self.root, "workspace", "dependencies"), *table(self.root, "patch").values()]):
            self.track(target, manifests)
        for crate in crates:
            self.track(crate, manifests)
        # A crate first reached as another's path dependency is still published.
        if any(crate in self.binaries for crate in crates):
            for path in BINARY_BUILD_FILES:
                if c.tree.is_file(path):
                    c.add(path)
        return c

    def dependency_names(self, tables):
        """The package names behind the dependencies in ``tables``, renames resolved."""
        names = set()
        shared = table(self.root, "workspace", "dependencies")
        for deps in tables:
            for key, dep in deps.items() if isinstance(deps, dict) else ():
                if isinstance(dep, dict) and dep.get("workspace") is True:
                    dep = shared.get(key, {})
                name = dep.get("package", key) if isinstance(dep, dict) else key
                names.add(name if isinstance(name, str) else key)
        return names

    def path_entries(self, crate, tables):
        """The repository paths of the `path` dependencies in ``tables``."""
        found = []
        for deps in tables:
            for dep in deps.values() if isinstance(deps, dict) else ():
                if not isinstance(dep, dict):
                    continue
                # `workspace = true` needs nothing here: the workspace's own path
                # entries are tracked from the root manifest.
                path = dep.get("path")
                if not isinstance(path, str):
                    continue
                target = join(crate, path)
                if target is None or path.startswith("/"):
                    self.c.unresolved(f"{manifest_path(crate)}: path dependency {path!r} leaves the repository")
                elif target not in found:
                    found.append(target)
        return found

    def track(self, crate, manifests):
        """Track one crate and the local crates it builds against."""
        if crate in self.tracked:
            return
        self.tracked.add(crate)
        c = self.c
        manifest = manifests.get(crate)
        if manifest is None:
            try:
                manifest = load_manifest(self.sha, crate)
            except tomllib.TOMLDecodeError as exc:
                c.unresolved(f"{manifest_path(crate)} does not parse: {exc}")
                return
            if manifest is None:
                c.unresolved(f"path dependency {crate or '.'} has no Cargo.toml")
                return
        c.add(manifest_path(crate))
        package = table(manifest, "package")
        ws_package = table(self.root, "workspace", "package")

        # Sources: src/ holds the crate's modules by convention; targets placed
        # elsewhere are tracked with the modules they declare.
        src = join(crate, "src")
        if c.tree.is_dir(src):
            c.add(src)
        roots = []
        lib = manifest.get("lib")
        if isinstance(lib, dict) and isinstance(lib.get("path"), str):
            roots.append(join(crate, lib["path"]))
        bins = [b for b in manifest.get("bin", []) or [] if isinstance(b, dict)]
        roots += [join(crate, b["path"]) for b in bins if isinstance(b.get("path"), str)]
        outside = [r for r in roots if r and not r.startswith(f"{src}/")]
        for root in outside:
            c.add(root)
        autobins = package.get("autobins", True) is not False
        default_roots = [join(crate, "src/lib.rs"), join(crate, "src/main.rs")]
        if autobins:
            bin_dir = join(crate, "src/bin")
            default_roots += [
                f for f in c.tree.files
                if f.startswith(f"{bin_dir}/") and f.endswith(".rs") and f[len(bin_dir) + 1 :].count("/") <= 1
                and ("/" not in f[len(bin_dir) + 1 :] or f.endswith("/main.rs"))
            ]
        # An explicit [[bin]] without a `path` is still found by convention,
        # whether or not autobins is on.
        for b in bins:
            name = b.get("name")
            if isinstance(name, str) and not isinstance(b.get("path"), str):
                inferred = [f"src/bin/{name}.rs", f"src/bin/{name}/main.rs"]
                if name == package.get("name"):
                    inferred.insert(0, "src/main.rs")
                default_roots += [join(crate, f) for f in inferred]
        self.scanner.roots.update(r for r in roots + default_roots if r)
        if bins or (autobins and (c.tree.is_file(join(crate, "src/main.rs")) or c.tree.is_dir(join(crate, "src/bin")))):
            self.binaries.add(crate)
        self.scanner.crate = crate
        self.scanner.scan([f for f in c.tree.files if f.startswith(f"{src}/")])
        self.scanner.scan(outside, follow_mods="root")

        self.build_script(crate, package)

        readme, from_ws = inherited(package.get("readme"), ws_package, "readme")
        readme_base = "" if from_ws else crate
        if isinstance(readme, str):
            c.add(join(readme_base, readme))
        elif readme is True:
            c.add(join(readme_base, "README.md"))
        elif readme is None:
            for name in DEFAULT_READMES:
                if c.tree.is_file(join(crate, name) or name):
                    c.add(join(crate, name))
                    break
        license_file, from_ws = inherited(package.get("license-file"), ws_package, "license-file")
        if isinstance(license_file, str):
            c.add(join("" if from_ws else crate, license_file))
        for pattern in DEFAULT_LICENSE_GLOBS:
            c.add_glob(join(crate, pattern) or pattern)

        tables = [manifest.get(t) for t in DEPENDENCY_TABLES]
        for target in table(manifest, "target").values():
            if isinstance(target, dict):
                tables += [target.get(t) for t in DEPENDENCY_TABLES]
        if isinstance(package.get("name"), str):
            self.build_deps[package["name"]] = self.dependency_names(tables)
        for dep in self.path_entries(crate, tables):
            self.track(dep, manifests)

    def build_script(self, crate, package):
        """Track a build script and the inputs it declares through `cargo:rerun-if-changed`."""
        c = self.c
        build = package.get("build")
        if build is False:
            return
        path = join(crate, build) if isinstance(build, str) else join(crate, "build.rs")
        if not path or not c.tree.is_file(path):
            if isinstance(build, str):
                c.unresolved(f"{manifest_path(crate)}: build script {build!r} is not a tracked file")
            return
        c.add(path)
        self.scanner.crate = crate
        self.scanner.roots.add(path)  # the build script is its own crate root
        self.scanner.scan([path], follow_mods=True)
        text = strip_comments(read_files(self.sha, [path]).get(path, ""))
        declared = False
        for m in re.finditer(STRING, text, re.S):
            value = literal(m) or ""
            for hit in RERUN.finditer(value):
                declared = True
                target = value[hit.end() :].split("\\n")[0]
                if "{" in target or not target:
                    c.unresolved(
                        f"{path}:{line_of(text, m.start())}: rerun-if-changed takes a path worked out at build time;"
                        " add the files involved to 'paths'"
                    )
                elif target.startswith("/") or join(crate, target) is None:
                    c.unresolved(f"{path}: rerun-if-changed={target} is outside the repository")
                elif join(crate, target) == "":
                    c.unresolved(f"{path}: rerun-if-changed={target} names the whole repository; add its inputs to 'paths'")
                else:
                    c.add(join(crate, target))
        # Any rerun-if directive replaces cargo's default of rerunning the script
        # on every change in the crate, so environment-only directives declare
        # that it reads no files.
        if not declared and not any(RERUN_ENV.search(literal(m) or "") for m in re.finditer(STRING, text, re.S)):
            c.unresolved(
                f"{path} declares no cargo:rerun-if-changed, so cargo treats every file in the crate as its"
                " input; declare its inputs, or add them to 'paths'"
            )


# ── Commands ────────────────────────────────────────────────────────


def material_view(data):
    view = {k: v for k, v in data.items()}
    for keys in DEV_ONLY_KEYS:
        parent = view
        for key in keys[:-1]:
            child = parent.get(key)
            if not isinstance(child, dict):
                parent = None
                break
            parent[key] = child = dict(child)
            parent = child
        if isinstance(parent, dict):
            parent.pop(keys[-1], None)
    targets = view.get("target")
    if isinstance(targets, dict):
        kept = {}
        for name, spec in targets.items():
            if isinstance(spec, dict):
                spec = {k: v for k, v in spec.items() if k != "dev-dependencies"}
            if spec != {}:
                kept[name] = spec
        if kept:
            view["target"] = kept
        else:
            view.pop("target")
    return view


def lock_closure(sha):
    """The locked packages the published crates build with: (name, version, source, checksum).

    Cargo.lock also pins what tests, benches and examples build with, so a lock
    change counts only when it reaches this set. The walk starts from each
    tracked crate's normal and build dependencies; a workspace crate's own lock
    entry lists its dev-dependencies too, so for those the manifest is used.
    """
    text = git_show(sha, "Cargo.lock")
    if text is None:
        return None
    packages = [p for p in tomllib.loads(text).get("package", []) if isinstance(p, dict) and "name" in p]
    by_name = {}
    for p in packages:
        by_name.setdefault(p["name"], []).append(p)
    project = Project(sha)
    project.locate()

    def find(spec):
        name, _, rest = spec.partition(" ")
        found = by_name.get(name, [])
        version, _, source = rest.partition(" ")
        if version:
            found = [p for p in found if p.get("version") == version]
        if source:
            found = [p for p in found if f"({p.get('source')})" == source] or found
        return found

    stack = [p for deps in project.build_deps.values() for name in deps for p in by_name.get(name, [])]
    seen, closure = set(), set()
    while stack:
        p = stack.pop()
        key = (p["name"], p.get("version"), p.get("source"))
        if key in seen:
            continue
        seen.add(key)
        closure.add((*key, p.get("checksum")))
        if p.get("source") is None and p["name"] in project.build_deps:
            stack += [q for name in project.build_deps[p["name"]] for q in by_name.get(name, [])]
        else:
            stack += [q for spec in p.get("dependencies", []) if isinstance(spec, str) for q in find(spec)]
    return closure


def material(base_sha, head_sha, path):
    base, head = git_show(base_sha, path), git_show(head_sha, path)
    if base is None or head is None:
        return base is not head
    if posixpath.basename(path) == "Cargo.lock":
        return lock_closure(base_sha) != lock_closure(head_sha)
    return material_view(tomllib.loads(base)) != material_view(tomllib.loads(head))


def main(argv):
    if len(argv) == 5 and argv[1] == "material":
        try:
            return 0 if material(argv[2], argv[3], argv[4]) else 1
        except tomllib.TOMLDecodeError as exc:
            print(f"Cannot parse {argv[4]}: {exc}", file=sys.stderr)
            return 2
    if len(argv) != 4 or argv[1] != "package-paths":
        print(__doc__, file=sys.stderr)
        return 2
    paths, problems = [], []
    for sha in dict.fromkeys(argv[2:4]):
        c = Project(sha).locate()
        paths += [p for p in c.paths if p not in paths]
        problems += [p for p in c.problems if p not in problems]
    for path in paths:
        print(path)
    for problem in problems:
        print(problem, file=sys.stderr)
    return UNRESOLVED if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
