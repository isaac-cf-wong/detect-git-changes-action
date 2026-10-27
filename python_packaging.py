"""Packaging-aware inspection of a Python project for the ``python`` preset.

Usage::

    python_packaging.py material <base-sha> <head-sha> [pyproject.toml|hatch.toml]
        Exit 0 if the build configuration differs between the revisions in a
        table that reaches the built distribution, 1 if it does not, 2 if either
        revision cannot be read or parsed (the caller treats that as material).
        With a file name, only that file's tables are compared; without one,
        pyproject.toml with hatch.toml merged in.

    python_packaging.py package-paths <base-sha> <head-sha>
        Print one git pathspec per line for the files that go into the built
        package in either revision: the package sources located from the build
        backend's configuration, and the readme and license files declared in
        pyproject.toml. Exit 0 when the sources were located, or 3 when they
        could not be (fully) located, with the reasons on stderr; the paths
        printed are then incomplete.

The configuration is read, never executed: setup.py is parsed with ``ast``, so
a setup.py that computes its arguments cannot be followed. Only the standard
library is used; ``tomllib`` needs Python 3.11 or later.
"""

import ast
import configparser
import fnmatch
import posixpath
import re
import subprocess
import sys
import tomllib
import warnings

PYPROJECT = "pyproject.toml"
HATCH_TOML = "hatch.toml"
# License files backends include by default; the entrypoint tracks the same
# patterns, and these are here so symlinks among them are followed.
DEFAULT_LICENSE_GLOBS = ["LICEN[CS]E*", "LICEN[CS]ES/**", "COPYING*", "NOTICE*", "AUTHORS*"]
UNRESOLVED = 3

# Key paths whose contents reach the built wheel/sdist or its metadata. Anything
# outside them -- [dependency-groups], [tool.ruff], [tool.pytest], hatch
# environments, poetry dependency groups -- only shapes the development setup.
MATERIAL_KEYS = (
    ("build-system",),
    ("project",),
    # hatchling / hatch-vcs
    ("tool", "hatch", "build"),
    ("tool", "hatch", "metadata"),
    ("tool", "hatch", "version"),
    # setuptools / setuptools-scm
    ("tool", "setuptools"),
    ("tool", "setuptools_scm"),
    ("tool", "setuptools-scm"),
    # flit, pdm, maturin, scikit-build-core, meson-python, uv's build backend
    ("tool", "flit"),
    ("tool", "pdm", "build"),
    ("tool", "pdm", "version"),
    ("tool", "maturin"),
    ("tool", "scikit-build"),
    ("tool", "meson-python"),
    ("tool", "uv", "build-backend"),
)

# Poetry keeps metadata and dev-only groups side by side under [tool.poetry],
# so it is compared whole except for the dev-only keys.
POETRY_DEV_ONLY = ("group", "dev-dependencies")

# Names setuptools' flat-layout auto-discovery never treats as a top-level
# package or module (setuptools.discovery.FlatLayoutPackageFinder._EXCLUDE and
# FlatLayoutModuleFinder.DEFAULT_EXCLUDE).
FLAT_LAYOUT_EXCLUDED_PACKAGES = (
    "ci", "bin", "debian", "doc", "docs", "documentation", "manpages", "news",
    "newsfragments", "changelog", "test", "tests", "unit_test", "unit_tests",
    "example", "examples", "scripts", "tools", "util", "utils", "python",
    "build", "dist", "venv", "env", "requirements", "tasks", "fabfile",
    "site_scons", "benchmark", "benchmarks", "exercise", "exercises", "htmlcov",
    "[._]*",
)  # fmt: skip
FLAT_LAYOUT_EXCLUDED_MODULES = (
    "setup", "conftest", "test", "tests", "example", "examples", "build",
    "toxfile", "noxfile", "pavement", "dodo", "tasks", "fabfile",
    "[Ss][Cc]onstruct", "conanfile", "manage", "benchmark", "benchmarks",
    "exercise", "exercises", "[._]*",
)  # fmt: skip

_MISSING = object()


# ── Reading a revision ──────────────────────────────────────────────


def git_show(sha, path):
    """The text of a file at a revision, or None when it does not exist there."""
    result = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8")


def load(sha, merge_hatch=True):
    """Parse pyproject.toml at a revision; None when the file does not exist there.

    A hatch.toml beside it is merged into [tool.hatch] the way hatchling does:
    each of its top-level tables replaces the one of the same name.
    """
    text = git_show(sha, PYPROJECT)
    if text is None:
        return None
    data = tomllib.loads(text)
    hatch_text = git_show(sha, HATCH_TOML) if merge_hatch else None
    if hatch_text is not None:
        tool = data.setdefault("tool", {})
        if isinstance(tool, dict):
            hatch = tool.get("hatch")
            tool["hatch"] = {**(hatch if isinstance(hatch, dict) else {}), **tomllib.loads(hatch_text)}
    return data


class Tree:
    """The tracked files of a revision, for existence checks without a checkout."""

    def __init__(self, sha):
        out = subprocess.run(["git", "ls-tree", "-r", "-z", sha], capture_output=True, check=True).stdout.decode("utf-8")
        self.files = set()
        link_blobs = {}
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            mode, _, blob = meta.split(" ")
            self.files.add(path)
            if mode == "120000":
                link_blobs[path] = blob
        # A symlink's blob is its target. Resolved against the link's directory;
        # None when it leaves the repository.
        self.links = {}
        if link_blobs:
            batch = subprocess.run(
                ["git", "cat-file", "--batch"],
                input=("\n".join(link_blobs.values()) + "\n").encode(),
                capture_output=True,
                check=True,
            ).stdout
            # Each record is "<sha> blob <size>\n<content>\n"; read by size, as a
            # target may itself contain a newline.
            pos = 0
            for path in link_blobs:
                header_end = batch.index(b"\n", pos)
                size = int(batch[pos:header_end].split()[2])
                target = batch[header_end + 1 : header_end + 1 + size].decode("utf-8", "surrogateescape")
                pos = header_end + 1 + size + 1
                resolved = None if target.startswith("/") else clean(posixpath.join(posixpath.dirname(path), target))
                self.links[path] = resolved or None
        self.dirs = set()
        for f in self.files:
            parent = posixpath.dirname(f)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)

    def loops(self, link):
        """True when resolving ``link`` as the OS would never ends: it revisits a
        path, or takes more than MAX_SYMLINK_HOPS hops."""
        path, seen = link, set()
        while len(seen) <= MAX_SYMLINK_HOPS:
            if path in seen:
                return True
            seen.add(path)
            if path in self.links:
                path = self.links[path]
            else:
                above = next((l for l in self.links if path.startswith(f"{l}/")), None)
                if above is None:
                    return False
                path = posixpath.join(self.links[above], path[len(above) + 1 :]) if self.links[above] else None
            if path is None:
                return False  # leaves the repository, reported on its own
        return True

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return path in self.dirs

    def dirs_under(self, base):
        prefix = f"{base}/" if base else ""
        return sorted(d for d in self.dirs if d.startswith(prefix))


