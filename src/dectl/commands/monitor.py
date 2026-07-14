import typer

from dectl.config import DectlConfig
from dectl.config import PipelineConfig
from dectl.env import substitute_env
from dectl.logs import tail_log_groups
from dectl.output import console
from dectl.output import error
from dectl.output import info
from dectl.session import make_session


def build_monitor_sources(pipeline: PipelineConfig) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve the pipeline's monitor selection into (prefix, log_group) tail sources.

    Returns the sources plus any warnings for entries that reference a resource that is not
    configured or, for a state machine, has no CloudWatch log group to tail."""
    entries = []
    warnings = []

    for alias in pipeline.monitor.lambdas:
        fn = pipeline.lambdas.get(alias)
        if fn is None:
            warnings.append(f'monitor lists lambda "{alias}" but it is not configured')
            continue
        entries.append((alias, 'cyan', f'/aws/lambda/{substitute_env(fn.name)}'))

    for alias in pipeline.monitor.step_functions:
        sfn = pipeline.step_functions.get(alias)
        if sfn is None:
            warnings.append(f'monitor lists step function "{alias}" but it is not configured')
            continue
        if not sfn.log_group:
            warnings.append(f'step function "{alias}" has no log_group set; enable CloudWatch logging to monitor it')
            continue
        entries.append((alias, 'magenta', substitute_env(sfn.log_group)))

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
