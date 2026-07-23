import boto3
from rich.table import Table

from dectl.output import console
from dectl.output import emit_json


def search_service(session: boto3.Session, keyword: str, service: str, region: str) -> list[list[str]]:
    results = []
    try:
        if service == 's3':
            client = session.client('s3')
            resp = client.list_buckets()
            for b in resp.get('Buckets', []):
                if keyword.lower() in b['Name'].lower():
                    results.append([b['Name']])

        elif service == 'lambda':
            client = session.client('lambda', region_name=region)
            paginator = client.get_paginator('list_functions')
            for page in paginator.paginate():
                for fn in page.get('Functions', []):
                    if keyword.lower() in fn['FunctionName'].lower():
                        results.append([fn['FunctionName'], fn.get('Runtime', ''), fn.get('LastModified', '')])

        elif service == 'lambda-layers':
            client = session.client('lambda', region_name=region)
            resp = client.list_layers()
            for layer in resp.get('Layers', []):
                if keyword.lower() in layer['LayerName'].lower():
                    version = layer.get('LatestMatchingVersion', {}).get('Version', '')
                    results.append([layer['LayerName'], str(version)])

        elif service == 'glue':
            client = session.client('glue', region_name=region)
            resp = client.list_jobs()
            for name in resp.get('JobNames', []):
                if keyword.lower() in name.lower():
                    results.append([name])

        elif service == 'stepfunctions':
            client = session.client('stepfunctions', region_name=region)
            resp = client.list_state_machines()
            for sm in resp.get('stateMachines', []):
                if keyword.lower() in sm['name'].lower():
                    results.append([sm['name'], sm.get('status', '')])

        elif service == 'iam-roles':
            client = session.client('iam')
            paginator = client.get_paginator('list_roles')
            for page in paginator.paginate():
                for role in page.get('Roles', []):
                    if keyword.lower() in role['RoleName'].lower():
                        results.append([role['RoleName']])

        elif service == 'iam-policies':
            client = session.client('iam')
            paginator = client.get_paginator('list_policies')
            for page in paginator.paginate(Scope='Local'):
                for policy in page.get('Policies', []):
                    if keyword.lower() in policy['PolicyName'].lower():
                        results.append([policy['PolicyName']])

        elif service == 'secrets':
            client = session.client('secretsmanager', region_name=region)
            paginator = client.get_paginator('list_secrets')
            for page in paginator.paginate():
                for secret in page.get('SecretList', []):
                    if keyword.lower() in secret['Name'].lower():
                        results.append([secret['Name']])

    except Exception as e:
        if 'AccessDenied' in str(e) or 'not authorized' in str(e):
            results.append(['(access denied)'])
        else:
            results.append([f'(error: {e})'])

    return results


SERVICES = ['s3', 'lambda', 'lambda-layers', 'glue', 'stepfunctions', 'iam-roles', 'iam-policies', 'secrets']


def run_search(session: boto3.Session, keyword: str, region: str, as_json: bool = False) -> None:
    matches = {service: search_service(session, keyword, service, region) for service in SERVICES}
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