def lookup(data, keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def table(data, *keys):
    value = lookup(data, keys)
    return value if isinstance(value, dict) else {}


def str_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def strings(value):
    """Every string inside a TOML value, depth first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def clean(path):
    """A repository-relative path, or None for one that leaves the repository."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    path = posixpath.normpath(path)
    if path == ".":
        return ""
    if path == ".." or path.startswith("../"):
        return None
    return path


def is_pattern(path):
    return any(ch in path for ch in "*?[")


# ── Collecting pathspecs ────────────────────────────────────────────


# As many as Linux follows before giving up with ELOOP.
MAX_SYMLINK_HOPS = 40


def glob_rests(pattern, path):
    """How a git ``:(glob)`` pattern, as components, can continue below ``path``.

    One tuple of remaining components per way the pattern's leading components
    match ``path``'s; an empty tuple when the pattern matches ``path`` itself or a
    directory above it, which the ``/**`` companion pathspec then covers.
    """
    rests = set()

    def walk(i, j):
        if i == len(pattern):
            rests.add(())
        elif j == len(path):
            rest = tuple(pattern[i:])
            rests.add(() if all(p == "**" for p in rest) else rest)
        elif pattern[i] == "**":
            walk(i + 1, j)
            walk(i, j + 1)
        elif fnmatch.fnmatchcase(path[j], pattern[i]):
            walk(i + 1, j + 1)

    walk(0, 0)
    return rests


class Collector:
    """The pathspecs found for one revision and the reasons any were missed."""

    def __init__(self, tree, sha):
        self.tree = tree
        self.sha = sha
        self.paths = []
        self.problems = []
        self._entered = set()

    def _append(self, spec):
        # The pathspecs are printed one per line.
        if "\n" in spec or "\r" in spec:
            self.unresolved(f"path {spec!r} contains a line break")
        elif spec not in self.paths:
            self.paths.append(spec)

    def add(self, path, _hops=0):
        """A file or directory; a git pathspec without wildcards matches both.

        A symlink at, under or above the path changes nothing in the diff when
        the file it leads to is edited, so where it leads is added too.
        """
        path = clean(path)
        if not path or not self._enter(f":(literal){path}", path, _hops):
            return
        for link, target in self.tree.links.items():
            if link == path or link.startswith(f"{path}/"):
                self._follow(link, target, lambda t: self.add(t, _hops + 1))
            elif path.startswith(f"{link}/"):
                self._append(f":(literal){link}")
                self._follow(link, target, lambda t: self.add(posixpath.join(t, path[len(link) + 1 :]), _hops + 1))

    def add_glob(self, pattern, _hops=0):
        pattern = clean(pattern)
        if not pattern or not self._enter(f":(glob){pattern}", pattern, _hops):
            return
        self._append(f":(glob){pattern}/**")
        for link, target in self.tree.links.items():
            for rest in glob_rests(pattern.split("/"), link.split("/")):
                if not rest:
                    self._follow(link, target, lambda t: self.add(t, _hops + 1))
                else:
                    self._append(f":(literal){link}")
                    self._follow(link, target, lambda t: self.add_glob(posixpath.join(t, *rest), _hops + 1))

    def _enter(self, spec, path, hops):
        """Add ``spec``; False when it is already there or symlinks loop."""
        if spec in self._entered:
            return False
        if hops > MAX_SYMLINK_HOPS:
            self.unresolved(f"symlinks leading to {path} loop or nest more than {MAX_SYMLINK_HOPS} deep")
            return False
        self._entered.add(spec)
        self._append(spec)
        return True

    def _follow(self, link, target, then):
        if target is None:
            self.unresolved(f"symlink {link} points outside the repository")
        elif self.tree.loops(link):
            self.unresolved(f"symlink {link} loops or nests more than {MAX_SYMLINK_HOPS} deep")
        else:
            then(target)

    def add_path_or_glob(self, path):
        if isinstance(path, str) and is_pattern(path):
            self.add_glob(path)
        else:
            self.add(path)

    def add_gitignore(self, pattern):
        """A gitignore-style pattern, as hatchling's ``include`` takes."""
        if not isinstance(pattern, str):
            return
        pattern = pattern.strip()
        if not pattern or pattern.startswith(("#", "!")):
            return
        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        pattern = pattern.strip("/")
        self.add_glob(pattern if anchored else f"**/{pattern}")

    def unresolved(self, reason):
        if reason not in self.problems:
            self.problems.append(reason)

    def build_hook(self, path, backend):
        """Code that runs during the build can add files no configuration names."""
        self.add(path)
        self.unresolved(
            f"{backend}: build hook {path} runs during the build and may add files the preset cannot see;"
            " add the files it reads to 'paths'"
        )


def import_names(name):
    """Directory names a project called ``name`` may keep its package under."""
    if not isinstance(name, str) or not name:
        return []
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    candidates = [
        # hatchling: normalize_file_name_component() of the raw and PEP 503 names
        re.sub(r"[^\w\d.]+", "_", name),
        re.sub(r"[^\w\d.]+", "_", normalized),
        # the import-name convention the other backends follow
        re.sub(r"[-.]+", "_", name),
        re.sub(r"[-.]+", "_", name).lower(),
    ]
    return list(dict.fromkeys(candidates))


def project_name(data):
    name = lookup(data, ("project", "name"))
    if not isinstance(name, str):
        name = lookup(data, ("tool", "poetry", "name"))
    return name if isinstance(name, str) else None


def discover_by_name(c, names, roots=("", "src")):
    """Add every ``<root>/<name>`` package or module that exists; True if any did."""
    found = False
    for name in names:
        for root in roots:
            path = posixpath.join(root, name) if root else name
            if c.tree.is_dir(path):
                c.add(path)
                found = True
            if c.tree.is_file(f"{path}.py"):
                c.add(f"{path}.py")
                found = True
    return found


def minimal(dirs):
    """Drop every directory that is inside another one in the list."""
    kept = []
    for d in sorted(dirs):
        if not any(d == k or d.startswith(f"{k}/") for k in kept):
            kept.append(d)
    return kept


# ── Backends ────────────────────────────────────────────────────────


def hatch(c, data, sha):
    build = table(data, "tool", "hatch", "build")
    wheel = table(build, "targets", "wheel")

    # A wheel-target option replaces the build-wide one, as hatchling reads them.
    def option(key):
        return str_list(wheel.get(key, build.get(key, [])))

    include, packages, only_include = option("include"), option("packages"), option("only-include")
    for path in packages + only_include:
        c.add(path)
    for pattern in include:
        c.add_gitignore(pattern)
    force_include = {**table(build, "force-include"), **table(wheel, "force-include")}
    for source in force_include:
        c.add(source)
    # Installed outside the package (the wheel's .data directory) or into its
    # .dist-info; wheel-target options only. Keys are repository sources.
    for key in ("shared-data", "shared-scripts", "extra-metadata"):
        for source in table(wheel, key):
            c.add(source)
    for hooks in (table(build, "hooks"), table(wheel, "hooks")):
        custom = hooks.get("custom")
        if isinstance(custom, dict):
            c.build_hook(custom.get("path", "hatch_build.py"), "hatchling")

    # Metadata read from files: the version source, and metadata hooks that
    # fill dynamic fields from files (hatch-requirements-txt,
    # hatch-fancy-pypi-readme). A hook that runs code, or one not known
    # here, may read files no configuration names.
    version = table(data, "tool", "hatch", "version")
    if version:
        source = version.get("source", "regex")
        path = version.get("path", "package.json" if source == "nodejs" else None)
        c.add(path)
        for directory in str_list(version.get("search-paths")):
            c.add(directory)
        if source == "code" and isinstance(path, str):
            scan_python(c, clean(path) or path)
        elif source not in KNOWN_HATCH_VERSION_SOURCES:
            c.unresolved(f"hatchling: version source {source!r} is not one the python preset knows, and may read files")
    for name, hook in table(data, "tool", "hatch", "metadata", "hooks").items():
        if name == "custom":
            path = hook.get("path", "hatch_build.py") if isinstance(hook, dict) else "hatch_build.py"
            c.add(path)
            c.unresolved(f"hatchling: custom metadata hook {path} runs code that may read files the preset cannot see")
            continue
        if name not in KNOWN_HATCH_METADATA_HOOKS:
            c.unresolved(f"hatchling: metadata hook {name!r} is not one the python preset knows, and may read files")
        # The known hooks take plain file paths; their other strings (readme
        # text, regex substitutions) are not paths, so only a tracked file counts.
        for value in strings(hook):
            if c.tree.is_file(clean(value) or ""):
                c.add(value)
    if include or packages or only_include:
        return

    # hatchling's default file selection: a package or module named after the
    # project at the root or under src/, or a single namespace package holding it.
    names = import_names(project_name(data))
    if discover_by_name(c, names):
        return
    for name in names:
        hits = [f for f in c.tree.files if re.fullmatch(rf"[^/]+/{re.escape(name)}/__init__\.py", f)]
        if len(hits) == 1:
            c.add(hits[0].split("/")[0])
            return
    if not force_include:
        c.unresolved("hatchling: no packages, include or only-include configured, and no package named after the project")


# Version sources whose inputs are the `path` file (and, for `code`,
# `search-paths`; for `nodejs`, package.json), or nothing in the repository:
# vcs (hatch-vcs), uv-dynamic-versioning and versioningit derive the version
# from git, env from the environment.
KNOWN_HATCH_VERSION_SOURCES = ("regex", "code", "nodejs", "vcs", "uv-dynamic-versioning", "versioningit", "env")

# Metadata hooks whose file inputs all appear as strings in their table.
KNOWN_HATCH_METADATA_HOOKS = ("requirements_txt", "fancy-pypi-readme")


class Find:
    """A setuptools ``find:`` / ``find_packages()`` specification."""

    def __init__(self, where=(".",), include=("*",), exclude=(), namespaces=False):
        self.where = str_list(list(where)) or ["."]
        self.include = str_list(list(include)) or ["*"]
        self.exclude = str_list(list(exclude))
        self.namespaces = namespaces

    def dirs(self, tree):
        found = []
        for where in self.where:
            base = clean(where)
            if base is None:
                continue
            for d in tree.dirs_under(base):
                parts = (posixpath.relpath(d, base) if base else d).split("/")
                if not all(part.isidentifier() for part in parts):
                    continue
                if not self.namespaces and not all(
                    tree.is_file(posixpath.join(base, *parts[: i + 1], "__init__.py")) for i in range(len(parts))
                ):
                    continue
                name = ".".join(parts)
                if any(fnmatch.fnmatchcase(name, p) for p in self.include) and not any(
                    fnmatch.fnmatchcase(name, p) for p in self.exclude
                ):
                    found.append(d)
        return minimal(found)


class SetuptoolsConfig:
    def __init__(self):
        self.packages = None  # None, a list of names, or a Find
        self.package_dir = {}
        self.py_modules = None
        self.sources = []
        self.data_files = []  # sources installed through data-files, globs allowed
        self.metadata_files = []  # files dynamic metadata is read from
        self.attr_modules = []  # modules `attr:` directives import a value from

    def package_path(self, name):
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self.package_dir:
                return posixpath.join(self.package_dir[prefix], *parts[i:])
        return posixpath.join(self.package_dir.get("", ""), *parts)


def read_setuptools_pyproject(cfg, data):
    st = table(data, "tool", "setuptools")
    packages = st.get("packages")
    if isinstance(packages, list):
        cfg.packages = str_list(packages)
    elif isinstance(packages, dict) and isinstance(packages.get("find"), dict):
        find = packages["find"]
        cfg.packages = Find(
            find.get("where", ["."]), find.get("include", ["*"]), find.get("exclude", []), find.get("namespaces", True)
        )
    if isinstance(st.get("package-dir"), dict):
        cfg.package_dir.update(st["package-dir"])
    if "py-modules" in st:
        cfg.py_modules = str_list(st["py-modules"])
    for ext in st.get("ext-modules", []) or []:
        if isinstance(ext, dict):
            cfg.sources += str_list(ext.get("sources"))
    for patterns in table(st, "data-files").values():
        cfg.data_files += str_list(patterns)

    # [tool.setuptools.dynamic]: {file = ...} and {attr = ...} at any depth
    # (optional-dependencies nests one table per extra).
    def directives(value):
        if isinstance(value, dict):
            if "file" in value:
                cfg.metadata_files += str_list(value["file"])
            if isinstance(value.get("attr"), str):
                cfg.attr_modules.append(value["attr"].rsplit(".", 1)[0])
            for item in value.values():
                if isinstance(item, dict):
                    directives(item)

    directives(table(st, "dynamic"))


def cfg_list(value):
    return [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]


def read_setup_cfg(cfg, text, c):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        c.unresolved(f"setup.cfg does not parse: {exc}")
        return
    # `file:` and `attr:` directives, in [metadata], [options] and the
    # [options.*] sections such as extras_require.
    for section in parser.sections():
        for value in parser[section].values():
            value = value.strip()
            if value.startswith("file:"):
                cfg.metadata_files += cfg_list(value[len("file:") :])
            elif value.startswith("attr:"):
                cfg.attr_modules.append(value[len("attr:") :].strip().rsplit(".", 1)[0])
    if not parser.has_section("options"):
        return
    options = parser["options"]
    if "package_dir" in options:
        for line in cfg_list(options["package_dir"]):
            key, _, value = line.partition("=")
            cfg.package_dir[key.strip()] = value.strip()
    packages = options.get("packages", "").strip()
    if packages in ("find:", "find_namespace:"):
        find = parser["options.packages.find"] if parser.has_section("options.packages.find") else {}
        cfg.packages = Find(
            cfg_list(find.get("where", ".")),
            cfg_list(find.get("include", "*")),
            cfg_list(find.get("exclude", "")),
            namespaces=packages == "find_namespace:",
        )
    elif packages:
        cfg.packages = cfg_list(packages)
    if "py_modules" in options:
        cfg.py_modules = cfg_list(options["py_modules"])
    if parser.has_section("options.data_files"):
        for value in parser["options.data_files"].values():
            cfg.data_files += cfg_list(value)


def call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class Anchored:
    """A path an expression builds, and what it is relative to.

    ``anchor`` is None for a bare relative path (resolved against both the
    repository root, the build's working directory, and the scanned file's
    directory), ``"root"`` for the working directory, ``"self"`` for the scanned
    file itself, or an int: the scanned file's directory ``n`` levels up.
    """

    def __init__(self, anchor, rel=""):
        self.anchor = anchor
        self.rel = rel

    def parent(self):
        if self.anchor == "self":
            return Anchored(0)
        if self.rel:
            return Anchored(self.anchor, posixpath.dirname(self.rel))
        if isinstance(self.anchor, int):
            return Anchored(self.anchor + 1)
        return None  # the parent of the working directory: outside the repository

    def join(self, other):
        if self.anchor == "self" or other.anchor is not None:
            return None  # joining onto a file, or an absolute/anchored right-hand side
        return Anchored(self.anchor, posixpath.join(self.rel, other.rel) if self.rel else other.rel)

    def candidates(self, here):
        if self.anchor == "self":
            return []
        if self.anchor is None:
            bases = list(dict.fromkeys(("", here)))
        elif self.anchor == "root":
            bases = [""]
        else:
            parts = here.split("/") if here else []
            if self.anchor > len(parts):
                return None
            bases = ["/".join(parts[: len(parts) - self.anchor])]
        out = []
        for base in bases:
            joined_path = posixpath.join(base, self.rel) if base else self.rel
            path = "" if joined_path in ("", ".") else clean(joined_path)  # "" is the repository root
            if path is None:
                return None
            out.append(path)
        return out


# Calls that read a file, and where the path is: ("arg", index, keywords) for
# an argument, ("receiver",) for the object the method is called on.
OPEN_MODULES = ("io", "codecs", "os")
READ_CALLS = {
    "read_text": ("receiver",),
    "read_bytes": ("receiver",),
    "run_path": ("arg", 0, ("path_name",)),
    "spec_from_file_location": ("arg", 1, ("location",)),
    "SourceFileLoader": ("arg", 1, ("path",)),
    "load_source": ("arg", 1, ("pathname",)),
}
IMPORT_CALLS = ("import_module", "__import__", "run_module")
SYS_PATH_CALLS = ("insert", "append", "addsitedir")


def argument(node, index, keywords):
    if index is not None and len(node.args) > index and not isinstance(node.args[index], ast.Starred):
        return node.args[index]
    return next((k.value for k in node.keywords if k.arg in keywords), None)


def is_write_only(mode):
    """True for a literal open() mode that only writes ("w", "a", "x"; not "+")."""
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and "r" not in mode.value
        and "+" not in mode.value
        and any(ch in mode.value for ch in "wax")
    )


