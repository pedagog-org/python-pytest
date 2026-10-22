# Failure corpus, graded under process isolation

From a real grading run (validation of the corpus as an assignment). `error` as
students would see it today, and the trace's status line (what `describe_failure`
made of it).

## 1. An assert comparing values (pytest explains them).

`test_failures.py::test_assert_comparing_values` · status `fail` · 8 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:21: AssertionError: assert 2 == 3)
```

**`error` today:**

```
def test_assert_comparing_values():
        """1. An assert comparing values (pytest explains them)."""
>       assert len(filled(1, 2, 3)) == 3
E       assert 2 == 3
E        +  where 2 = len(<linked_list.LinkedList object at 0xffffa5b74d10>)
E        +    where <linked_list.LinkedList object at 0xffffa5b74d10> = filled(1, 2, 3)

/pedagog/tests/test_failures.py:21: AssertionError
```

## 2. An assert with the instructor's message.

`test_failures.py::test_assert_with_a_message` · status `fail` · 10 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:26: AssertionError: to_list() should keep insertion order)
```

**`error` today:**

```
def test_assert_with_a_message():
        """2. An assert with the instructor's message."""
>       assert filled(1, 2).to_list() == [1, 2], "to_list() should keep insertion order"
E       AssertionError: to_list() should keep insertion order
E       assert [2, 1] == [1, 2]
E         
E         At index 0 diff: 2 != 1
E         Use -v to get more diff

/pedagog/tests/test_failures.py:26: AssertionError
```

## 3. An exception raised in the student's code.

`test_failures.py::test_exception_in_student_code` · status `fail` · 10 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:31: AttributeError: 'NoneType' object has no attribute 'value')
```

**`error` today:**

```
def test_exception_in_student_code():
        """3. An exception raised in the student's code."""
>       filled().pop_front()
E       AttributeError: 'NoneType' object has no attribute 'value'
E       student code traceback:
E         File "linked_list.py", line 33, in pop_front
E           value = self.head.value
E                   ^^^^^^^^^^^^^^^

/pedagog/tests/test_failures.py:31: AttributeError
```

## 4. An exception in the test itself (a method the student didn't write).

`test_failures.py::test_exception_in_the_test` · status `fail` · 7 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:36: AttributeError: 'LinkedList' object has no attribute 'reverse')
```

**`error` today:**

```
def test_exception_in_the_test():
        """4. An exception in the test itself (a method the student didn't write)."""
>       filled(1, 2).reverse()
        ^^^^^^^^^^^^^^^^^^^^
E       AttributeError: 'LinkedList' object has no attribute 'reverse'

/pedagog/tests/test_failures.py:36: AttributeError
```

## 5. An expected exception that never came.

`test_failures.py::test_expected_exception_missing` · status `fail` · 7 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:41: Failed: DID NOT RAISE <class 'ValueError'>)
```

**`error` today:**

```
def test_expected_exception_missing():
        """5. An expected exception that never came."""
>       with pytest.raises(ValueError):
             ^^^^^^^^^^^^^^^^^^^^^^^^^
E       Failed: DID NOT RAISE <class 'ValueError'>

/pedagog/tests/test_failures.py:41: Failed
```

## pop_front checks

`test_failures.py::test_failed_checks` · status `fail` · 10 lines of `error`

**Computed short description** (from the trace):

```
fail (checks failed: front, order)
```

**`error` today:**

```
test_failures.py:55: AssertionError: assert 1 == 2
 +  where 1 = <bound method LinkedList.pop_front of <linked_list.LinkedList object at 0xffffa5b74d10>>()
 +    where <bound method LinkedList.pop_front of <linked_list.LinkedList object at 0xffffa5b74d10>> = <linked_list.LinkedList object at 0xffffa5b74d10>.pop_front

test_failures.py:57: AssertionError: assert [2] == [3]
  
  At index 0 diff: 2 != 3
  Use -v to get more diff

