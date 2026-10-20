from pathlib import Path
from types import SimpleNamespace

import pytest

from flowx_core.llm_client.coder import Coder, LLMGenerationError


class _FakeStreamChunk:
    def __init__(self, content: str):
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=content))]


class _FakeStreamingCompletions:
    def __init__(self, responses, *, probe_path: Path | None = None, expected_partial: str | None = None):
        self._responses = list(responses)
        self.probe_path = probe_path
        self.expected_partial = expected_partial
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        chunks = self._responses.pop(0)

        def _iter_chunks():
            for index, chunk in enumerate(chunks):
                if index == 1 and self.probe_path is not None and self.expected_partial is not None:
                    assert self.probe_path.read_text(encoding="utf-8") == self.expected_partial
                yield _FakeStreamChunk(chunk)

        return _iter_chunks()


class _FakeStreamingClient:
    def __init__(self, responses, *, probe_path: Path | None = None, expected_partial: str | None = None):
        self.chat = SimpleNamespace(
            completions=_FakeStreamingCompletions(
                responses,
                probe_path=probe_path,
                expected_partial=expected_partial,
            )
        )


class _FallbackStreamingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            raise RuntimeError("stream unsupported")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="fallback = True\n"))]
        )


class _FallbackStreamingClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FallbackStreamingCompletions())


class _InterruptingStreamingCompletions:
    def __init__(self, *, partial_chunks=None, fallback_content: str = "fallback = True\n"):
        self.partial_chunks = list(partial_chunks or [])
        self.fallback_content = fallback_content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            partial_chunks = list(self.partial_chunks)

            def _iter_chunks():
                for chunk in partial_chunks:
                    yield _FakeStreamChunk(chunk)
                raise RuntimeError(
                    "peer closed connection without sending complete message body (incomplete chunked read)"
                )

            return _iter_chunks()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.fallback_content))]
        )


class _InterruptingStreamingClient:
    def __init__(self, *, partial_chunks=None, fallback_content: str = "fallback = True\n"):
        self.chat = SimpleNamespace(
            completions=_InterruptingStreamingCompletions(
                partial_chunks=partial_chunks,
                fallback_content=fallback_content,
            )
        )


def test_generate_code_aggregates_streamed_chunks_and_requests_streaming() -> None:
    client = _FakeStreamingClient([["```python\n", "value = 1\n", "print(value)\n", "```"]])
    coder = Coder(client=client)

    generated = coder.generate_code("write code")

    assert generated == "value = 1\nprint(value)"
    assert client.chat.completions.calls[0]["stream"] is True


def test_code_to_file_writes_stream_progressively(tmp_path: Path) -> None:
    target_path = tmp_path / "generated.py"
    client = _FakeStreamingClient(
        [["```python\nalpha = 1\n", "beta = 2\n", "```"]],
        probe_path=target_path,
        expected_partial="alpha = 1\n",
    )
    coder = Coder(client=client)

    written_path = coder.code_to_file("write code", str(target_path))

    assert written_path == target_path
    assert target_path.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\n"


def test_generate_code_falls_back_to_non_stream_when_stream_fails() -> None:
    client = _FallbackStreamingClient()
    coder = Coder(client=client)

    generated = coder.generate_code("write code")

    assert generated == "fallback = True"
    assert client.chat.completions.calls[0]["stream"] is True
    assert "stream" not in client.chat.completions.calls[1]


def test_generate_code_retries_without_stream_when_stream_iteration_fails_before_content() -> None:
    client = _InterruptingStreamingClient(partial_chunks=[], fallback_content="fallback = 2\n")
    coder = Coder(client=client)

    generated = coder.generate_code("write code")

    assert generated == "fallback = 2"
    assert client.chat.completions.calls[0]["stream"] is True
    assert "stream" not in client.chat.completions.calls[1]


def test_code_to_file_retries_without_stream_when_stream_interrupts_after_partial_output(tmp_path: Path) -> None:
    target_path = tmp_path / "generated.py"
    client = _InterruptingStreamingClient(
        partial_chunks=["alpha = 1\n"],
        fallback_content="alpha = 1\nbeta = 2\n",
    )
    coder = Coder(client=client)

    written_path = coder.code_to_file("write code", str(target_path))

    assert written_path == target_path
    assert target_path.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\n"
    assert client.chat.completions.calls[0]["stream"] is True
    assert "stream" not in client.chat.completions.calls[1]


class _FlakyCompletions:
    def __init__(self, failures: int, content: str = "value = 1\n"):
        self.failures = failures
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("upstream failed")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class _FlakyClient:
    def __init__(self, failures: int, content: str = "value = 1\n"):
        self.chat = SimpleNamespace(completions=_FlakyCompletions(failures, content))


def test_code_to_file_retries_after_transient_failure(tmp_path: Path) -> None:
    target_path = tmp_path / "generated.py"
    client = _FlakyClient(failures=1)
    coder = Coder(client=client, use_streaming=False, max_retries=2)

    written_path = coder.code_to_file("write code", str(target_path))

    assert written_path == target_path
    assert target_path.read_text(encoding="utf-8") == "value = 1\n"
    assert len(client.chat.completions.calls) == 2


def test_code_to_file_raises_after_max_retries_exhausted(tmp_path: Path) -> None:
    target_path = tmp_path / "generated.py"
    client = _FlakyClient(failures=10)
    coder = Coder(client=client, use_streaming=False, max_retries=2)

    with pytest.raises(LLMGenerationError):
        coder.code_to_file("write code", str(target_path))

    assert len(client.chat.completions.calls) == 3
    assert not target_path.exists()