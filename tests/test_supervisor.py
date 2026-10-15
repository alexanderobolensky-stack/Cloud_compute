import asyncio
import sys

from agent_portal.schemas import AgentSpec
from agent_portal.supervisor import Supervisor, build_command


def make_supervisor(tmp_path, events):
    return Supervisor(tmp_path / "logs", 200, 3.0, lambda level, msg, aid=None: events.append((level, msg, aid)))


async def wait_for(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def test_build_command_substitutes_python():
    spec = AgentSpec(id="a", name="A", command="{python} -c 'print(1)'")
    assert build_command(spec) == [sys.executable, "-c", "print(1)"]


def test_start_captures_logs_and_stop(tmp_path):
    async def scenario():
        events = []
        sup = make_supervisor(tmp_path, events)
        code = "import time\nprint('hello', flush=True)\nwhile True: time.sleep(0.1)"
        sup.register(AgentSpec(id="sleepy", name="S", command=[sys.executable, "-c", code]))
        rt = await sup.start("sleepy")
        assert rt.state == "running" and rt.pid
        await wait_for(lambda: any(line == "hello" for _, line in rt.logs))
        await sup.stop("sleepy")
        assert rt.state == "stopped" and rt.pid is None
        assert (tmp_path / "logs" / "sleepy.log").read_text().splitlines().count("hello") == 1
        assert [e[1] for e in events][-1] == "Остановлен"

    asyncio.run(scenario())


def test_crash_restarts_until_limit(tmp_path):
    async def scenario():
        events = []
        sup = make_supervisor(tmp_path, events)
        sup.register(AgentSpec(id="bad", name="B", command=[sys.executable, "-c", "raise SystemExit(3)"],
                               restart_policy="on-failure", max_restarts=1))
        rt = await sup.start("bad")
        await wait_for(lambda: rt.restarts == 1 and rt.state == "crashed" and rt.retry is None, timeout=15)
        assert rt.exit_code == 3
        assert any("лимит перезапусков" in e[1] for e in events)

    asyncio.run(scenario())


def test_clean_exit_not_restarted_on_failure_policy(tmp_path):
    async def scenario():
        sup = make_supervisor(tmp_path, [])
        sup.register(AgentSpec(id="ok", name="O", command=[sys.executable, "-c", "print('done')"]))
        rt = await sup.start("ok")
        await wait_for(lambda: rt.state == "exited")
        await asyncio.sleep(0.3)
        assert rt.restarts == 0 and rt.exit_code == 0

    asyncio.run(scenario())
