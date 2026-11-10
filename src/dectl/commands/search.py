from collections.abc import Callable
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

from rich.table import Table

from dectl.output import console
from dectl.output import emit_json

if TYPE_CHECKING:
    import boto3

# One row per resource, its name first. The name is what the keyword is matched against.
Row = list[str]


def s3_buckets(client: Any) -> Iterator[Row]:
    for bucket in client.list_buckets().get('Buckets', []):
        yield [bucket['Name']]


def lambda_functions(client: Any) -> Iterator[Row]:
    for page in client.get_paginator('list_functions').paginate():
        for fn in page.get('Functions', []):
            yield [fn['FunctionName'], fn.get('Runtime', ''), fn.get('LastModified', '')]


def lambda_layers(client: Any) -> Iterator[Row]:
    for layer in client.list_layers().get('Layers', []):
        yield [layer['LayerName'], str(layer.get('LatestMatchingVersion', {}).get('Version', ''))]


def glue_jobs(client: Any) -> Iterator[Row]:
    for name in client.list_jobs().get('JobNames', []):
        yield [name]


def state_machines(client: Any) -> Iterator[Row]:
    for machine in client.list_state_machines().get('stateMachines', []):
        yield [machine['name'], machine.get('status', '')]


def iam_roles(client: Any) -> Iterator[Row]:
    for page in client.get_paginator('list_roles').paginate():
        for role in page.get('Roles', []):
            yield [role['RoleName']]


def iam_policies(client: Any) -> Iterator[Row]:
    for page in client.get_paginator('list_policies').paginate(Scope='Local'):
        for policy in page.get('Policies', []):
            yield [policy['PolicyName']]


def secrets(client: Any) -> Iterator[Row]:
    for page in client.get_paginator('list_secrets').paginate():
        for secret in page.get('SecretList', []):
            yield [secret['Name']]


class Searcher(NamedTuple):
    """One service `dectl search` scans: the client it lists names with, and how."""

    service: str
    # S3 and IAM list from a global endpoint, so their clients take no region.
    regional: bool
    scan: Callable[[Any], Iterator[Row]]

    def client(self, session: 'boto3.Session', region: str) -> Any:
        return session.client(self.service, region_name=region) if self.regional else session.client(self.service)


# The services `dectl search` scans, in the order it prints them. The search command's help and
# the scan both read this, so a service added here is searched and documented at once.
SEARCHERS: dict[str, Searcher] = {
    's3': Searcher('s3', False, s3_buckets),
    'lambda': Searcher('lambda', True, lambda_functions),
    'lambda-layers': Searcher('lambda', True, lambda_layers),
    'glue': Searcher('glue', True, glue_jobs),
    'stepfunctions': Searcher('stepfunctions', True, state_machines),
    'iam-roles': Searcher('iam', False, iam_roles),
    'iam-policies': Searcher('iam', False, iam_policies),
    'secrets': Searcher('secretsmanager', True, secrets),
}


def search_service(client: Any, keyword: str, service: str) -> list[Row]:
    needle = keyword.lower()
    try:
        return [row for row in SEARCHERS[service].scan(client) if needle in row[0].lower()]
    except Exception as e:
        if 'AccessDenied' in str(e) or 'not authorized' in str(e):
            return [['(access denied)']]
        return [[f'(error: {e})']]


def run_search(session: 'boto3.Session', keyword: str, region: str, as_json: bool = False) -> None:
    """Scan every service at once, and print the matches in `SEARCHERS` order.

    The scans share nothing, so the search waits on its slowest service rather than on all of
    them in turn. Every client is built here first, one after another, because a boto3 session
    is not safe to use from several threads and a client is."""
    clients = {service: searcher.client(session, region) for service, searcher in SEARCHERS.items()}
    with ThreadPoolExecutor(max_workers=len(SEARCHERS)) as pool:
        scans = {service: pool.submit(search_service, clients[service], keyword, service) for service in SEARCHERS}
    matches = {service: scan.result() for service, scan in scans.items()}
    matches = {service: rows for service, rows in matches.items() if rows}

    if as_json:
        emit_json({'keyword': keyword, 'region': region, 'matches': matches})
        return

    for service, rows in matches.items():
        table = Table(title=service, show_header=False, title_style='bold cyan')
        for row in rows:
            table.add_row(*row)
        console.print(table)
        console.print()