def is_write_only_flags(flags):
    """True for os.open() flags that open for writing only: O_WRONLY, without O_RDWR."""
    names = {getattr(n, "attr", None) or getattr(n, "id", None) for n in ast.walk(flags)} if flags is not None else set()
    return "O_WRONLY" in names and "O_RDWR" not in names


def parse(text):
    """``ast.parse()``, without the SyntaxWarnings (e.g. invalid escapes) it prints
    to stderr, where the action would show each as a warning about the preset."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(text)


def scan_python(c, path, module=None, seen=None, imported=False):
    """Track what a Python file run at build time reads, or report that it cannot be told.

    The file is parsed, never run. Tracked: string constants naming a tracked
    file; paths composed with ``/``, ``os.path.join()``, ``Path()``, ``.parent``
    and ``dirname()``, anchored where they are anchored (the file's own
    directory for ``__file__``, the working directory otherwise); local modules
    imported statically, through a literal ``importlib.import_module()``, or
    from a directory added to ``sys.path`` (by whatever name ``sys`` is
    imported under), each scanned in turn. A name is
    followed only when it is bound exactly once in the file; a reader helper is
    followed through its call sites, except in a module reached by import
    (``imported``), whose functions any importer may call with anything. Anything else that reads a file or imports
    a module -- a path or name that cannot be worked out, a form not recognised
    -- is reported as unresolved, so the preset fails open rather than miss it.
    """
    seen = set() if seen is None else seen
    if path in seen:
        return
    seen.add(path)
    if module is None:
        text = git_show(c.sha, path)
        if text is None:
            return
        try:
            module = parse(text)
        except SyntaxError as exc:
            c.unresolved(f"{path} does not parse: {exc}")
            return
    here = posixpath.dirname(path)

    def unresolved(node, action, noun):
        c.unresolved(
            f"{path} {action} (line {node.lineno}) whose {noun} cannot be worked out without running it;"
            " add the files involved to 'paths'"
        )

    # Bindings: a name is trusted only when it is bound exactly once anywhere.
    bindings = {}
    values = {}
    for node in ast.walk(module):
        names = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names = [node.id]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names = list(node.names) * 2
        for name in names:
            bindings[name] = bindings.get(name, 0) + 1
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[node.target.id] = node.value

    def joined(node, env=None, depth=0):
        """The Anchored path an expression builds, or None when it cannot be told."""
        if depth > 30 or node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("/"):
                return None
            return Anchored(None, node.value)
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return Anchored("self")
            if env is not None and node.id in env:
                bound = env[node.id]
                return joined(bound[0], None, depth + 1) if len(bound) == 1 else None
            if bindings.get(node.id) == 1 and node.id in values:
                return joined(values[node.id], None, depth + 1)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = joined(node.left, env, depth + 1), joined(node.right, env, depth + 1)
            return left.join(right) if left and right else None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            inner = joined(node.value, env, depth + 1)
            return inner.parent() if inner else None
        if isinstance(node, ast.Call):
            name = call_name(node)
            if node.keywords and name not in ("Path", "PurePath", "PosixPath"):
                return None
            if name in ("join", "Path", "PurePath", "PosixPath"):
                parts = []
                for arg in node.args:
                    if isinstance(arg, ast.Starred):
                        if not (isinstance(arg.value, ast.Name) and env is not None and arg.value.id in env):
                            return None
                        parts += [joined(n, None, depth + 1) for n in env[arg.value.id]]
                    else:
                        parts.append(joined(arg, env, depth + 1))
                if any(part is None for part in parts):
                    return None
                if not parts:
                    return Anchored("root") if name != "join" else None
                result = parts[0]
                for part in parts[1:]:
                    result = result.join(part)
                    if result is None:
                        return None
                return result
            if name in ("str", "fspath") and len(node.args) == 1:
                return joined(node.args[0], env, depth + 1)
            if name in ("dirname", "abspath", "realpath", "normpath"):
                inner = joined(node.args[0], env, depth + 1) if len(node.args) == 1 else None
                if inner is None:
                    return None
                return inner.parent() if name == "dirname" else inner
            if name in ("resolve", "absolute") and isinstance(node.func, ast.Attribute) and not node.args:
                return joined(node.func.value, env, depth + 1)
            if name in ("getcwd", "cwd") and not node.args:
                return Anchored("root")
        return None

    def track(anchored):
        """Add the tracked files or directories a path names; True if it names any."""
        if anchored.anchor == "self":
            return True
        found = False
        for target in anchored.candidates(here) or []:
            if target and (c.tree.is_file(target) or c.tree.is_dir(target)):
                c.add(target)
                found = True
                if target.endswith(".py") and c.tree.is_file(target):
                    scan_python(c, target, seen=seen, imported=True)
        return found

    # Each node's enclosing top-level function, to follow reader helpers.
    enclosing = {}
    functions = {}
    for top in module.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[top.name] = top
            for node in ast.walk(top):
                enclosing[node] = top

    def parameters(func):
        args = func.args
        return [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs] + [
            a.arg for a in (args.vararg, args.kwarg) if a is not None
        ]

    def environments(node, target):
        """The bindings to resolve ``target`` under: one per call site of its function.

        [None] when the path does not depend on a parameter of the enclosing
        function. None when it does and the call sites cannot all be bound: the
        function is referenced other than by a direct call (an alias, a callback,
        a caller in another file), or a call passes arguments that cannot be
        bound. An empty list -- no call site -- counts as unbindable too, so a
        read is never accepted vacuously.
        """
        func = enclosing.get(node)
        params = parameters(func) if func is not None else []
        used = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} if target is not None else set()
        if func is None or not used & set(params):
            return [None]
        if imported:
            return None
        if bindings.get(func.name) != 1:
            return None
        loads = [n for n in ast.walk(module) if isinstance(n, ast.Name) and n.id == func.name and isinstance(n.ctx, ast.Load)]
        calls = [n for n in ast.walk(module) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == func.name]
        if not calls or len(loads) != len(calls):
            return None
        positional = [a.arg for a in func.args.posonlyargs + func.args.args]
        defaults = dict(zip(positional[len(positional) - len(func.args.defaults) :], func.args.defaults))
        defaults.update({a.arg: d for a, d in zip(func.args.kwonlyargs, func.args.kw_defaults) if d is not None})
        params = positional
        envs = []
        for call in calls:
            if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                return None
            env = {name: [value] for name, value in defaults.items()}
            for name, value in zip(params, call.args):
                env[name] = [value]
            if func.args.vararg:
                env[func.args.vararg.arg] = list(call.args[len(params) :])
            elif len(call.args) > len(params):
                return None
            for k in call.keywords:
                env[k.arg] = [k.value]
            if func.args.kwarg is not None:
                env.pop(func.args.kwarg.arg, None)
            envs.append(env)
        return envs

    # Directories added to sys.path, searched by later imports. Only a direct
    # sys.path.insert()/append() or site.addsitedir() is understood, through
    # whatever name `import sys`/`import site` binds; any other use of sys.path
    # -- an alias, slice assignment, +=, `from sys import path`, the module
    # itself passed around -- could add a directory the imports below would
    # miss, so it is reported.
    import_bases = ["", "src"]
    module_names = {"sys": set(), "site": set()}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in module_names:
                    module_names[a.name].add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module in module_names:
            names = {a.name for a in node.names}
            if names & {"path", "addsitedir", "*"}:
                unresolved(node, f"imports {node.module}.{'path' if node.module == 'sys' else 'addsitedir'}", "effect on imports")

    def is_sys_path(node):
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and getattr(node.value, "id", None) in module_names["sys"]
        )

    direct = {
        id(n.func.value)
        for n in ast.walk(module)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("insert", "append")
        and is_sys_path(n.func.value)
    }
    attribute_receivers = {id(n.value) for n in ast.walk(module) if isinstance(n, ast.Attribute)}
    for node in ast.walk(module):
        if is_sys_path(node) and id(node) not in direct:
            unresolved(node, "uses sys.path in a way", "effect on imports")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in module_names["sys"] | module_names["site"]
            and id(node) not in attribute_receivers
        ):
            unresolved(node, f"passes the module {node.id} around, which may reach sys.path", "effect on imports")
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and call_name(node) in SYS_PATH_CALLS and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if not (is_sys_path(receiver) or getattr(receiver, "id", None) in module_names["site"]):
            continue
        anchored = joined(node.args[-1]) if node.args else None
        targets = anchored.candidates(here) if anchored else None
        # "" is the repository root, which is always there.
        dirs = [t for t in targets or [] if t == "" or c.tree.is_dir(t)]
        if dirs:
            import_bases += [d for d in dirs if d not in import_bases]
        else:
            unresolved(node, "adds a directory to sys.path", "location")

    def import_module(name, bases):
        """Track and scan a local module and the packages above it; True if it is local."""
        parts = name.split(".")
        for base in bases:
            found = False
            for i in range(1, len(parts) + 1):
                stem_path = "/".join(parts[:i])
                stem = clean(posixpath.join(base, stem_path) if base else stem_path)
                # Importing a.b.c runs a/__init__.py and a/b/__init__.py first.
                for candidate in (f"{stem}/__init__.py", f"{stem}.py") if stem else ():
                    if c.tree.is_file(candidate):
                        c.add(candidate)
                        scan_python(c, candidate, seen=seen, imported=True)
                        found = True
                        break
            if found:
                return True
        return False

    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            target = clean(node.value)
            if target and c.tree.is_file(target):
                c.add(target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                bases = [posixpath.normpath(posixpath.join(here or ".", *[".."] * (node.level - 1)))]
                bases = ["" if b == "." else b for b in bases]
                names = [node.module or ""] + [f"{node.module}.{a.name}" if node.module else a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                bases, names = import_bases, [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                bases, names = import_bases, [a.name for a in node.names]
            for name in names:
                if name:
                    import_module(name, bases)
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)) or isinstance(node, (ast.Call, ast.Attribute)):
            # A composed path anywhere -- whatever it is passed to -- is tracked when it exists.
            anchored = joined(node)
            if anchored is not None and anchored.rel:
                track(anchored)
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)

        if name in IMPORT_CALLS:
            target = argument(node, 0, ("name", "mod_name"))
            literal = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) else None
            if literal is not None and literal.startswith("."):
                # Relative to the `package` argument, as importlib resolves it:
                # ".version" in package "scripts" is scripts.version, ".." goes up.
                package = argument(node, 1, ("package",)) if name == "import_module" else None
                package = package.value if isinstance(package, ast.Constant) and isinstance(package.value, str) else None
                level = len(literal) - len(literal.lstrip("."))
                parts = package.split(".") if package else []
                if not package or level - 1 > len(parts) - 1:
                    literal = None
                else:
                    stem = parts[: len(parts) - (level - 1)]
                    literal = ".".join(stem + ([literal[level:]] if literal[level:] else []))
            if literal is None:
                unresolved(node, "imports a module", "name")
            else:
                import_module(literal, import_bases)
            continue

        if name == "open":
            func = node.func
            module_name = getattr(func.value, "id", None) if isinstance(func, ast.Attribute) else None
            if isinstance(func, ast.Name) or module_name in OPEN_MODULES:
                target = argument(node, 0, ("file", "filename", "path"))
                mode = argument(node, 1, ("flags",) if module_name == "os" else ("mode",))
            else:
                target = func.value if isinstance(func, ast.Attribute) else None
                mode = argument(node, 0, ("mode",))
            if (is_write_only_flags(mode) if module_name == "os" else is_write_only(mode)):
                continue
        elif name in READ_CALLS:
            spec = READ_CALLS[name]
            if spec[0] == "receiver":
                target = node.func.value if isinstance(node.func, ast.Attribute) else None
            else:
                target = argument(node, spec[1], spec[2])
        else:
            continue

        envs = environments(node, target)
        resolved = bool(envs) and all(
            (anchored := joined(target, env)) is not None and track(anchored) for env in envs
        )
        if not resolved:
            unresolved(node, "reads a file", "path")


def read_setup_py(cfg, text, c):
    try:
        module = parse(text)
    except SyntaxError as exc:
        c.unresolved(f"setup.py does not parse: {exc}")
        return
    # The files setup.py reads and the local modules it imports.
    scan_python(c, "setup.py", module)
    calls = [n for n in ast.walk(module) if isinstance(n, ast.Call) and call_name(n) == "setup"]
    if not calls:
        return
    call = calls[0]

    # A keyword given a plain name is followed to its module-level assignment.
    assigned = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value

    def resolve(node):
        seen = set()
        while isinstance(node, ast.Name) and node.id in assigned and node.id not in seen:
            seen.add(node.id)
            node = assigned[node.id]
        return node

    def literal(node, what):
        try:
            return ast.literal_eval(resolve(node))
        except (ValueError, TypeError, SyntaxError):
            c.unresolved(f"setup.py: {what} is computed, so it cannot be read without running setup.py")
            return None

    if any(k.arg is None for k in call.keywords) or call.args:
        c.unresolved("setup.py: setup() takes arguments that cannot be read without running setup.py")
    keywords = {k.arg: k.value for k in call.keywords if k.arg}
    if "cmdclass" in keywords:
        c.unresolved("setup.py: cmdclass replaces build commands, which may add files the preset cannot see")

    if "packages" in keywords:
        node = resolve(keywords["packages"])
        if isinstance(node, ast.Call) and call_name(node) in ("find_packages", "find_namespace_packages"):
            args = {k.arg: k.value for k in node.keywords if k.arg}
            if node.args:
                args.setdefault("where", node.args[0])
            spec = {key: literal(args[key], f"{call_name(node)}({key}=...)") for key in args}
            if None not in spec.values():
                cfg.packages = Find(
                    str_list(spec.get("where", ".")),
                    spec.get("include", ("*",)),
                    spec.get("exclude", ()),
                    namespaces=call_name(node) == "find_namespace_packages",
                )
        else:
            value = literal(node, "packages")
            if value is not None:
                cfg.packages = str_list(list(value))
    if "package_dir" in keywords:
        value = literal(keywords["package_dir"], "package_dir")
        if isinstance(value, dict):
            cfg.package_dir.update(value)
    if "py_modules" in keywords:
        value = literal(keywords["py_modules"], "py_modules")
        if value is not None:
            cfg.py_modules = str_list(list(value))
    if "data_files" in keywords:
        value = literal(keywords["data_files"], "data_files")
        if value is not None:
            for entry in value if isinstance(value, (list, tuple)) else []:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    cfg.data_files += str_list(list(entry[1]))
                elif isinstance(entry, str):
                    cfg.data_files.append(entry)
    if "ext_modules" in keywords:
        found = False
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node) or ""
            if name.endswith("Extension"):
                sources = next((k.value for k in node.keywords if k.arg == "sources"), None)
                if sources is None and len(node.args) > 1:
                    sources = node.args[1]
                if sources is not None:
                    found = True
                    value = literal(sources, "an extension's sources")
                    if value is not None:
                        cfg.sources += str_list(list(value) if not isinstance(value, str) else value)
            elif name == "cythonize" and node.args:
                found = True
                value = literal(node.args[0], "cythonize()'s sources")
                if value is not None:
                    cfg.sources += str_list(value if isinstance(value, str) else list(value))
        if not found:
            c.unresolved("setup.py: ext_modules is set but no extension sources could be read")


def setuptools(c, data, sha):
    cfg = SetuptoolsConfig()
    read_setuptools_pyproject(cfg, data)
    for path, reader in (("setup.cfg", read_setup_cfg), ("setup.py", read_setup_py)):
        text = git_show(sha, path)
        if text is not None:
            reader(cfg, text, c)

    for source in cfg.sources + cfg.data_files + cfg.metadata_files:
        c.add_path_or_glob(source)
    for module_name in cfg.attr_modules:
        path = cfg.package_path(module_name)
        c.add(path)
        c.add(f"{path}.py")

    if cfg.packages is None and cfg.py_modules is None:
        # setuptools auto-discovery: src-layout when the package root exists,
        # otherwise flat-layout minus the names it reserves for non-packages.
        root = cfg.package_dir.get("", "src")
        if root and c.tree.is_dir(clean(root) or ""):
            c.add(root)
            return
        found = False
        for d in c.tree.dirs_under(""):
            if "/" in d or not d.isidentifier():
                continue
            if not any(fnmatch.fnmatchcase(d, p) for p in FLAT_LAYOUT_EXCLUDED_PACKAGES):
                c.add(d)
                found = True
        for f in c.tree.files:
            stem = f[:-3]
            if "/" in f or not f.endswith(".py") or not stem.isidentifier():
                continue
            if not any(fnmatch.fnmatchcase(stem, p) for p in FLAT_LAYOUT_EXCLUDED_MODULES):
                c.add(f)
                found = True
        if not found and not cfg.sources:
            c.unresolved("setuptools: no packages configured and none found by auto-discovery")
        return

    if isinstance(cfg.packages, Find):
        dirs = cfg.packages.dirs(c.tree)
        if not dirs and not cfg.py_modules and not cfg.sources:
            c.unresolved(f"setuptools: package discovery under {', '.join(cfg.packages.where)} matched no package directories")
        for d in dirs:
            c.add(d)
    elif cfg.packages:
        for d in minimal(cfg.package_path(name) for name in cfg.packages):
            c.add(d)
    for name in cfg.py_modules or []:
        c.add(f"{cfg.package_path(name)}.py")


def poetry(c, data, sha):
    tool = table(data, "tool", "poetry")

    def for_wheel(entry):
        formats = str_list(entry.get("format", ["sdist", "wheel"])) if isinstance(entry, dict) else ["wheel"]
        return "wheel" in formats

    packages = tool.get("packages")
    if isinstance(packages, list):
        for entry in packages:
            if isinstance(entry, dict) and isinstance(entry.get("include"), str) and for_wheel(entry):
                c.add_path_or_glob(posixpath.join(entry.get("from", ""), entry["include"]))
    for entry in tool.get("include", []) or []:
        path = entry if isinstance(entry, str) else entry.get("path") if isinstance(entry, dict) else None
        if path and for_wheel(entry):
            c.add_path_or_glob(path)
    for readme in str_list(tool.get("readme")):
        c.add(readme)
    build = tool.get("build")
    script = build if isinstance(build, str) else build.get("script") if isinstance(build, dict) else None
    if script:
        c.build_hook(script, "poetry")
    if not packages and not discover_by_name(c, import_names(project_name(data))):
        c.unresolved("poetry: no packages configured and no package named after the project")


def flit(c, data, sha):
    module = lookup(data, ("tool", "flit", "module", "name"))
    if not isinstance(module, str):
        module = (project_name(data) or "").replace("-", "_")
    external = lookup(data, ("tool", "flit", "external-data", "directory"))
    if isinstance(external, str):
        c.add(external)
    if not module or not discover_by_name(c, [module.replace(".", "/")]):
        c.unresolved("flit: no module named after the project")


def pdm(c, data, sha):
    build = table(data, "tool", "pdm", "build")
    version = table(data, "tool", "pdm", "version")
    source = version.get("source")
    if source == "file":
        c.add(version.get("path"))
    elif source == "call":
        # The getter is imported from the project root; scan its module.
        getter = version.get("getter")
        module = getter.split(":")[0] if isinstance(getter, str) else ""
        stem = module.replace(".", "/")
        for path in (f"{stem}.py", f"{stem}/__init__.py", f"src/{stem}.py", f"src/{stem}/__init__.py"):
            if module and c.tree.is_file(path):
                c.add(path)
                scan_python(c, path)
                break
        else:
            c.unresolved(f"pdm-backend: version getter {getter!r} not found in the repository")
    elif source not in (None, "scm"):
        c.unresolved(f"pdm-backend: version source {source!r} is not one the python preset knows, and may read files")
    hook = build.get("custom-hook", "pdm_build.py")
    if c.tree.is_file(clean(hook) or ""):
        c.build_hook(hook, "pdm-backend")
    # wheel-data: files installed through the wheel's .data directory.
    for entries in table(build, "wheel-data").values():
        for entry in entries if isinstance(entries, list) else []:
            c.add_path_or_glob(entry.get("path") if isinstance(entry, dict) else entry)
    includes = str_list(build.get("includes"))
    for path in includes:
        c.add_path_or_glob(path)
    if includes:
        return
    package_dir = build.get("package-dir", "src" if c.tree.is_dir("src") else "")
    if package_dir:
        c.add(package_dir)
    elif not discover_by_name(c, import_names(project_name(data)), roots=("",)):
        c.unresolved("pdm-backend: no includes configured and no package named after the project")


def uv(c, data, sha):
    backend = table(data, "tool", "uv", "build-backend")
    root = backend.get("module-root", "src")
    names = str_list(backend.get("module-name")) or [re.sub(r"[-.]+", "_", (project_name(data) or "").lower())]
    for name in names:
        path = posixpath.join(root, name.replace(".", "/")) if root else name.replace(".", "/")
        c.add(path)
        c.add(f"{path}.py")
    for directory in table(backend, "data").values():
        c.add(directory)


def cargo_manifest(c, crate, sha):
    """A crate's parsed Cargo.toml, {} when there is none, None when it does not parse."""
    path = posixpath.join(crate, "Cargo.toml") if crate else "Cargo.toml"
    text = git_show(sha, path)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        c.unresolved(f"maturin: {path} does not parse, so its path dependencies are unknown: {exc}")
        return None


