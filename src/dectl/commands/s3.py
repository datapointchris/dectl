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

# Where mounted buckets land. A short, top-level path under $HOME so a mounted bucket is
# easy to cd into (~/buckets/PIPELINE/ALIAS); the pipeline segment namespaces buckets
# that share an alias across pipelines.
MOUNT_BASE = Path.home() / 'buckets'


def shell_variable_name(prefix: str, alias: str) -> str:
    """Build the lowercase shell variable name for a bucket: prefix_alias.

    Hyphens (legal in pipeline/bucket names but not in shell identifiers) and any other
    non-identifier character collapse to underscores.
    """
    return re.sub(r'[^a-z0-9_]', '_', f'{prefix}_{alias}'.lower())


def require_linux(pipeline_name: str) -> None:
    if platform.system() != 'Linux':
        error(f's3 mount is Linux-only (needs mount-s3 / FUSE). On {platform.system()}, use "dectl {pipeline_name} s3 export".')
        raise typer.Exit(1)


def make_s3_bucket_app(pipeline_name: str, alias: str, bucket_template: str, config: DectlConfig) -> typer.Typer:
    """Build the per-bucket sub-app: `dectl PIPELINE s3 <alias> <verb>`.

    Instance verbs act on one bucket. Bulk export lives at the s3 level (see make_s3_app),
    because it spans every bucket and has no single alias."""
    bucket_app = typer.Typer(
        no_args_is_help=True,
        help=f'Bucket [bold]{alias}[/bold] → s3://{bucket_template}',
    )

    def resolved_bucket() -> str:
        return substitute_env(bucket_template)

    @bucket_app.command(epilog=f'Example:\n\naws s3 cp "$(dectl {pipeline_name} s3 {alias} uri)/file.txt" .')
    def uri() -> None:
        """Print this bucket's bare s3:// URI (no markup), for use in $(...) and scripts."""
        # Bare print(), not the rich console: the output is meant for command substitution,
        # so it must carry no markup or ANSI escapes.
        print(f's3://{resolved_bucket()}')

    @bucket_app.command(epilog=f'Example:\n\ndectl {pipeline_name} s3 {alias} mount')
    def mount() -> None:
        """Mount this bucket as a local directory using Mountpoint for Amazon S3 (Linux only).

        Shells out to `mount-s3`, mounting at ~/buckets/PIPELINE/ALIAS using the pipeline's
        configured profile and region. FUSE is Linux-only; on macOS use `export` instead.
        """
        bucket = resolved_bucket()
        require_linux(pipeline_name)
        mount_s3_bin = shutil.which('mount-s3')
        if not mount_s3_bin:
            error('mount-s3 not found. Install Mountpoint for Amazon S3: https://github.com/awslabs/mountpoint-s3')
            raise typer.Exit(1)

        mountpoint = MOUNT_BASE / pipeline_name / alias
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

    @bucket_app.command(epilog=f'Example:\n\ndectl {pipeline_name} s3 {alias} unmount')
    def unmount() -> None:
        """Unmount this bucket if it was mounted with `mount` (Linux only)."""
        require_linux(pipeline_name)
        umount_bin = shutil.which('umount')
        if not umount_bin:
            error('umount not found on PATH')
            raise typer.Exit(1)

        mountpoint = MOUNT_BASE / pipeline_name / alias
        result = subprocess.run([umount_bin, str(mountpoint)], capture_output=True, text=True)
        if result.returncode != 0:
            error(result.stderr.strip() or 'umount failed')
            raise typer.Exit(1)
        success(f'unmounted {mountpoint}')

    return bucket_app


def make_s3_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    buckets = pipeline.buckets
    alias_list = ', '.join(buckets.keys()) or '(none configured)'
    example = next(iter(buckets), 'raw')

    s3_app = typer.Typer(
        no_args_is_help=True,
        help=(
            f'S3 buckets in [bold]{pipeline_name}[/bold] — pick a bucket for mount/unmount/uri, '
            f'or `export` to load them all into your shell.\n\nBuckets: {alias_list}'
        ),
    )

    @s3_app.command(
        rich_help_panel='All buckets',
        epilog=(
            'Examples:\n\n'
            f'eval "$(dectl {pipeline_name} s3 export)" — load every bucket URI into your shell\n\n'
            f'aws s3 cp "${shell_variable_name(pipeline_name, example)}/file.txt" .'
        ),
    )
    def export(
        prefix: Annotated[
            str | None,
            typer.Option('--prefix', help=f'Shell-variable name prefix. Default: the pipeline name ("{pipeline_name}").'),
        ] = None,
    ) -> None:
        """Print shell `export` statements mapping every bucket to its s3:// URI.

        A CLI cannot modify the environment of the shell that launched it, so this prints the
        assignments to stdout for you to evaluate:

            eval "$(dectl PIPELINE s3 export)"

        Each bucket becomes a lowercase variable named `<prefix>_<alias>` (prefix defaults to the
        pipeline name; hyphens become underscores) whose value is the full `s3://bucket` URI, so
        `aws s3 cp "$pipeline_raw/key" .` just works.
        """
        # Plain print(), not the rich console: this output is meant to be eval'd, so it must be
        # free of markup and ANSI escapes. Bucket names are DNS-safe, so single-quoting suffices.
        var_prefix = prefix if prefix is not None else pipeline_name
        for alias, bucket in buckets.items():
            print(f"export {shell_variable_name(var_prefix, alias)}='s3://{substitute_env(bucket)}'")

    for alias, bucket_template in buckets.items():
        s3_app.add_typer(
            make_s3_bucket_app(pipeline_name, alias, bucket_template, config),
            name=alias,
            rich_help_panel='Buckets',
        )
    return s3_app
