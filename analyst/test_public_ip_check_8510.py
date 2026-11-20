import asyncio
from unittest.mock import AsyncMock, patch
from analyst import public_ip_check_8510 as subject


def test_ip_comparison_cache_and_failures():
    async def run():
        subject._cache = None
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b'49.205.36.145', b'')
        with patch.object(asyncio, 'create_subprocess_exec', return_value=proc) as spawn:
            assert (await subject.public_ip_status('49.205.36.145'))['status'] == 'MATCH'
            assert (await subject.public_ip_status('49.205.39.195'))['status'] == 'MISMATCH'
            assert (await subject.public_ip_status(''))['status'] == 'NOT_CONFIGURED'
            assert (await subject.public_ip_status('127.0.0.1'))['status'] == 'NOT_CONFIGURED'
            assert spawn.call_count == 1
            assert '-4' in spawn.call_args.args
            proc.communicate.return_value = (b'not an IP', b'')
            failure = await subject.public_ip_status('49.205.36.145', force=True)
            assert failure['status'] == 'CHECK_FAILED'
            assert failure['current_ip'] is None
    asyncio.run(run())