def cargo_paths(c, crate, tables):
    """Add the ``path`` of every dependency in ``tables``; the crates they name."""
    found = []
    for deps in tables:
        for dep in deps.values() if isinstance(deps, dict) else ():
            if isinstance(dep, dict) and isinstance(dep.get("path"), str):
                target = clean(posixpath.join(crate, dep["path"]))
                if target is None:
                    c.unresolved(f"maturin: path dependency {dep['path']!r} of {crate or '.'} leaves the repository")
                elif target not in found:
                    c.add(target)
                    found.append(target)
    return found


def cargo_workspace_paths(c, root, sha):
    """Path entries a workspace manifest shares with its members: [workspace.dependencies], [patch.*]."""
    manifest = cargo_manifest(c, root, sha)
    if manifest:
        tables = [table(manifest, "workspace", "dependencies"), *table(manifest, "patch").values()]
        for target in cargo_paths(c, root, tables):
            cargo_path_dependencies(c, target, sha, set())


def cargo_path_dependencies(c, crate, sha, seen):
    """Track the local crates a crate builds against, through ``path`` dependencies.

    Dev-dependencies are left out: they build tests, not the extension.
    """
    if crate in seen:
        return
    seen.add(crate)
    manifest = cargo_manifest(c, crate, sha)
    if not manifest:
        return
    tables = [manifest.get("dependencies"), manifest.get("build-dependencies")]
    for target in table(manifest, "target").values():
        if isinstance(target, dict):
            tables += [target.get("dependencies"), target.get("build-dependencies")]
    for dep in cargo_paths(c, crate, tables):
        cargo_path_dependencies(c, dep, sha, seen)


