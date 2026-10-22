import io
import os
import socket

from pedagog_pytest.isolation import output
from pedagog_pytest.isolation.output import Output


def handed_over(out: Output) -> list[int]:
    """The write ends, as the runner child receives them."""
    grader, child = socket.socketpair()
    out.send(grader)
    _, fds, _, _ = socket.recv_fds(child, 16, 2)
    grader.close()
    child.close()
    return fds


def emitted(out: Output) -> tuple[str, str]:
    o, e = io.StringIO(), io.StringIO()
    out.emit(o, e)
    return o.getvalue(), e.getvalue()


def test_each_stream_goes_to_its_own_target():
    out = Output()
    w_out, w_err = handed_over(out)
    os.write(w_out, b"to stdout\n")
    os.write(w_err, b"to stderr\n")
    assert emitted(out) == ("to stdout\n", "to stderr\n")
    assert emitted(out) == ("", "")
    os.close(w_out)
    os.close(w_err)
    out.close()


def test_a_character_split_across_writes_is_decoded_whole():
    out = Output()
    w_out, w_err = handed_over(out)
    os.write(w_out, "é".encode()[:1])
    assert emitted(out) == ("", "")
    os.write(w_out, "é".encode()[1:])
    assert emitted(out) == ("é", "")
    os.close(w_out)
    os.close(w_err)
    out.close()


def test_output_past_the_limit_is_dropped_with_a_note(monkeypatch):
    monkeypatch.setattr(output, "MAX_OUTPUT_BYTES", 1024 * 1024)
    out = Output()
    w_out, w_err = handed_over(out)
    os.close(w_err)
    chunk = b"y" * 65536
    for _ in range(20):  # 1.25 MiB, more than a pipe holds: the thread must drain it
        os.write(w_out, chunk)
    os.close(w_out)
    out.finish(timeout=5)
    text, _ = emitted(out)
    assert text == "y" * (1024 * 1024) + "\n…(student output cut at 1 MiB)\n"
    out.close()
