"""Packaging-aware inspection of a Python project for the ``python`` preset.

Usage::

    python_packaging.py material <base-sha> <head-sha>
        Exit 0 if pyproject.toml differs between the revisions in a table that
        reaches the built distribution, 1 if it does not, 2 if either revision
        cannot be read or parsed (the caller treats that as material).

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

PYPROJECT = "pyproject.toml"
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


def load(sha):
    """Parse pyproject.toml at a revision; None when the file does not exist there."""
    text = git_show(sha, PYPROJECT)
    return None if text is None else tomllib.loads(text)


class Tree:
    """The tracked files of a revision, for existence checks without a checkout."""

    def __init__(self, sha):
        out = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", sha], capture_output=True, check=True
        ).stdout.decode("utf-8")
        self.files = {f for f in out.split("\0") if f}
        self.dirs = set()
        for f in self.files:
            parent = posixpath.dirname(f)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)

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
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


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


class Collector:
    """The pathspecs found for one revision and the reasons any were missed."""

    def __init__(self, tree):
        self.tree = tree
        self.paths = []
        self.problems = []

    def _append(self, spec):
        if spec not in self.paths:
            self.paths.append(spec)

    def add(self, path):
        """A file or directory; a git pathspec without wildcards matches both."""
        path = clean(path)
        if path:
            self._append(f":(literal){path}")

    def add_glob(self, pattern):
        pattern = clean(pattern)
        if pattern:
            self._append(f":(glob){pattern}")
            self._append(f":(glob){pattern}/**")

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


def cfg_list(value):
    return [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]


def read_setup_cfg(cfg, text, c):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        c.unresolved(f"setup.cfg does not parse: {exc}")
        return
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


def read_setup_py(cfg, text, c):
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        c.unresolved(f"setup.py does not parse: {exc}")
        return
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
                    [spec.get("where", ".")],
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

    for source in cfg.sources + cfg.data_files:
        c.add_path_or_glob(source)

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
        for d in cfg.packages.dirs(c.tree):
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


def maturin(c, data, sha):
    tool = table(data, "tool", "maturin")
    manifest = tool.get("manifest-path", "Cargo.toml")
    crate = posixpath.dirname(manifest) if isinstance(manifest, str) else ""
    if crate:
        c.add(crate)
    else:
        # The crate at the repository root: its manifest, lock file, build
        # script and Rust sources.
        for path in ("Cargo.toml", "Cargo.lock", "build.rs", "src"):
            c.add(path)
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


def material(base_sha, head_sha):
    base, head = load(base_sha), load(head_sha)
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
    for pattern in str_list(project.get("license-files")):
        c._append(f":(glob){pattern}")


def package_paths(sha):
    c = Collector(Tree(sha))
    try:
        data = load(sha)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        c.unresolved(f"{PYPROJECT} at {sha[:12]} does not parse: {exc}")
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
    if len(argv) != 4 or argv[1] not in ("material", "package-paths"):
        print(__doc__, file=sys.stderr)
        return 2
    command, base_sha, head_sha = argv[1:]
    if command == "material":
        try:
            return 0 if material(base_sha, head_sha) else 1
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            print(f"Cannot parse {PYPROJECT}: {exc}", file=sys.stderr)
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
