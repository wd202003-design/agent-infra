import asyncio


async def execute_task(store, task, factory):
    backend = None
    state = 'failed'
    result = {'simulated': task['provider'] == 'demo', 'cleanup': 'not_started'}
    created = False
    creation_started = False
    try:
        backend = factory()
        if store.get(task['id'])['state'] != 'cancel_requested':
            creation_started = True
            await backend.create(task['id'])
            created = True
            store.bind(task['id'], task['epoch'], backend.id)
            if store.get(task['id'])['state'] != 'cancel_requested':
                outcome = await backend.execute(task['payload'])
                result.update(outcome)
                state = 'succeeded' if outcome.get('exit_code') == 0 and not outcome.get('execution_error') else 'failed'
                if outcome.get('exit_code') is None:
                    state = 'needs_review'
            else:
                state = 'cancelled'
        else:
            state = 'cancelled'
    except asyncio.CancelledError:
        state = 'needs_review'
        result['error_type'] = 'WorkerInterrupted'
    except Exception as exc:
        # Raw SDK exceptions can include request/response credentials.
        result['error_type'] = type(exc).__name__
        state = 'needs_review' if creation_started else 'failed'
    finally:
        if created:
            try:
                await backend.destroy()
                result['cleanup'] = 'simulated' if task['provider'] == 'demo' else 'management_api_absent'
            except Exception as exc:
                state = 'needs_review'
                result['cleanup'] = 'unknown'
                result['cleanup_error_type'] = type(exc).__name__
        elif creation_started:
            state = 'needs_review'
            result['cleanup'] = 'creation_outcome_unknown'
        else:
            result['cleanup'] = 'not_created'
        store.finish(task['id'], task['epoch'], state, result)


async def worker_loop(store, factory, stop, provider):
    while not stop.is_set():
        task = store.claim()
        if task:
            if task['provider'] != provider:
                store.finish(task['id'], task['epoch'], 'needs_review', {'reason': 'backend_changed_before_execution'})
            else:
                await execute_task(store, task, factory)
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=.25)
            except TimeoutError:
                pass