rubric checks failed: front, order
```

## 7. A fixture (setup) error.

`test_failures.py::test_setup_error` · status `error` · 6 lines of `error`

**Computed short description** (from the trace):

```
error (test_failures.py:62: ConnectionError: the course's test server is down)
```

**`error` today:**

```
@pytest.fixture
    def server():
>       raise ConnectionError("the course's test server is down")
E       ConnectionError: the course's test server is down

/pedagog/tests/test_failures.py:62: ConnectionError
```

## 8. A failure deep in the student's code (a long traceback).

`test_failures.py::test_deep_recursion` · status `fail` · 19 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:71: AttributeError: 'NoneType' object has no attribute 'value')
```

**`error` today:**

```
def test_deep_recursion():
        """8. A failure deep in the student's code (a long traceback)."""
>       filled(1, 2).find(3)
E       AttributeError: 'NoneType' object has no attribute 'value'
E       student code traceback:
E         File "linked_list.py", line 50, in find
E           return self._find(self.head, value)
E                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E         File "linked_list.py", line 56, in _find
E           return self._find(node.next, value)
E                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E         File "linked_list.py", line 56, in _find
E           return self._find(node.next, value)
E                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E         File "linked_list.py", line 54, in _find
E           if node.value == value:
E              ^^^^^^^^^^

/pedagog/tests/test_failures.py:71: AttributeError
```

## prints the list

`test_failures.py::test_wrong_output` · status `fail` · 14 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:79: AssertionError: assert '' == '1 -> 2 -> 3\n')
```

**`error` today:**

```
score = <pedagog_pytest.scoring.Score object at 0xffff8b705e20>
capsys = <_pytest.capture.CaptureFixture object at 0xffff8b705dc0>

    @rubric_item("prints the list", points=1, id="show")
    def test_wrong_output(score: Score, capsys):
        """9. Comparing printed output (under process isolation, capsys doesn't see the
        student's prints: they happen in the other process)."""
        filled(1, 2, 3).show()
>       assert capsys.readouterr().out == "1 -> 2 -> 3\n"
E       AssertionError: assert '' == '1 -> 2 -> 3\n'
E         
E         - 1 -> 2 -> 3

/pedagog/tests/test_failures.py:79: AssertionError
```

## 10. A test that runs past its time limit.

`test_failures.py::test_timeout` · status `fail` · 42 lines of `error`

**Computed short description** (from the trace):

```
fail (test_failures.py:86: TestTimeout: test exceeded its 1s time limit)
```

**`error` today:**

```
@timeout(1)
    def test_timeout():
        """10. A test that runs past its time limit."""
>       filled(1).spin()

/pedagog/tests/test_failures.py:86: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/netref.py:239: in __call__
    return syncreq(_self, consts.HANDLE_CALL, args, kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/netref.py:63: in syncreq
    return conn.sync_request(handler, proxy, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/protocol.py:744: in sync_request
    return _async_res.value
           ^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/async_.py:109: in value
    self.wait()
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/async_.py:51: in wait
    self._conn.serve(self._ttl, waiting=self._waiting)
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/protocol.py:464: in serve
    data = self._channel.poll(timeout) and self._channel.recv()
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/channel.py:47: in poll
    return self.stream.poll(timeout)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/stream.py:48: in poll
    rl = p.poll(timeout.timeleft())
         ^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/lib/compat.py:124: in poll
    events = self._poll.poll(timeout)
             ^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

signum = 14
frame = <frame at 0xffff8b6b57b0, file '/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/lib/compat.py', line 124, code poll>

    def on_alarm(signum: int, frame: FrameType | None) -> None:
>       raise TestTimeout(f"test exceeded its {seconds:g}s time limit")
E       pedagog_pytest.plugin.TestTimeout: test exceeded its 1s time limit

/opt/pedagog/pytest/lib/python3.12/site-packages/pedagog_pytest/plugin.py:224: TestTimeout
```

## 11. A syntax error in the student's file

`run.status` `error`; `run.output`:

```
test_failures.py:
/opt/pedagog/pytest/lib/python3.12/site-packages/_pytest/python.py:498: in importtestmodule
    mod = import_path(
/opt/pedagog/pytest/lib/python3.12/site-packages/_pytest/pathlib.py:587: in import_path
    importlib.import_module(module_name)
/usr/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
<frozen importlib._bootstrap>:1387: in _gcd_import
    ???
<frozen importlib._bootstrap>:1360: in _find_and_load
    ???
<frozen importlib._bootstrap>:1331: in _find_and_load_unlocked
    ???
<frozen importlib._bootstrap>:935: in _load_unlocked
    ???
/opt/pedagog/pytest/lib/python3.12/site-packages/_pytest/assertion/rewrite.py:186: in exec_module
    exec(co, module.__dict__)
/pedagog/tests/test_failures.py:9: in <module>
    from linked_list import LinkedList
<frozen importlib._bootstrap>:1412: in _handle_fromlist
    ???
/opt/pedagog/pytest/lib/python3.12/site-packages/pedagog_pytest/isolation/client.py:336: in __getattr__
    module = session.unpack(session.root.load(self.__name__))
                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/netref.py:239: in __call__
    return syncreq(_self, consts.HANDLE_CALL, args, kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/netref.py:63: in syncreq
    return conn.sync_request(handler, proxy, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/protocol.py:744: in sync_request
    return _async_res.value
           ^^^^^^^^^^^^^^^^
/opt/pedagog/pytest/lib/python3.12/site-packages/rpyc/core/async_.py:111: in value
    raise self._obj
E     File "/tmp/pedagog-run-ax4d24ww/code/linked_list.py", line 2
E       def append(self, value)
E                              ^
E   _get_exception_class.<locals>.Derived: expected ':'
```
