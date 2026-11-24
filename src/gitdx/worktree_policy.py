"""Conservative byte-preserving worktree policy, without helper execution."""

from pathlib import Path

from .errors import UnsupportedFormat
from .filesystem import read_file


def validate_attributes(data):
    """Refuse every transforming rule, even if it would not match today."""
    if len(data)>1024*1024:raise UnsupportedFormat("attribute file exceeds its limit")
    count = 0
    for line in data.split(b'\n'):
        line = line.strip()
        if not line or line.startswith(b'#'):continue
        count += 1
        if count>10_000 or len(line)>4096:raise UnsupportedFormat("attribute rule budget exceeded")
        parts = line.split()
        if len(parts)<2 or parts[0].startswith((b'!',b'[attr]',b'"')) or b'\\' in parts[0]:
            raise UnsupportedFormat("unsupported attribute pattern or macro")
        for value in parts[1:]:
            if value==b'binary':continue  # Built-in -text -diff -merge macro.
            if value in {b'-'+name for name in (b'text',b'crlf',b'ident',b'filter',b'working-tree-encoding',b'eol')}:
                continue
            if value in (b'-diff',b'!diff',b'diff',b'-merge',b'!merge',b'merge',b'-delta',b'delta',b'export-ignore',b'-export-ignore'):
                continue
            raise UnsupportedFormat("attribute transformation or unsupported attribute semantics")


def worktree_policy(config):
    values = config.values
    if any(e.section=='filter' for e in values.entries):raise UnsupportedFormat("external filter configuration is unsupported")
    if values.get_all('core','attributesfile'):raise UnsupportedFormat("external attribute files are unsupported")
    autocrlf = values.get('core','autocrlf','false')
    if autocrlf is None or autocrlf.lower()=='input' or values.boolean('core','autocrlf'):
        raise UnsupportedFormat("automatic line-ending conversion is unsupported")
    for key in ('eol','checkroundtripencoding'):
        if values.get_all('core',key):raise UnsupportedFormat("explicit conversion configuration is unsupported")
    # Helpers, hooks, credentials, signing and environment overrides are never
    # selected for execution by staging, status or checkout.


def excludes_file(repo, config):
    values = config.values.get_all('core','excludesfile')
    if not values or values[-1]=='':return b''
    value = values[-1]
    if value is None:raise UnsupportedFormat("exclude file requires a path value")
    path = Path(value)
    if path.anchor or '..' in path.parts or value.startswith(('~','/')) or '\\' in value or ':' in value:
        raise UnsupportedFormat("exclude file must be relative to the working-tree root")
    root = repo.work_tree
    try:return read_file(root,root/path,1024*1024)
    except FileNotFoundError:return b''