def maturin(c, data, sha):
    tool = table(data, "tool", "maturin")
    manifest = tool.get("manifest-path", "Cargo.toml")
    crate = posixpath.dirname(manifest) if isinstance(manifest, str) else ""
    if crate:
        c.add(crate)
        # Cargo looks upwards from the crate for the workspace it belongs to,
        # whose manifest and lock file then shape the build.
        parent = crate
        while parent:
            parent = posixpath.dirname(parent)
            for name in ("Cargo.toml", "Cargo.lock"):
                path = posixpath.join(parent, name) if parent else name
                if c.tree.is_file(path):
                    c.add(path)
                    if name == "Cargo.toml":
                        cargo_workspace_paths(c, parent, sha)
    else:
        # The crate at the repository root: its manifest, lock file, build
        # script and Rust sources.
        for path in ("Cargo.toml", "Cargo.lock", "build.rs", "src"):
            c.add(path)
        cargo_workspace_paths(c, "", sha)
    cargo_path_dependencies(c, crate, sha, set())
    module = tool.get("module-name")
    if not isinstance(module, str):
        module = re.sub(r"[-.]+", "_", project_name(data) or "")
    python_source = tool.get("python-source", "")
    if python_source:
        c.add(python_source)
    elif module:
        top = module.split(".")[0]
        c.add(top)
        c.add(f"{top}.pyi")
    for entry in tool.get("include", []) or []:
        path = entry if isinstance(entry, str) else entry.get("path") if isinstance(entry, dict) else None
        c.add_path_or_glob(path)
    # The directory installed through the wheel's .data directory.
    c.add(tool.get("data"))


