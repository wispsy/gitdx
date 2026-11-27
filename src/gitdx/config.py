"""Ordered local Git configuration with explicit, non-executing policy."""

from dataclasses import dataclass, field
from pathlib import Path
import re

from .errors import UnsupportedFormat
from .objects import ObjectFormat


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    section: str
    subsection: str | None
    name: str
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class Config:
    entries: tuple[ConfigEntry, ...] = ()

    def get_all(self, section, name, *, subsection=None):
        return tuple(e.value for e in self.entries if
                     (e.section, e.subsection, e.name) == (section.lower(), subsection, name.lower()))

    def get(self, section, name, default=None, *, subsection=None):
        values = self.get_all(section, name, subsection=subsection)
        return values[-1] if values else default

    def boolean(self, section, name, default=False, *, subsection=None):
        values = self.get_all(section, name, subsection=subsection)
        if not values:return default
        value = values[-1]
        if value is None:return True
        value = value.lower()
        if value in ("true", "yes", "on"):return True
        if value in ("false", "no", "off", ""):return False
        if re.fullmatch(r"[+-]?[0-9]+[kmg]?", value):
            number = value.rstrip("kmg")
            if len(number)>20:raise UnsupportedFormat("configuration boolean integer exceeds its limit")
            return int(number) != 0
        raise UnsupportedFormat("invalid configuration boolean")


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    version: int
    bare: bool
    object_format: ObjectFormat
    filemode: bool = True
    symlinks: bool = True
    values: Config = field(default_factory=Config, repr=False)

    @property
    def ref_storage(self):return (self.values.get('extensions','refstorage','files') or '').lower()


def _value(lines, index, text):
    result = [];pending = "";quoted = False;cursor = 0
    while True:
        if cursor == len(text):
            if quoted:raise UnsupportedFormat("unterminated configuration quote")
            return "".join(result), index
        char = text[cursor];cursor += 1
        if char in "#;" and not quoted:
            return "".join(result), index
        if char == '"':
            result.append(pending);pending = "";quoted = not quoted
        elif char == "\\":
            if cursor == len(text):
                index += 1
                if index >= len(lines):raise UnsupportedFormat("incomplete configuration continuation")
                text = lines[index];cursor = 0
                continue
            escape = text[cursor];cursor += 1
            mapping = {"n":"\n", "t":"\t", "b":"\b", '"':'"', "\\":"\\"}
            if escape not in mapping:raise UnsupportedFormat("invalid configuration escape")
            result.extend((pending,mapping[escape]));pending = ""
        elif char in " \t" and not quoted:pending += char
        else:
            result.extend((pending,char));pending = ""


def parse_values(data: bytes, *, max_entries=10_000) -> Config:
    """Parse bytes only. Includes are recorded, never followed by this parser."""
    if not isinstance(data, bytes):raise TypeError("configuration must be immutable bytes")
    if type(max_entries) is not int or max_entries<=0:raise ValueError("configuration entry limit must be positive")
    if len(data)>1024*1024:raise UnsupportedFormat("configuration exceeds its byte limit")
    try:text = data.decode("utf-8-sig")
    except UnicodeError as error:raise UnsupportedFormat("configuration must be UTF-8 in this profile") from error
    if "\0" in text:raise UnsupportedFormat("NUL in configuration")
    lines = text.replace("\r\n","\n").split("\n");entries = [];section = subsection = None;index = 0
    while index<len(lines):
        line = lines[index].lstrip(" \t")
        if not line or line.startswith(("#",";")):
            index += 1;continue
        if line.startswith("["):
            match = re.match(r'\[([A-Za-z0-9.-]+)(?:[ \t]+"((?:[^"\\]|\\.)*)")?[ \t]*\]', line)
            if not match:raise UnsupportedFormat("invalid configuration section")
            section, subsection = match[1].lower(), match[2]
            if subsection is not None:subsection = re.sub(r"\\(.)", r"\1", subsection)
            elif "." in section:section,subsection = section.split(".",1)
            line = line[match.end():].lstrip(" \t")
            if not line or line.startswith(("#",";")):
                index += 1;continue
        if section is None:raise UnsupportedFormat("configuration variable outside a section")
        match = re.fullmatch(r'([A-Za-z][A-Za-z0-9-]*)(?:[ \t]*=[ \t]*(.*)|[ \t]*(?:[#;].*)?)',line)
        if not match:raise UnsupportedFormat("invalid configuration variable")
        name, raw = match[1].lower(),match[2]
        if raw is None:value = None
        else:value,index = _value(lines,index,raw)
        entries.append(ConfigEntry(section,subsection,name,value))
        if len(entries)>max_entries:raise UnsupportedFormat("configuration entry count exceeded")
        index += 1
    return Config(tuple(entries))


