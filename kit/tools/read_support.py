"""Shared read-only JSON, path and Git support for supported kit contracts."""

import json
import math
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


class Problem(Exception):
    def __init__(self, where, message, code=1):
        self.where, self.message, self.code = where, message, code


def bad(where, message):
    raise Problem(where, message)


def unsupported(where, message):
    raise Problem(where, message, 2)


def emit(message):
    # Keep normal Unicode readable and escape only text this stream cannot encode.
    encoding = sys.stdout.encoding or "utf-8"
    print(str(message).encode(encoding, errors="backslashreplace").decode(encoding))


def git_environment():
    # Never inherit caller config injection, tracing, discovery or object overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
    return env


def obj(value, where, required, optional=()):
    if not isinstance(value, dict):
        bad(where, "ожидается объект")
    missing = set(required) - value.keys()
    if missing:
        bad(where, "нет обязательных полей: " + ", ".join(sorted(missing)))
    extra = value.keys() - set(required) - set(optional)
    if extra:
        unsupported(where, "неизвестные поля: " + ", ".join(sorted(extra)))
    return value


def string(value, where):
    if not isinstance(value, str) or not value.strip():
        bad(where, "ожидается непустая строка")
    return value


def strings(value, where, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        bad(where, "ожидается " + ("непустой " if nonempty else "") + "список строк")
    for i, item in enumerate(value):
        string(item, f"{where}/{i}")
    if len(value) != len(set(value)):
        bad(where, "повтор значения")
    return value


def revision(value, where):
    if type(value) is not int or value < 1:
        bad(where, "редакция должна быть положительным целым числом")


def equal(actual, expected, where):
    if type(actual) is not type(expected) or actual != expected:
        bad(where, f"ожидалось {expected!r}, записано {actual!r}")


def number(value, where):
    if (type(value) not in (int, float, Decimal) or
            (type(value) is Decimal and not value.is_finite()) or
            (type(value) is float and not math.isfinite(value)) or value < 0):
        bad(where, "нужно неотрицательное конечное число")


def canonical(value):
    # Typed structural comparison avoids lossy float conversion or serializing
    # exact decimal numbers as strings. Container types and bool/int stay distinct.
    if isinstance(value, dict):
        return (dict, tuple((k, canonical(v)) for k, v in sorted(value.items())))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(canonical(v) for v in value))
    return (type(value), value)


def parse(data, where):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                bad(where, f"повтор ключа JSON: {key}")
            result[key] = value
        return result

    def constant(value):
        bad(where, f"недопустимое число JSON: {value}")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant,
                          parse_float=Decimal)
    except (ValueError, UnicodeError, RecursionError, InvalidOperation) as error:
        bad(where, f"нечитаемый JSON: {error}")


class BaseChecker:
    def __init__(self, root, state):
        root_path = Path(root).absolute()
        # Reject aliases before resolving: a symlink root must not expand authority.
        for part in [root_path, *root_path.parents]:
            if part.is_symlink():
                bad("root", "символическая ссылка в корне или его родителях")
        self.root = root_path.resolve(strict=True)
        if not self.root.is_dir():
            bad("root", "корень не является каталогом")
        self.state_name = state
        self.ids = set()
        self.infos = []

    def local(self, value, where, must_exist=True):
        string(value, where)
        p = PurePosixPath(value)
        if (p.is_absolute() or value != p.as_posix() or
                any(part in ("..", ".git") for part in p.parts) or
                any(c in value for c in ("\\", ":", "\n", "\r", "\0"))):
            bad(where, "нужен нормализованный относительный путь внутри корня, без .git")
        current = self.root
        for part in p.parts:
            current = current / part
            if current.is_symlink():
                bad(where, "символические ссылки не поддерживаются; чтение запрещено")
        if not current.resolve().is_relative_to(self.root):
            bad(where, "путь выходит за допустимый корень")
        if must_exist and not current.exists():
            bad(where, f"отсутствует источник: {value}")
        return current

    def file(self, value, where):
        path = self.local(value, where)
        if not path.is_file():
            unsupported(where, "поддерживаются только обычные файлы, без каталогов")
        return path

    def ident(self, value, where):
        string(value, where)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", value):
            bad(where, "идентификатор: латинская буква, затем буквы, цифры, точка, _ или -")
        if value in self.ids:
            bad(where, f"повтор идентификатора: {value}")
        self.ids.add(value)

    def git(self, *args):
        command = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                   "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                   "--git-dir=" + str(self.root / ".git"),
                   "--work-tree=" + str(self.root), *args]
        try:
            result = subprocess.run(command, cwd=self.root, env=git_environment(), capture_output=True,
                                    timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            bad("Git", f"не удалось прочитать Git: {error}")
        if result.returncode:
            # Do not print content from repository configuration or arbitrary paths.
            bad("Git", f"операция чтения {args[0]} не выполнена (код {result.returncode})")
        return result.stdout

    def config_keys(self, data):
        # Explicit non-repository GIT_DIR prevents setup from discovering/reading the
        # target config BEFORE --no-includes takes effect. Parse only supplied bytes.
        command = ["git", "--no-pager", "--no-optional-locks", "--git-dir=" + os.devnull,
                   "config", "--file", "-", "--no-includes", "--null", "--name-only", "--list"]
        try:
            result = subprocess.run(command, input=data, cwd=self.root, env=git_environment(),
                                    capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            bad(".git/config", f"не удалось разобрать конфигурацию Git: {type(error).__name__}")
        if result.returncode:
            # Git stderr can contain arbitrary config values and outside paths.
            bad(".git/config", f"неверная конфигурация или ошибка её разбора Git (код {result.returncode})")
        return result.stdout.split(b"\0")[:-1]

    def repository(self):
        meta = self.root / ".git"
        if meta.is_symlink():
            bad(".git", "символическая ссылка запрещена")
        if not meta.is_dir():
            if meta.is_file():
                unsupported(".git", "связанные рабочие копии и gitfile пока не поддерживаются")
            bad(".git", "отсутствует локальный Git-репозиторий")
        # Git can otherwise follow metadata links and include external config files.
        for parent, dirs, files in os.walk(meta, followlinks=False):
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    bad(".git", "символическая ссылка в служебных данных Git")
        for name in ("commondir", "objects/info/alternates", "info/grafts"):
            if (meta / name).exists():
                unsupported(".git/" + name, "внешнее/изменённое хранилище Git не поддерживается")
        config = meta / "config"
        if not config.is_file():
            bad(".git/config", "отсутствует конфигурация Git")
        for key in self.config_keys(config.read_bytes()):
            parts = key.lower().split(b".")
            if (parts[0] in (b"include", b"includeif", b"extensions", b"worktree") or
                    key.lower() == b"core.worktree" or
                    (parts[0] == b"remote" and parts[-1] in (b"promisor", b"partialclonefilter"))):
                unsupported(".git/config", "include/includeIf, worktree, расширения и partial clone Git не поддерживаются")
        equal(self.git("rev-parse", "--is-bare-repository").strip(), b"false", ".git")

    def paths(self, values, where):
        strings(values, where, nonempty=True)
        for i, path in enumerate(values):
            self.file(path, f"{where}/{i}")

    def tree(self, ref):
        entries = {}
        for entry in self.git("ls-tree", "-rz", "--full-tree", ref).split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                entries[os.fsdecode(name)] = tuple(metadata.split())
        return entries