def scikit_build(c, data, sha):
    wheel = table(data, "tool", "scikit-build", "wheel")
    packages = wheel.get("packages")
    if isinstance(packages, dict):
        packages = list(packages.values())
    if isinstance(packages, list) and packages:
        for path in str_list(packages):
            c.add(path)
    else:
        discover_by_name(c, import_names(project_name(data)), roots=("src", "python", ""))
    c.add_glob("**/CMakeLists.txt")
    c.add_glob("**/*.cmake")
    c.unresolved("scikit-build-core: the native sources CMake compiles are not declared in pyproject.toml")


def meson(c, data, sha):
    c.add_glob("**/meson.build")
    c.add("meson.options")
    c.add("meson_options.txt")
    c.unresolved("meson-python: the sources meson builds are not declared in pyproject.toml")


BACKENDS = {
    "hatchling.build": hatch,
    "setuptools.build_meta": setuptools,
    "setuptools.build_meta:__legacy__": setuptools,
    "flit_core.buildapi": flit,
    "flit.buildapi": flit,
    "pdm.backend": pdm,
    "pdm.pep517.api": pdm,
    "poetry.core.masonry.api": poetry,
    "poetry.masonry.api": poetry,
    "maturin": maturin,
    "scikit_build_core.build": scikit_build,
    "mesonpy": meson,
    "uv_build": uv,
}