def read_config(path, *, root, includes=False, max_depth=8) -> Config:
    """Inspect explicitly selected local files; never import ambient config.

    Includes, when selected, must be relative, beneath root, without parent
    components or filesystem redirects. Repository mutation does not select this
    reader: its config snapshot remains exactly the local config file.
    """
    from .filesystem import read_file
    root = Path(root).absolute();path = Path(path).absolute();total = 0;entries = []
    def visit(path, active):
        nonlocal total
        if path in active or len(active)>max_depth:raise UnsupportedFormat("configuration include cycle/depth exceeded")
        if not path.is_relative_to(root):raise UnsupportedFormat("configuration include leaves selected root")
        data = read_file(root,path,1024*1024);total += len(data)
        if total>1024*1024:raise UnsupportedFormat("configuration include byte budget exceeded")
        for entry in parse_values(data).entries:
            entries.append(entry)
            if len(entries)>10_000:raise UnsupportedFormat("configuration include entry budget exceeded")
            if entry.section == "includeif":raise UnsupportedFormat("conditional configuration includes are unsupported")
            if entry.section != "include":continue
            if not includes:raise UnsupportedFormat("configuration includes were not selected")
            if entry.subsection is not None or entry.name!="path" or not entry.value:
                raise UnsupportedFormat("unsupported configuration include")
            relative = Path(entry.value)
            if relative.is_absolute() or ".." in relative.parts or entry.value.startswith("~") or "\\" in entry.value or ":" in entry.value:
                raise UnsupportedFormat("configuration include must be a confined relative path")
            visit(path.parent/relative,(*active,path))
    if type(max_depth) is not int or max_depth<0:raise ValueError("include depth must be nonnegative")
    visit(path,())
    return Config(tuple(entries))


def parse_config(data: bytes) -> RepositoryConfig:
    values = parse_values(data)
    for entry in values.entries:
        if entry.section in ("include","includeif"):
            raise UnsupportedFormat("configuration includes require a separately selected reader; repository operations refuse them")
        if entry.section in ("core","extensions") and entry.subsection is not None:
            raise UnsupportedFormat("subsections of core/extensions are unsupported")
    version = values.get("core","repositoryformatversion","0")
    if version not in ("0","1"):raise UnsupportedFormat("unsupported repository format version")
    extensions = {e.name:e.value for e in values.entries if e.section=="extensions"}
    if any(k not in ("objectformat","refstorage") for k in extensions):raise UnsupportedFormat("unsupported required repository extension")
    if extensions and version!="1":raise UnsupportedFormat("repository extensions require format version 1")
    if (extensions.get("refstorage","files") or "").lower() not in ('files','reftable'):raise UnsupportedFormat("unsupported reference storage")
    try:format = ObjectFormat((extensions.get("objectformat","sha1") or "").lower())
    except ValueError as error:raise UnsupportedFormat("unsupported repository object format") from error
    if values.get_all("core","worktree") or values.boolean("core","sparsecheckout"):
        raise UnsupportedFormat("custom worktrees and sparse checkout are unsupported")
    return RepositoryConfig(int(version),values.boolean("core","bare"),format,
                            values.boolean("core","filemode",True),values.boolean("core","symlinks",True),values)
