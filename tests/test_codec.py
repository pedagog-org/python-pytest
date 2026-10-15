import pytest

from pedagog_pytest.isolation.codec import NotPlain, decode, encode

PLAIN = [
    None,
    True,
    42,
    3.5,
    "text",
    b"\x00\xff",
    [1, [2, 3]],
    (1, ("a", None)),
    {"a": 1, 2: [3], (4, 5): {"x"}},
    {1, 2},
    frozenset({"f"}),
]


@pytest.mark.parametrize("value", PLAIN)
def test_plain_values_round_trip_exactly(value):
    back = decode(encode(value))
    assert back == value
    assert type(back) is type(value)


class Sub(list):
    pass


@pytest.mark.parametrize("value", [object(), Sub([1]), [1, lambda: 0], {"k": print}])
def test_non_plain_values_are_refused(value):
    with pytest.raises(NotPlain):
        encode(value)


def test_cycles_and_deep_nesting_are_refused():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(NotPlain):
        encode(cyclic)
    deep = []
    for _ in range(100):
        deep = [deep]
    with pytest.raises(NotPlain):
        encode(deep)


def test_refs_become_handles_and_resolve_on_decode():
    marker = object()
    text = encode([1, marker], on_other=lambda v: {"$ref": 7})
    assert decode(text, resolve={7: marker}.__getitem__)[1] is marker


def test_refs_are_rejected_without_a_resolver():
    with pytest.raises(ValueError, match="unexpected object reference"):
        decode('{"$ref": 0}')