# ── Commands ────────────────────────────────────────────────────────


def material_view(data):
    view = {keys: lookup(data, keys) for keys in MATERIAL_KEYS}
    poetry_table = lookup(data, ("tool", "poetry"))
    if isinstance(poetry_table, dict):
        view["poetry"] = {k: v for k, v in poetry_table.items() if k not in POETRY_DEV_ONLY}
    return view


def load_hatch_toml(sha):
    text = git_show(sha, HATCH_TOML)
    return None if text is None else {"tool": {"hatch": tomllib.loads(text)}}


def material(base_sha, head_sha, path=None):
    """Whether the packaging tables differ: of ``path`` alone (pyproject.toml or
    hatch.toml) when given, so a tooling-only edit to one is not carried by a
    build edit to the other; of both merged otherwise."""
    if path == HATCH_TOML:
        base, head = load_hatch_toml(base_sha), load_hatch_toml(head_sha)
    else:
        merge = path is None
        base, head = load(base_sha, merge), load(head_sha, merge)
    if base is None or head is None:
        # Added or removed outright: that changes how the package is built.
        return base is not head
    return material_view(base) != material_view(head)


def declared_paths(c, data):
    project = table(data, "project")
    readme = project.get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    c.add(readme)
    license_ = project.get("license")
    if isinstance(license_, dict):
        c.add(license_.get("file"))
    # PEP 639 globs; `**` matches across directories, as git's glob magic does.
    for pattern in str_list(project.get("license-files")) + DEFAULT_LICENSE_GLOBS:
        c.add_glob(pattern)


