"""Explicitly enable or disable the prepared pelican scene in an existing OWUI lab."""

import argparse
import asyncio


async def configure(enabled):
    from open_webui.models.functions import Functions
    from open_webui.models.tools import Tools
    from supervisor.register import MAIN_ID

    if await Functions.get_function_by_id(MAIN_ID) is None or await Tools.get_tool_by_id('coding_agent') is None:
        raise RuntimeError('请先运行 supervisor.register 注册协作助手和编码任务。')
    for store, identifier, getter, setter in (
        (Tools, 'coding_agent', 'get_tool_valves_by_id', 'update_tool_valves_by_id'),
        (Functions, MAIN_ID, 'get_function_valves_by_id', 'update_function_valves_by_id'),
    ):
        valves = dict(await getattr(store, getter)(identifier) or {})
        valves['ENABLE_PELICAN_DEMO'] = enabled
        if identifier == 'coding_agent':
            valves['DEMO_RETURN_SECONDS'] = 180
        await getattr(store, setter)(identifier, valves)
    print('鹈鹕骑车预制演示：' + ('已开启，编码工具进入后约 3 分钟交付。' if enabled else '已关闭，编码任务使用正常 SDK 流程。'))
    print('用户仍须在聊天工具菜单中勾选“编码任务”；此配置不替用户开启工具。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--enable', action='store_true')
    choice.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    asyncio.run(configure(args.enable))


if __name__ == '__main__':
    main()
