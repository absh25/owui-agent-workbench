import json
import unittest

from supervisor.task_plan import TaskPlanRunner, decode_tool_result, result_succeeded


def entry(callable_, properties=None, required=None):
    return {'callable': callable_, 'spec': {'parameters': {'type': 'object',
            'properties': properties or {}, 'required': required or []}}}


def task(identifier, tool, arguments=None, dependencies=None):
    return {'id': identifier, 'title': identifier, 'tool': tool,
            'arguments': arguments or {}, 'depends_on': dependencies or []}


class TaskPlanEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_denials_never_release_a_dependent_node(self):
        for status in ('DENIED', 'CANCELLED', 'REJECTED', 'FAILED', 'WAITING_FOR_APPROVAL',
                       'TIMED_OUT', 'UNAUTHORIZED', 'INVALID_ARGUMENTS'):
            async def blocked():
                return {'status': status, 'execution_performed': False}

            async def dependent():
                self.fail('A rejected tool must never release its dependent')

            with self.subTest(status=status):
                report = await TaskPlanRunner({'get_current_weather': entry(blocked),
                                              'get_weather_forecast': entry(dependent)}).run([
                    task('read', 'get_current_weather'), task('after', 'get_weather_forecast', dependencies=['read'])])
                self.assertEqual(report['status'], 'partial_failed')
                self.assertEqual([item['status'] for item in report['tasks']], ['failed', 'blocked'])
        self.assertFalse(result_succeeded('get_current_weather', {'execution_performed': False}))
        self.assertFalse(result_succeeded('get_current_weather', {'status': ' REJECTED '}))

    def test_specialist_success_fields_cannot_override_an_error_flag(self):
        self.assertFalse(result_succeeded('delegate_retrieval_task',
                                         {'status': 'sufficient', 'isError': True}))
        self.assertFalse(result_succeeded('delegate_coding_task',
                                         {'status': 'completed', 'verification': {'passed': True}, 'is_error': True}))

    async def test_json_and_mcp_envelopes_decode_before_dependency_success(self):
        actual = {'status': 'sufficient', 'evidence': [{'chunk_id': 'source', 'quote': 'actual quotation'}]}
        mcp = {'content': [{'type': 'text', 'text': json.dumps(actual)}]}

        async def retrieve():
            return json.dumps(mcp)

        async def code(context=''):
            self.assertIn('source', context)
            self.assertIn('actual quotation', context)
            return {'status': 'completed', 'verification': {'passed': True}}

        report = await TaskPlanRunner({'delegate_retrieval_task': entry(retrieve),
                                      'delegate_coding_task': entry(code, {'context': {'type': 'string'}})}).run([
            task('read', 'delegate_retrieval_task'),
            task('code', 'delegate_coding_task', dependencies=['read'])])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['tasks'][0]['result'], actual)
        self.assertTrue(decode_tool_result(json.dumps({**mcp, 'isError': True}))['isError'])

    async def test_http_transport_headers_are_not_dependency_evidence(self):
        async def weather():
            return {'temperature_c': 20}, {'Authorization': 'PRIVATE_TRANSPORT_HEADER'}

        async def code(context=''):
            self.assertIn('temperature_c', context)
            self.assertNotIn('PRIVATE_TRANSPORT_HEADER', context)
            return {'status': 'completed', 'verification': {'passed': True}}

        report = await TaskPlanRunner({'get_current_weather': entry(weather),
                                      'delegate_coding_task': entry(code, {'context': {'type': 'string'}})}).run([
            task('weather', 'get_current_weather'), task('code', 'delegate_coding_task', dependencies=['weather'])])
        self.assertEqual(report['status'], 'completed')
        self.assertNotIn('PRIVATE_TRANSPORT_HEADER', json.dumps(report))

    async def test_nullable_anyof_and_type_unions_still_reject_wrong_types(self):
        called = []

        async def weather(location=None):
            called.append(location)
            return {'location': location}

        for shape in ({'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                      {'type': ['string', 'null']}):
            tools = {'get_current_weather': entry(weather, {'location': shape})}
            for value in (None, 'Shanghai'):
                report = await TaskPlanRunner(tools).run([task('read', 'get_current_weather', {'location': value})])
                self.assertEqual(report['status'], 'completed')
            report = await TaskPlanRunner(tools).run([task('read', 'get_current_weather', {'location': 42})])
            self.assertEqual(report['status'], 'invalid_arguments')
            self.assertFalse(report['execution_started'])
        self.assertEqual(called, [None, 'Shanghai', None, 'Shanghai'])

    async def test_retrieval_and_code_require_direct_evidence_dependency(self):
        async def never_run():
            self.fail('The complete invalid plan must reject before executing')

        tools = {name: entry(never_run) for name in
                 ('delegate_retrieval_task', 'delegate_coding_task', 'get_current_weather')}
        for plan in ([task('read', 'delegate_retrieval_task'), task('code', 'delegate_coding_task')],
                     [task('read', 'delegate_retrieval_task'),
                      task('middle', 'get_current_weather', dependencies=['read']),
                      task('code', 'delegate_coding_task', dependencies=['middle'])]):
            report = await TaskPlanRunner(tools).run(plan)
            self.assertEqual(report['status'], 'invalid_arguments')
            self.assertFalse(report['execution_started'])

    async def test_canonical_name_resolves_only_one_current_authorized_tool(self):
        called = []

        async def weather():
            called.append(True)
            return {'temperature_c': 20}

        tools = {'weather_server_get_current_weather': entry(weather)}
        report = await TaskPlanRunner(tools).run([task('read', 'get_current_weather')])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['tasks'][0]['tool'], 'weather_server_get_current_weather')
        self.assertEqual(called, [True])
        for bad in ({}, {**tools, 'other_server_get_current_weather': entry(weather)},
                    {'weather_server_get_current_weather': {**entry(weather), 'direct': True}}):
            report = await TaskPlanRunner(bad).run([task('read', 'get_current_weather')])
            self.assertEqual(report['status'], 'invalid_arguments')
            self.assertFalse(report['execution_started'])
        self.assertEqual(called, [True])

    async def test_malformed_tool_name_is_a_typed_parameter_error(self):
        report = await TaskPlanRunner({}).run([task('read', [])])
        self.assertEqual(report['status'], 'invalid_arguments')
        self.assertFalse(report['execution_started'])


if __name__ == '__main__':
    unittest.main()
