import typer

from dectl.config import DectlConfig
from dectl.config import PipelineConfig
from dectl.env import substitute_env
from dectl.env import warn_if_environment_had_no_effect
from dectl.logs import lambda_log_group
from dectl.logs import tail_log_groups
from dectl.output import console
from dectl.output import error
from dectl.output import info
from dectl.session import make_session


def build_monitor_sources(pipeline: PipelineConfig) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve the pipeline's monitor selection into (prefix, log_group) tail sources.

    Returns the sources plus any warnings for entries that reference a resource that is not
    configured or, for a state machine, has no CloudWatch log group to tail.

    The env guard reads the unsubstituted names of exactly these sources, so an unmonitored
    resource carrying `{env}` cannot hide that a monitored one hardcodes its environment."""
    entries = []
    aws_names = []
    warnings = []

    for alias in pipeline.monitor.lambdas:
        fn = pipeline.lambdas.get(alias)
        if fn is None:
            warnings.append(f'monitor lists lambda "{alias}" but it is not configured')
            continue
        aws_names.append(fn.name)
        entries.append((alias, 'cyan', lambda_log_group(substitute_env(fn.name))))

    for alias in pipeline.monitor.step_functions:
        sfn = pipeline.step_functions.get(alias)
        if sfn is None:
            warnings.append(f'monitor lists step function "{alias}" but it is not configured')
            continue
        if not sfn.log_group:
            warnings.append(f'step function "{alias}" has no log_group set; enable CloudWatch logging to monitor it')
            continue
        aws_names.append(sfn.log_group)
        entries.append((alias, 'magenta', substitute_env(sfn.log_group)))

    if aws_names:
        warn_if_environment_had_no_effect(aws_names)

    # Left-pad the aliases so the prefixes line up into readable columns in the combined stream.
    width = max((len(alias) for alias, color, group in entries), default=0)
    sources = [(f'[{color}]{alias.ljust(width)}[/{color}] ', group) for alias, color, group in entries]
    return sources, warnings


def run_monitor(pipeline: PipelineConfig, config: DectlConfig) -> None:
    sources, warnings = build_monitor_sources(pipeline)
    for warning in warnings:
        console.print(f'[yellow]{warning}[/yellow]')

    if not sources:
        error('nothing to monitor — add lambdas/step_functions under this pipeline\'s "monitor" in config')
        raise typer.Exit(1)

    info(f'monitoring {len(sources)} sources (Ctrl-C to stop):')
    for prefix, group in sources:
        info(f'  {prefix}{group}')

    logs_client = make_session(config).client('logs')
    tail_log_groups(logs_client, sources, follow=True)
