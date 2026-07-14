import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from dectl.config import DectlConfig
from dectl.env import substitute_env
from dectl.output import error
from dectl.output import success

# Where mounted buckets land. Kept under the user cache dir so mounts are per-user and
# obviously ephemeral rather than polluting the home directory.
MOUNT_BASE = Path.home() / '.cache' / 'dectl' / 'mounts'


def shell_variable_name(pipeline_name: str, shortname: str) -> str:
    """Build the lowercase shell variable name for a bucket: pipeline_shortname.

    Hyphens (legal in pipeline/bucket names but not in shell identifiers) and any other
    non-identifier character collapse to underscores.
    """
    return re.sub(r'[^a-z0-9_]', '_', f'{pipeline_name}_{shortname}'.lower())


def make_s3_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    buckets = pipeline.buckets
    shortname_list = ', '.join(buckets.keys()) or '(none configured)'
    example = next(iter(buckets), 'SHORTNAME')
    shortname_arg_help = f'Bucket shortname from config. Available: {shortname_list}'

    s3_app = typer.Typer(
        no_args_is_help=True,
        help=(f'Export bucket URIs to your shell and mount buckets locally in [bold]{pipeline_name}[/bold].\n\nBuckets: {shortname_list}'),
    )

    def resolve_bucket(shortname: str) -> str:
        if shortname not in buckets:
            known = ', '.join(buckets.keys())
            error(f'unknown bucket "{shortname}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return substitute_env(buckets[shortname])

    def require_linux() -> None:
        if platform.system() != 'Linux':
            error(f's3 mount is Linux-only (needs mount-s3 / FUSE). On {platform.system()}, use "dectl {pipeline_name} s3 export".')
            raise typer.Exit(1)

    @s3_app.command(
        epilog=(
            'Examples:\n\n'
            f'eval "$(dectl {pipeline_name} s3 export)" — load bucket URIs into your shell\n\n'
            f'aws s3 cp "${shell_variable_name(pipeline_name, example)}/file.txt" .'
        ),
    )
    def export() -> None:
        """Print shell `export` statements mapping each bucket to its s3:// URI.

        A CLI cannot modify the environment of the shell that launched it, so this prints
        the assignments to stdout for you to evaluate:

            eval "$(dectl PIPELINE s3 export)"

        Each bucket becomes a lowercase variable named `pipeline_shortname` whose value is
        the full `s3://bucket` URI, so `aws s3 cp "$pipeline_raw/key" .` just works.
        """
        # Plain print(), not the rich console: this output is meant to be eval'd, so it must
        # be free of markup and ANSI escapes. Bucket names are DNS-safe, so single-quoting
        # the value is sufficient shell quoting.
        for shortname, bucket in buckets.items():
            print(f"export {shell_variable_name(pipeline_name, shortname)}='s3://{substitute_env(bucket)}'")

    @s3_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} s3 mount {example}',
    )
    def mount(
        shortname: Annotated[str, typer.Argument(help=shortname_arg_help)],
    ) -> None:
        """Mount a bucket as a local directory using Mountpoint for Amazon S3 (Linux only).

        Shells out to `mount-s3`, mounting the bucket at
        ~/.cache/dectl/mounts/PIPELINE/SHORTNAME using the pipeline's configured profile
        and region. FUSE is Linux-only; on macOS use `export` instead.
        """
        bucket = resolve_bucket(shortname)
        require_linux()
        mount_s3_bin = shutil.which('mount-s3')
        if not mount_s3_bin:
            error('mount-s3 not found. Install Mountpoint for Amazon S3: https://github.com/awslabs/mountpoint-s3')
            raise typer.Exit(1)

        mountpoint = MOUNT_BASE / pipeline_name / shortname
        mountpoint.mkdir(parents=True, exist_ok=True)

        command = [mount_s3_bin, bucket, str(mountpoint)]
        if config.defaults.aws_profile:
            command += ['--profile', config.defaults.aws_profile]
        if config.defaults.region:
            command += ['--region', config.defaults.region]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            error(result.stderr.strip() or 'mount-s3 failed')
            raise typer.Exit(1)
        success(f'mounted s3://{bucket} at {mountpoint}')

    @s3_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} s3 unmount {example}',
    )
    def unmount(
        shortname: Annotated[str, typer.Argument(help=shortname_arg_help)],
    ) -> None:
        """Unmount a bucket previously mounted with `mount` (Linux only)."""
        # Validate the shortname so we give a config-level error rather than umount's cryptic
        # "not mounted" when someone fat-fingers the name.
        resolve_bucket(shortname)
        require_linux()
        umount_bin = shutil.which('umount')
        if not umount_bin:
            error('umount not found on PATH')
            raise typer.Exit(1)

        mountpoint = MOUNT_BASE / pipeline_name / shortname

        result = subprocess.run([umount_bin, str(mountpoint)], capture_output=True, text=True)
        if result.returncode != 0:
            error(result.stderr.strip() or 'umount failed')
            raise typer.Exit(1)
        success(f'unmounted {mountpoint}')

    return s3_app
