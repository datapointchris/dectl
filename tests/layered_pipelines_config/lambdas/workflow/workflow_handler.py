def handler(event, context):
    print('workflow', event, flush=True)
    return {'stage': 'done'}