def package_paths(sha):
    c = Collector(Tree(sha), sha)
    try:
        data = load(sha)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        c.unresolved(f"{PYPROJECT} or {HATCH_TOML} at {sha[:12]} does not parse: {exc}")
        return c
    data = data or {}
    if not data and not any(c.tree.is_file(f) for f in ("setup.py", "setup.cfg")):
        c.unresolved(f"no {PYPROJECT}, setup.py or setup.cfg at {sha[:12]}")
        return c
    declared_paths(c, data)
    # Without a [build-system] table, build frontends fall back to setuptools.
    backend = lookup(data, ("build-system", "build-backend"))
    if backend is _MISSING:
        setuptools(c, data, sha)
    elif backend in BACKENDS:
        BACKENDS[backend](c, data, sha)
    else:
        c.unresolved(f"build backend {backend!r} is not one the python preset knows")
    return c


def main(argv):
    if len(argv) not in (4, 5) or argv[1] not in ("material", "package-paths") or (len(argv) == 5 and argv[1] != "material"):
        print(__doc__, file=sys.stderr)
        return 2
    command, base_sha, head_sha = argv[1:4]
    if command == "material":
        try:
            return 0 if material(base_sha, head_sha, argv[4] if len(argv) == 5 else None) else 1
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            print(f"Cannot parse {PYPROJECT} or {HATCH_TOML}: {exc}", file=sys.stderr)
            return 2

    paths, problems = [], []
    for sha in dict.fromkeys((base_sha, head_sha)):
        c = package_paths(sha)
        paths += [p for p in c.paths if p not in paths]
        problems += [p for p in c.problems if p not in problems]
    for path in paths:
        print(path)
    for problem in problems:
        print(problem, file=sys.stderr)
    return UNRESOLVED if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
