def handler(event, context):
    print('process', event, flush=True)
    return {'orders': event.get('orders', [])}
