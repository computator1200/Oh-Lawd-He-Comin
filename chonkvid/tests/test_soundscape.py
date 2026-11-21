"""The world map's soundscape: one conversation per contact link, spoken by Voxtral."""

from __future__ import annotations

import pytest

from chonkvid import soundscape
from chonkvid.world import build_world


@pytest.fixture()
def world():
    return build_world(24, 3)


def test_index_covers_every_link_once(world):
    idx = soundscape.chatter_index(world)
    assert idx["n"] == world.n and idx["seed"] == world.seed
    keys = [e["key"] for e in idx["edges"]]
    assert len(keys) == len(set(keys)) == world.graph.number_of_edges()
    for e in idx["edges"]:
        assert world.graph.has_edge(e["s"], e["t"])
        assert e["kind"] in ("household", "workplace")
        assert e["audio"]["a"].startswith("/api/world/chatter/audio?")
        assert "who=b" in e["audio"]["b"]
        assert e["names"][0] and e["names"][1]


def test_script_is_deterministic_and_names_both_agents(world):
    a, b = next(iter(world.graph.edges()))
    first = soundscape.script_for({x.id: x for x in world.agents}, a, b, world.seed)
    again = soundscape.script_for({x.id: x for x in world.agents}, a, b, world.seed)
    assert first == again
    agents = {x.id: x for x in world.agents}
    names = {agents[a].name, agents[b].name}
    blob = first["a"] + " " + first["b"]
    assert any(n in blob for n in names)
    # one line names the other agent, so the pair reads as a dialogue
    assert first["a"] != first["b"]


def test_scripts_vary_across_links(world):
    agents = {x.id: x for x in world.agents}
    lines = {soundscape.script_for(agents, u, v, world.seed)["a"]
             for u, v in world.graph.edges()}
    assert len(lines) >= 4, "every link sounding identical would be a wall of noise"


def test_links_speak_the_language_of_their_office(world):
    # a French office: the line must actually be French, not English with an accent
    langs = {world.agents[0].id: "fr"}
    idx = soundscape.chatter_index(world, langs)
    pair = {tuple(sorted((e["s"], e["t"]))): e for e in idx["edges"]}
    key = tuple(sorted((world.agents[0].id, next(iter(world.graph.neighbors(world.agents[0].id))))))
    edge = pair[key]
    assert edge["lang"] == "fr"
    assert "lang=fr" in edge["audio"]["a"]
    text = edge["lines"]["a"] + " " + edge["lines"]["b"]
    assert any(w in text.lower() for w in ("reçu", "envoyé", "recopie", "transfère", "coller", "mot"))


def test_unknown_language_falls_back_to_english(world):
    idx = soundscape.chatter_index(world, {world.agents[0].id: "klingon"})
    assert {e["lang"] for e in idx["edges"]} == {"en"}
    import asyncio

    class MockLLM:
        source = "mock"

    u, v = next(iter(world.graph.edges()))
    with pytest.raises(ValueError):
        asyncio.run(soundscape.clip_for(MockLLM(), world, u, v, "a", "klingon"))


def test_edge_key_is_order_independent():
    assert soundscape.edge_key(7, 3) == soundscape.edge_key(3, 7) == "3-7"


def test_mock_clip_is_playable_wav_and_cached(world, tmp_path, monkeypatch):
    monkeypatch.setattr(soundscape, "SOUND_DIR", tmp_path)
    calls = {"n": 0}
    real = soundscape._mock_wav

    def counting(text, seconds=2.6, rate=22050):
        calls["n"] += 1
        return real(text, seconds, rate)

    monkeypatch.setattr(soundscape, "_mock_wav", counting)

    class MockLLM:
        source = "mock"

    import asyncio

    a, b = next(iter(world.graph.edges()))
    audio, mime = asyncio.run(soundscape.clip_for(MockLLM(), world, a, b, "a"))
    assert audio[:4] == b"RIFF" and mime == "audio/wav"
    assert calls["n"] == 1
    # second read comes from disk, not from synthesis
    again, _ = asyncio.run(soundscape.clip_for(MockLLM(), world, a, b, "a"))
    assert again == audio and calls["n"] == 1
    assert len(list(tmp_path.glob("*.wav"))) == 1


def test_clip_rejects_a_pair_that_is_not_linked(world):
    import asyncio

    class MockLLM:
        source = "mock"

    linked = {tuple(sorted(e)) for e in world.graph.edges()}
    pair = next((u, v) for u in range(world.n) for v in range(u + 1, world.n)
                if (u, v) not in linked)
    with pytest.raises(ValueError):
        asyncio.run(soundscape.clip_for(MockLLM(), world, *pair))
    with pytest.raises(ValueError):
        u, v = next(iter(world.graph.edges()))
        asyncio.run(soundscape.clip_for(MockLLM(), world, u, v, "z"))


def test_world_serves_chatter_index_and_audio(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from chonkvid.server import app

    monkeypatch.setattr(soundscape, "SOUND_DIR", tmp_path)
    c = TestClient(app)
    r = c.get("/api/world/chatter?n=16&seed=2")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 16 and len(body["edges"]) > 10
    edge = body["edges"][0]
    a = c.get(edge["audio"]["a"])
    assert a.status_code == 200
    assert a.headers["content-type"].startswith("audio/")
    assert len(a.content) > 4000
    bad = c.get(f"/api/world/chatter/audio?n=16&seed=2&s={edge['s']}&t={edge['t']}&who=z")
    assert bad.status_code == 400


def test_post_chatter_takes_a_language_map(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from chonkvid.server import app

    monkeypatch.setattr(soundscape, "SOUND_DIR", tmp_path)
    c = TestClient(app)
    r = c.post("/api/world/chatter", json={"n": 20, "seed": 1, "langs": {"0": "de", "1": "de"}})
    assert r.status_code == 200
    body = r.json()
    assert "de" in body["languages"]
    edge = next(e for e in body["edges"] if e["s"] == 0 or e["t"] == 0)
    assert edge["lang"] in ("de", "en")
    bad = c.post("/api/world/chatter", json={"n": 20, "langs": ["fr"]})
    assert bad.status_code == 400


def test_engine_has_a_real_mute_and_speech_bubbles():
    """Regression guards for the controls: mute has to kill live sources, not just timers."""
    from fastapi.testclient import TestClient

    from chonkvid.server import app

    c = TestClient(app)
    js = c.get("/static/world/sound.js").text
    assert "killEdge" in js and "sources.push" in js, "mute must stop scheduled sources"
    assert "active()" in js, "the bubbles need the engine's live list"
    world = c.get("/static/world/world.js").text
    assert "drawBubble" in world and "SoundPhysics.active()" in world
    assert 'zoom-in' in world and 'zoom-out' in world
    page = c.get("/world").text
    assert 'id="zoom-in"' in page and 'id="zoom-out"' in page
    assert "Sound off" in page


def test_world_view_loads_the_engine():
    from fastapi.testclient import TestClient

    from chonkvid.server import app

    c = TestClient(app)
    page = c.get("/world").text
    assert "sound.js" in page and "sound-toggle" in page
    js = c.get("/static/world/sound.js")
    assert js.status_code == 200 and "SoundPhysics" in js.text
