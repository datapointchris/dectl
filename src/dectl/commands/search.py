from collections.abc import Callable
from collections.abc import Iterator
from typing import TYPE_CHECKING

from rich.table import Table

from dectl.output import console
from dectl.output import emit_json

if TYPE_CHECKING:
    import boto3

# One row per resource, its name first. The name is what the keyword is matched against.
Row = list[str]


def s3_buckets(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for bucket in session.client('s3').list_buckets().get('Buckets', []):
        yield [bucket['Name']]


def lambda_functions(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for page in session.client('lambda', region_name=region).get_paginator('list_functions').paginate():
        for fn in page.get('Functions', []):
            yield [fn['FunctionName'], fn.get('Runtime', ''), fn.get('LastModified', '')]


def lambda_layers(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for layer in session.client('lambda', region_name=region).list_layers().get('Layers', []):
        yield [layer['LayerName'], str(layer.get('LatestMatchingVersion', {}).get('Version', ''))]


def glue_jobs(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for name in session.client('glue', region_name=region).list_jobs().get('JobNames', []):
        yield [name]


def state_machines(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for machine in session.client('stepfunctions', region_name=region).list_state_machines().get('stateMachines', []):
        yield [machine['name'], machine.get('status', '')]


def iam_roles(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for page in session.client('iam').get_paginator('list_roles').paginate():
        for role in page.get('Roles', []):
            yield [role['RoleName']]


def iam_policies(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for page in session.client('iam').get_paginator('list_policies').paginate(Scope='Local'):
        for policy in page.get('Policies', []):
            yield [policy['PolicyName']]


def secrets(session: 'boto3.Session', region: str) -> Iterator[Row]:
    for page in session.client('secretsmanager', region_name=region).get_paginator('list_secrets').paginate():
        for secret in page.get('SecretList', []):
            yield [secret['Name']]


# The services `dectl search` scans, in the order it prints them. The search command's help and
# the scan both read this, so a service added here is searched and documented at once.
SEARCHERS: dict[str, Callable[['boto3.Session', str], Iterator[Row]]] = {
    's3': s3_buckets,
    'lambda': lambda_functions,
    'lambda-layers': lambda_layers,
    'glue': glue_jobs,
    'stepfunctions': state_machines,
    'iam-roles': iam_roles,
    'iam-policies': iam_policies,
    'secrets': secrets,
}


def search_service(session: 'boto3.Session', keyword: str, service: str, region: str) -> list[Row]:
    needle = keyword.lower()
    try:
        return [row for row in SEARCHERS[service](session, region) if needle in row[0].lower()]
    except Exception as e:
        if 'AccessDenied' in str(e) or 'not authorized' in str(e):
            return [['(access denied)']]
        return [[f'(error: {e})']]


def run_search(session: 'boto3.Session', keyword: str, region: str, as_json: bool = False) -> None:
    matches = {service: search_service(session, keyword, service, region) for service in SEARCHERS}
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
