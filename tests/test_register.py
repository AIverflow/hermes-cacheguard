"""Tests of __init__.py: register(ctx)."""
import pytest


def test_register(env):
    reg = {"mw": [], "hook": [], "cmd": []}

    class Ctx:
        def register_middleware(self, name, fn):
            reg["mw"].append(name)

        def register_hook(self, name, fn):
            reg["hook"].append(name)

        def register_command(self, name, fn, **kw):
            reg["cmd"].append(name)

    env.plugin.register(Ctx())
    assert reg == {"mw": ["llm_execution", "llm_request"], "hook": ["transform_llm_output"], "cmd": ["cacheguard"]}


def test_register_without_llm_execution_middleware_still_loads(env):
    reg = {"hook": [], "cmd": []}

    class Ctx:
        def register_middleware(self, name, fn):
            if name == "llm_execution":
                raise AttributeError("renamed")

        def register_hook(self, name, fn):
            reg["hook"].append(name)

        def register_command(self, name, fn, **kw):
            reg["cmd"].append(name)

    env.plugin.register(Ctx())
    assert reg == {"hook": ["transform_llm_output"], "cmd": ["cacheguard"]}
    lines = [t for t in env.notices if t == "cacheguard is off: it could not start."]
    assert len(lines) == 1
    assert "llm_execution" not in lines[0] and "AttributeError" not in lines[0]
    assert "AttributeError: renamed" in env.plugin._health_text()  # the detail stays in /cacheguard


@pytest.mark.parametrize("hook, effect", [
    ("llm_request", "cacheguard lines may reach the model."),
    ("transform_llm_output", "no result lines."),
])
def test_health_names_a_hook_that_failed_to_register(env, hook, effect):
    class Ctx:
        def register_middleware(self, name, fn):
            if name == hook:
                raise RuntimeError("refused")

        def register_hook(self, name, fn):
            if name == hook:
                raise RuntimeError("refused")

        def register_command(self, name, fn, **kw):
            pass

    env.plugin.register(Ctx())  # never raises
    text = env.plugin._health_text()
    assert f"{hook}: RuntimeError: refused ({effect})" in text
    assert "health: degraded" in text


def test_register_adds_the_slash_command(env):
    seen = []

    class Ctx:
        def register_middleware(self, name, fn):
            pass

        def register_hook(self, name, fn):
            pass

        def register_command(self, name, fn, **kw):
            seen.append(name)

    env.plugin.register(Ctx())
    assert seen == ["cacheguard"]
