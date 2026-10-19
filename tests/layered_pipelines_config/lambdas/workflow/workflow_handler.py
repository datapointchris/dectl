from aws_durable_execution_sdk_python import DurableContext
from aws_durable_execution_sdk_python import durable_execution


@durable_execution
def handler(event, context: DurableContext):
    print('workflow', event, flush=True)
    return {'stage': 'done'}
