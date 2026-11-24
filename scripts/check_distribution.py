"""Build and exercise wheel/sdist installations outside the source checkout."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(*args, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qualification-only',action='store_true',help='run installed native compatibility and focused history/diff/push regressions')
    parser.add_argument('--output-dir',help='destination for versioned qualification records')
    parser.add_argument('--source-revision')
    parser.add_argument('--artifacts',help='qualify prebuilt release artifacts without rebuilding them')
    args=parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence=Path(args.output_dir).absolute() if args.output_dir else None
    if evidence:evidence.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        dist = Path(args.artifacts).absolute() if args.artifacts else workspace / "dist"
        if not args.artifacts:run("uv", "build", "--no-sources", "--out-dir", str(dist), cwd=root)
        artifacts = list(dist.glob("*.whl")) + list(dist.glob("*.tar.gz"))
        if len(artifacts) != 2:
            raise RuntimeError("expected exactly one wheel and one sdist")
        for i, artifact in enumerate(artifacts):
            profile='wheel' if artifact.suffix=='.whl' else 'sdist'
            venv = workspace / f"installed-{i}"
            run("uv", "venv", "--python", sys.executable, str(venv))
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run("uv", "pip", "install", "--python", str(python), "--no-deps", str(artifact))
            smoke = "import gitdx; from pathlib import Path; "
            smoke += f"assert Path(gitdx.__file__).resolve().is_relative_to(Path({str(venv)!r}).resolve()); "
            smoke += "assert len(gitdx.Blob(b'hello').to_raw().oid().hex) == 40"
            no_git = dict(os.environ, PATH="")
            run(str(python), "-I", "-c", smoke, cwd=workspace, env=no_git)
            run(str(python), "-I", "-m", "gitdx", "--help", cwd=workspace, env=no_git)
            report=(evidence or workspace)/(profile+'.json')
            selected=[str(python),'-I','-W','error::ResourceWarning',str(root/'scripts/qualify_repository.py'),
                      '--profile',profile,'--artifact',str(artifact),'--installed-prefix',str(venv),'--output',str(report)]
            if args.source_revision:selected.extend(('--source-revision',args.source_revision))
            run(*selected,cwd=workspace)
            selected_report=subprocess.run([str(python),'-I','-m','gitdx','capabilities','--evidence',str(report)],
                                           cwd=workspace,env=no_git,capture_output=True,text=True,check=True)
            if json.loads(selected_report.stdout)['qualification']['profile']!=profile:
                raise RuntimeError('installed capability report selected a different profile')
            print(profile+': native repository qualification and no-Git evidence selection passed',flush=True)
            if args.qualification_only:
                # Runtime evidence covers the native local/remote matrix. Focused
                # regressions also run against the independently installed APIs.
                for pattern in ('test_diff.py','test_revisions.py','test_ancestry.py','test_push*.py'):
                    if not list((root/'tests').glob(pattern)):raise RuntimeError('missing installed qualification fixtures: '+pattern)
                    run(str(python),'-I','-W','error::ResourceWarning','-m','unittest','discover',
                        '-s',str(root/'tests'),'-p',pattern,'-v',cwd=workspace,
                        env=dict(os.environ,GITDX_REQUIRE_GIT='1'))
            else:
                run(str(python), "-I", "-W", "error::ResourceWarning", "-m", "unittest", "discover",
                    "-s", str(root / "tests"), "-v", cwd=workspace)


if __name__ == "__main__":
    main()
