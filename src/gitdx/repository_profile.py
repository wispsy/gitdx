"""Required repository semantics checked before selecting local operations."""

import os

from .errors import UnsupportedFormat
from .filesystem import check_path


def check_repository_features(git_dir,config):
    for name in ('commondir','shallow','objects/info/alternates','objects/info/http-alternates'):
        target=git_dir/name;check_path(git_dir,target)
        if target.exists():raise UnsupportedFormat('unsupported repository metadata profile: '+name)
    remotes={e.subsection for e in config.values.entries if e.section=='remote'}
    for remote in remotes:
        if config.values.boolean('remote','promisor',subsection=remote) or config.values.get_all('remote','partialclonefilter',subsection=remote):
            raise UnsupportedFormat('promisor/partial-clone repositories are unsupported; no lazy fetch is selected')
    pack=git_dir/'objects/pack';check_path(git_dir,pack)
    if pack.exists():
        with os.scandir(pack) as entries:
            for count,entry in enumerate(entries,1):
                if count>4096:raise UnsupportedFormat('pack metadata entry limit exceeded')
                if entry.name.casefold().endswith('.promisor'):
                    raise UnsupportedFormat('promisor pack markers are unsupported')
