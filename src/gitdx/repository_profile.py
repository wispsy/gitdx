"""Required repository semantics checked before selecting local operations."""

import os

from .errors import UnsupportedFormat
from .filesystem import check_path, storage_exists, storage_names


def check_repository_features(git_dir,config):
    for name in ('commondir','shallow','objects/info/alternates','objects/info/http-alternates'):
        target=git_dir/name;check_path(git_dir,target)
        if storage_exists(target):raise UnsupportedFormat('unsupported repository metadata profile: '+name)
    remotes={e.subsection for e in config.values.entries if e.section=='remote'}
    for remote in remotes:
        if config.values.boolean('remote','promisor',subsection=remote) or config.values.get_all('remote','partialclonefilter',subsection=remote):
            raise UnsupportedFormat('promisor/partial-clone repositories are unsupported; no lazy fetch is selected')
    pack=git_dir/'objects/pack';check_path(git_dir,pack)
    if storage_exists(pack):
        for count,name in enumerate(storage_names(pack),1):
            if count>4096:raise UnsupportedFormat('pack metadata entry limit exceeded')
            if name.casefold().endswith('.promisor'):
                raise UnsupportedFormat('promisor pack markers are unsupported')
