"""Conservative local repository config parsing, without includes or commands."""

from dataclasses import dataclass
import re

from .errors import UnsupportedFormat
from .objects import ObjectFormat


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    version: int
    bare: bool
    object_format: ObjectFormat
    filemode: bool = True
    symlinks: bool = True


def parse_config(data: bytes) -> RepositoryConfig:
    """Read the local core/extensions subset; reject syntax we cannot interpret.

    Recognizes section/variable case folding, quoted subsection names, unquoted
    values, inline comments, bare boolean keys and repeated last-value keys.
    Includes, multiline/quoted/escaped values are deliberately unsupported.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise UnsupportedFormat("repository config must be UTF-8 in the current profile") from error
    values: dict[tuple[str, str], str] = {}
    section = None
    subsection = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            match = re.fullmatch(r'\[([A-Za-z0-9.-]+)(?: "([^"\\]*)")?\]\s*(?:[#;].*)?', line)
            if not match:
                raise UnsupportedFormat("unsupported repository config section syntax")
            section, subsection = match[1].lower(), match[2]
            if section in ("include", "includeif"):
                raise UnsupportedFormat("repository config includes are not supported yet")
            if section in ("core", "extensions") and subsection is not None:
                raise UnsupportedFormat("subsections of core/extensions are unsupported")
            continue
        if section is None:
            raise UnsupportedFormat("repository config variable outside a section")
        # Do not approximate quoted/escaped values, even in unrelated sections.
        if '"' in line or "\\" in line:
            raise UnsupportedFormat("quoted, escaped or continued config values are unsupported")
        line = re.split(r"[#;]", line, maxsplit=1)[0].strip()
        key, equals, value = line.partition("=")
        key = key.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", key):
            raise UnsupportedFormat("unsupported repository config variable syntax")
        if section in ("core", "extensions"):
            values[(section, key)] = value.strip() if equals else "true"

    def boolean(key: str, default: str = "false") -> bool:
        value = values.get(("core", key), default).lower()
        if value in ("true", "yes", "on", "1"):
            return True
        if value in ("false", "no", "off", "0", ""):
            return False
        raise UnsupportedFormat("invalid core boolean in repository config")

    version = values.get(("core", "repositoryformatversion"), "0")
    if version not in ("0", "1"):
        raise UnsupportedFormat("unsupported repository format version")
    extensions = {key: value for (section, key), value in values.items() if section == "extensions"}
    if any(key not in ("objectformat", "refstorage") for key in extensions):
        raise UnsupportedFormat("unsupported required repository extension")
    if extensions and version != "1":
        raise UnsupportedFormat("repository extensions require format version 1")
    if extensions.get("refstorage", "files").lower() != "files":
        raise UnsupportedFormat("only files reference storage is supported; reftable is not implemented yet")
    try:
        format = ObjectFormat(extensions.get("objectformat", "sha1").lower())
    except ValueError as error:
        raise UnsupportedFormat("unsupported repository object format") from error
    if ("core", "worktree") in values or boolean("sparsecheckout"):
        raise UnsupportedFormat("custom worktrees and sparse checkout are not supported yet")
    return RepositoryConfig(int(version), boolean("bare"), format, boolean("filemode","true"), boolean("symlinks","true"))
