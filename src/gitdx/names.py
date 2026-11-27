"""A portable subset of Git ref names used by local mutation APIs."""

from .errors import GitdxError


class InvalidRef(GitdxError, ValueError):
    """A ref name is invalid or outside the portable supported subset."""


def validate_refname(name: str, *, portable: bool = True) -> str:
    if not isinstance(name, str) or not name.startswith("refs/"):
        raise InvalidRef("ref names must be fully qualified under refs/")
    forbidden='~^:?*[\\'+('<>|"' if portable else '')
    if any(ord(c)<=32 or ord(c)==127 or portable and ord(c)>=127 or c in forbidden for c in name):
        raise InvalidRef("ref name contains an unsupported character")
    if ".." in name or "@{" in name or name.endswith("."):
        raise InvalidRef("ref name contains a reserved sequence")
    for part in name.split("/"):
        if not part or part.startswith(".") or part.endswith((".lock", ".")):
            raise InvalidRef("ref name contains a reserved path component")
        device = part.split(".", 1)[0].upper()
        if portable and device in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise InvalidRef("ref name contains a reserved platform device name")
    return name


def validate_branch(name: str) -> str:
    if not isinstance(name, str) or not name or name.startswith("-"):
        raise InvalidRef("branch name must be nonempty and cannot begin with a dash")
    validate_refname("refs/heads/" + name)
    return name
