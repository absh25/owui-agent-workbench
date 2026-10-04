"""Observed plan summaries and bounded model context; original OWUI traces stay intact."""

import json
import re

from supervisor.task_plan import TASK_PLAN_NAME, decode_tool_result, tool_kind


def current_plan_result(messages):
    calls, result, invalid_count, terminal = set(), None, 0, None
    for message in messages:
        if message.get('role') == 'user':
            calls, result, invalid_count, terminal = set(), None, 0, None
        elif message.get('role') == 'assistant':
            calls.update(call.get('id') for call in message.get('tool_calls') or []
                         if call.get('function', {}).get('name') == TASK_PLAN_NAME)
        elif message.get('role') == 'tool' and message.get('tool_call_id') in calls:
            result = decode_tool_result(message.get('content'))
            if not isinstance(result, dict):
                result = {'status': 'failed', 'execution_started': True,
                          'summary': '计划返回格式无效，无法确认任务结果。'}
            if result.get('status') == 'invalid_arguments' and result.get('execution_started') is False:
                invalid_count += 1
            else:
                terminal = result
    if terminal is not None:
        return terminal
    return {**result, 'invalid_argument_attempts': invalid_count} if result is not None else None


def can_correct_plan(result):
    return (isinstance(result, dict) and result.get('status') == 'invalid_arguments'
            and result.get('execution_started') is False
            and result.get('invalid_argument_attempts', 1) <= 2)


def _cell(value, limit=400):
    return str(value).replace('|', ' / ').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')[:limit]


def _seconds(value):
    return f'{value:.1f}s' if isinstance(value, (int, float)) else '未启动'


