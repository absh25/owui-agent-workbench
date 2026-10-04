"""Update only the current coding tool and main prompt; preserve user settings."""

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3

from open_webui.models.functions import Functions
from open_webui.models.tools import Tools
from open_webui.routers.tools import get_tool_specs
from open_webui.utils.plugin import load_tool_module_by_id


async def main():
    root = Path(__file__).parent
    content = (root / 'coding_tool.py').read_text(encoding='utf-8')
    module, _ = await load_tool_module_by_id('coding_agent', content=content)
    specs = get_tool_specs(module)
    for spec in specs:
        params = spec['parameters']
        params['properties'] = {key: value for key, value in params['properties'].items() if not key.startswith('__')}
        params['required'] = [key for key in params.get('required', []) if not key.startswith('__')]
    assert {'verification_commands', 'preview_target', 'read_only'} <= set(specs[0]['parameters']['properties'])
    database = Path('/app/backend/data/webui.db')
    backup = database.with_name('webui.db.bak-before-coding-verification-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    backup.chmod(0o600)
    assert await Tools.update_tool_by_id('coding_agent', {'content': content, 'specs': specs}) is not None
    assert await Functions.update_function_by_id('owui-main', {'content': (root / 'native_pipe.py').read_text(encoding='utf-8')}) is not None
    print('CODING_VERIFICATION_SYNCED', str(backup), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