def summarize_plan(report):
    """Render only recorded results, with no extra model round or guessed outcomes."""
    if report.get('status') == 'invalid_arguments':
        return '任务计划参数修正次数已用尽，未执行。\n\n' + _cell(report.get('summary', report.get('error', '参数不合法。')))
    labels = {'succeeded': '完成', 'failed': '失败', 'blocked': '依赖阻断',
              'timed_out': '超时', 'cancelled': '已取消', 'waiting': '等待依赖', 'ready': '等待执行'}
    lines = ['任务计划已结束。' if report.get('status') == 'completed' else '任务计划未全部完成，结果如下。', '',
             '| 任务 | 依赖 | 状态 | 开始时间（相对计划） | 执行耗时 |',
             '|---|---|---|---|---|']
    for task in report.get('tasks', []):
        lines.append('| ' + ' | '.join([
            _cell(task.get('id', '') + '：' + task.get('title', ''), 120),
            _cell(', '.join(task.get('depends_on', [])) or '无'),
            labels.get(task.get('status'), _cell(task.get('status'))),
            _seconds(task.get('started_after_seconds')), _seconds(task.get('wall_seconds'))]) + ' |')
    lines.extend(['', f"实际并发峰值：{report.get('peak_parallel', 0)}；计划耗时：{_seconds(report.get('wall_seconds'))}。"])
    for task in report.get('tasks', []):
        result = task.get('result')
        if not isinstance(result, dict):
            if result is not None:
                lines.extend(['', f"{_cell(task.get('id'))} 返回：{_cell(result, 900)}"])
            elif task.get('reason'):
                lines.extend(['', f"{_cell(task.get('id'))}：{_cell(task['reason'])}"])
            continue
        kind = tool_kind(task.get('tool', ''))
        lines.extend(['', f"**{_cell(task.get('id'))}：{_cell(task.get('title'))}**"])
        if kind == 'delegate_retrieval_task':
            lines.append(_cell(result.get('summary', result.get('status', '')), 1200))
            for ref in result.get('evidence', [])[:3]:
                if isinstance(ref, dict):
                    lines.append(f"\n> {_cell(ref.get('quote', ''), 700)}\n\n来源：{_cell(ref.get('citation') or ref.get('filename', ''))}")
            missing = result.get('missing_information') or []
            if missing:
                lines.append('缺口：' + '；'.join(_cell(item) for item in missing[:4]))
        elif kind == 'delegate_coding_task':
            verification = result.get('verification') or {}
            passed = result.get('status') == 'completed' and verification.get('passed') is True
            lines.append('独立验收：' + ('尚未运行（工作区忙，编码未开始）。' if result.get('status') == 'workspace_busy'
                         else '通过。' if passed else '未通过，不能按完成交付。'))
            lines.append(_cell(result.get('summary', ''), 900))
            if result.get('execution_mode') == 'demo_fixture' and result.get('sdk_used') is False:
                lines.append('执行方式：预制作品演示；未调用 SDK 现场编码。')
            files = result.get('changed_files') or []
            if files:
                lines.append('实际变更：' + '、'.join('`' + _cell(path) + '`' for path in files[:12]))
            for check in verification.get('checks', []):
                output = check.get('output', '')
                match = re.search(r'Ran (\d+) tests?', output)
                count = f"；实际 {match.group(1)} 个测试方法" if match else ''
                lines.append(f"验收命令：`{_cell(check.get('command', ''))}`；退出码 {check.get('exit_code')}{count}。")
                if check.get('exit_code') != 0 or check.get('passed') is False:
                    lines.append('失败输出：' + _cell(output[-800:], 800))
            for issue in (verification.get('issues') or result.get('open_issues') or [])[:4]:
                lines.append('问题：' + _cell(issue))
            suggestions = verification.get('suggestions') or (verification.get('visual_review') or {}).get('suggestions') or []
            for suggestion in suggestions[:3]:
                lines.append('改进建议（不阻塞验收）：' + _cell(suggestion))
            if result.get('delivery_errors'):
                lines.append('附件交付尚未完成：' + _cell(result['delivery_errors']))
            timings = result.get('timings') or {}
            if timings:
                values = [('排队', 'lock_wait_ms'), ('SDK 启动', 'sdk_startup_ms'),
                          ('SDK 与验收', 'sdk_and_verification_ms'), ('交付', 'delivery_ms')]
                parts = [f'{label} {timings[key] / 1000:.1f}s' for label, key in values
                         if isinstance(timings.get(key), (int, float))]
                if parts:
                    lines.append('编码耗时记录：' + '；'.join(parts) + '。')
        elif task.get('status') == 'succeeded':
            projects = result.get('projects')
            if isinstance(projects, list):
                lines.append('当前账号可见项目：' + '；'.join(_cell(p.get('key', '') + ' / ' + p.get('name', ''))
                            for p in projects[:3] if isinstance(p, dict)))
            else:
                lines.append(_cell(json.dumps(result, ensure_ascii=False, default=str), 1600))
        else:
            lines.append(_cell(result.get('error') or result.get('summary') or task.get('reason') or result, 900))
    return '\n'.join(lines)


def compact_coding_result(content):
    """Avoid resending duplicate SDK trace after success, retaining every acceptance outcome."""
    report = decode_tool_result(content)
    if not isinstance(report, dict) or report.get('status') != 'completed' or (report.get('verification') or {}).get('passed') is not True:
        return content
    keep = ('status', 'task_id', 'repo', 'summary', 'changed_files', 'open_issues',
            'execution_mode', 'sdk_used', 'prepared', 'scene', 'demo',
            'artifacts', 'delivery_errors', 'timings')
    compact = {key: report[key] for key in keep if key in report}
    verification = report['verification']
    compact['verification'] = {key: verification[key] for key in
                              ('passed', 'issues', 'suggestions', 'visual_review', 'attempts', 'changed_files', 'browser', 'screenshots', 'timings')
                              if key in verification}
    compact['verification']['checks'] = [{**check, 'output': check.get('output', '')[-2200:],
                                           'output_truncated': len(check.get('output', '')) > 2200}
                                         for check in verification.get('checks', [])]
    compact['trace_note'] = '完整工具证据和验收历史保留在 OWUI 原始工具结果中；此处省略重复轨迹。'
    return json.dumps(compact, ensure_ascii=False, default=str)
